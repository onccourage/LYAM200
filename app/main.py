"""FastAPI: API наборов, диалогов, чек-листов + раздача статики /web."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import db
from .config import settings
from .dataset import DatasetError, LoadResult, load_dataset, load_upload
from .audio import AudioError, resolve_audio_url
from .analytics import compute as compute_analytics
from .appeals import AppealError, appeal_details, create_appeal, resolve_appeal
from .batch import BatchManager
from .checklists import diff as checklist_diff
from .evaluator import estimate_impact, evaluate_conversation
from .gold import compare as gold_compare, load_gold
from .llm import LLMError, get_provider
from .models import Checklist, Conversation
from .seed import autoload


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    db.seed_checklists()
    db.mark_running_jobs_interrupted()
    if settings.seed_on_start:
        for r in autoload():
            print(f"[seed] {r}")
    yield


app = FastAPI(title="CQA — Conversation Quality Analytics", lifespan=lifespan)
batches = BatchManager()


# ---------------------------------------------------------------- helpers

def _summary(c: Conversation) -> dict:
    speakers: dict[str, int] = {}
    for m in c.messages:
        speakers[m.speaker] = speakers.get(m.speaker, 0) + 1
    return {
        "conversation_id": c.conversation_id,
        "group_id": c.group_id,
        "channel": c.channel,
        "language": c.language,
        "has_audio": bool(c.audio_url),
        "messages_count": len(c.messages),
        "speakers": speakers,
    }


def _load_response(r: LoadResult) -> dict:
    return {
        "group_id": r.group_id,
        "source": r.source,
        "total": len(r.conversations),
        "warnings": r.warnings,
        "conversations": [_summary(c) for c in r.conversations],
    }


def _checklist_response(cl: Checklist) -> dict:
    return {"checklist": cl.model_dump(mode="json"), "warnings": cl.warnings(), "total_weight": float(cl.total_weight())}


# ---------------------------------------------------------------- system

@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/config")
def public_config() -> dict:
    return settings.public()


# ---------------------------------------------------------------- datasets

@app.post("/api/datasets/{group_id}/load")
async def api_load_dataset(group_id: str, refresh: bool = True) -> dict:
    try:
        result = await run_in_threadpool(load_dataset, group_id, refresh=refresh)
    except DatasetError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _load_response(result)


@app.get("/api/datasets")
def api_list_datasets() -> list[dict]:
    return db.list_datasets()


@app.get("/api/datasets/{group_id}/conversations")
def api_list_conversations(group_id: str) -> list[dict]:
    """Диалоги набора + сводка текущей оценки (для списка и фильтров)."""
    evals = {e["conversation_id"]: e for e in db.list_current_evaluations(group_id)}
    out = []
    for c in db.list_conversations(group_id):
        row = _summary(c)
        row["preview"] = next((" ".join(m.text.split())[:120] for m in c.messages if m.speaker == "client"), "")
        row["evaluation"] = evals.get(c.conversation_id)
        out.append(row)
    return out


@app.get("/api/conversations/{group_id}/{conversation_id}")
def api_get_conversation(group_id: str, conversation_id: str) -> dict:
    c = db.get_conversation(group_id, conversation_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Диалог не найден")
    return c.model_dump(exclude={"raw"})


@app.get("/api/audio/{group_id}/{conversation_id}")
async def api_audio(group_id: str, conversation_id: str):
    """Перенаправить на актуальную ссылку на запись звонка (audio_url организаторов — HTML-страница)."""
    c = db.get_conversation(group_id, conversation_id)
    if c is None or not c.audio_url:
        raise HTTPException(status_code=404, detail="У диалога нет записи")
    try:
        url = await run_in_threadpool(resolve_audio_url, c.audio_url)
    except AudioError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return RedirectResponse(url, status_code=302)


@app.post("/api/conversations/upload")
async def api_upload(file: UploadFile = File(...), group_id: str = Form("MANUAL")) -> dict:
    try:
        payload = json.loads((await file.read()).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"Файл не является корректным JSON: {e}")
    try:
        result = load_upload(payload, group_id=group_id)
    except DatasetError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _load_response(result)


# ---------------------------------------------------------------- checklists

@app.get("/api/checklists")
def api_list_checklists() -> list[dict]:
    return db.list_checklists()


@app.get("/api/checklists/{checklist_id}")
def api_get_checklist(checklist_id: str, version: int | None = None) -> dict:
    cl = db.get_checklist(checklist_id, version)
    if cl is None:
        raise HTTPException(status_code=404, detail="Чек-лист не найден")
    return _checklist_response(cl)


@app.get("/api/checklists/{checklist_id}/versions")
def api_checklist_versions(checklist_id: str) -> list[dict]:
    return db.list_checklist_versions(checklist_id)


def _parse_checklist(body: dict) -> Checklist:
    try:
        return Checklist.model_validate(body)
    except ValidationError as e:
        errors = [f"{'.'.join(str(x) for x in err['loc']) or 'checklist'}: {err['msg']}" for err in e.errors()]
        raise HTTPException(status_code=422, detail={"errors": errors})


@app.get("/api/checklists/{checklist_id}/diff")
def api_checklist_diff(checklist_id: str, from_version: int, to_version: int | None = None) -> dict:
    """Что изменилось между версиями: какие критерии потребуют вызова LLM, а какие только пересчёта."""
    old = db.get_checklist(checklist_id, from_version)
    new = db.get_checklist(checklist_id, to_version)
    if old is None or new is None:
        raise HTTPException(status_code=404, detail="Версия чек-листа не найдена")
    return {"from_version": old.version, "to_version": new.version, "changes": checklist_diff(old, new)}


@app.post("/api/checklists/validate")
def api_validate_checklist(body: dict) -> dict:
    """Проверка без сохранения — для живого предпросмотра в редакторе."""
    return _checklist_response(_parse_checklist(body))


@app.post("/api/checklists")
def api_save_checklist(body: dict) -> dict:
    """Сохранить как НОВУЮ версию (старые версии неизменяемы)."""
    saved = db.save_checklist_version(_parse_checklist(body))
    return _checklist_response(saved)


# ---------------------------------------------------------------- evaluations

@app.post("/api/evaluations/{group_id}/{conversation_id}")
async def api_evaluate(group_id: str, conversation_id: str, checklist_id: str = "default_chat",
                       checklist_version: int | None = None, full: bool = False, samples: int | None = None,
                       provider: str = "default") -> dict:
    """Оценить один диалог. full=true — сбросить кэш критериев для этого диалога.
    provider — id профиля модели (переключатель в UI; "default" — из .env)."""
    conv = db.get_conversation(group_id, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Диалог не найден")
    cl = db.get_checklist(checklist_id, checklist_version)
    if cl is None:
        raise HTTPException(status_code=404, detail="Чек-лист не найден")
    try:
        settings.get_profile(provider)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _require_llm(provider)
    try:
        provider_obj = get_provider(provider)
    except LLMError as e:
        raise HTTPException(status_code=503, detail=str(e))
    out = await evaluate_conversation(conv, cl, provider_obj, use_cache=not full, samples=samples)
    if out.record["status"] == "error":
        raise HTTPException(status_code=502, detail=f"Модель недоступна: {out.record['llm_error']}")
    rec = dict(out.record)
    rec.pop("raw_responses", None)
    return {**rec, "llm_criteria": out.llm_criteria, "cached_criteria": out.cached_criteria}


@app.get("/api/evaluations/{group_id}/{conversation_id}")
def api_get_evaluation(group_id: str, conversation_id: str, raw: bool = False) -> dict:
    ev = db.get_current_evaluation(group_id, conversation_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="Диалог ещё не оценён")
    if not raw:
        ev.pop("raw_responses", None)
    ev["appeals"] = db.list_appeals(evaluation_id=ev["id"])
    return ev


@app.get("/api/evaluations/{group_id}/{conversation_id}/prompt")
def api_evaluation_prompt(group_id: str, conversation_id: str) -> dict:
    """Промпты, по которым модель оценивала диалог: рамка + критерии версии чек-листа + факты + транскрипт.

    Критерии реально отправлялись группами (evaluator._chunk — иначе Anthropic отклоняет схему на
    ~6+ разнородных критериях в одном запросе), поэтому здесь restoреставрируется РОВНО столько
    запросов, сколько реально ушло: по группе llm_criteria_chunk_size на вызов.
    """
    from .evaluator import _chunk
    from .preprocess import preprocess
    from .prompt_builder import PROMPT_VERSION, build_request
    from .scoring import applies_to_channel
    ev = db.get_current_evaluation(group_id, conversation_id)
    conv = db.get_conversation(group_id, conversation_id)
    if ev is None or conv is None:
        raise HTTPException(status_code=404, detail="Диалог ещё не оценён")
    cl = db.get_checklist(ev["checklist_id"], ev["checklist_version"])
    crit = [c for c in cl.enabled_criteria if applies_to_channel(c, conv.channel) and not c.requires_audio]
    prepared = preprocess(conv)
    chunks = _chunk(crit, settings.llm_criteria_chunk_size) or [[]]
    requests = [{**build_request(prepared, chunk), "criteria": [c.id for c in chunk]} for chunk in chunks]
    return {"requests": requests, "prompt_version": PROMPT_VERSION, "evaluated_with": ev["prompt_version"],
            "note": f"Восстановлено {len(requests)} запрос(ов) из версии чек-листа и транскрипта "
                    f"(группы по {settings.llm_criteria_chunk_size} критериев — реальное разбиение могло "
                    "отличаться, если настройка менялась после оценки)" +
                    ("" if ev["prompt_version"] == PROMPT_VERSION else f"; оценка сделана рамкой {ev['prompt_version']}")}


@app.get("/api/datasets/{group_id}/evaluations")
def api_list_evaluations(group_id: str) -> list[dict]:
    return db.list_current_evaluations(group_id)


@app.get("/api/datasets/{group_id}/analytics")
def api_analytics(group_id: str, checklist_id: str | None = None) -> dict:
    return compute_analytics(group_id, checklist_id)


@app.get("/api/datasets/{group_id}/gold-agreement")
def api_gold_agreement(group_id: str, checklist_id: str = "default_chat") -> dict:
    """Согласие текущих оценок с эталонной (человеческой) разметкой data/gold/*.json — тот же расчёт,
    что в scripts/eval_gold.py, но живьём в UI (критерий жюри «точность vs Ground Truth»)."""
    gold = [g for g in load_gold(settings.data_dir / "gold") if g.get("group_id", group_id) == group_id]
    total_slots = sum(len(g.get("answers") or {}) for g in gold)
    filled = sum(1 for g in gold for v in (g.get("answers") or {}).values() if v not in (None, "", {}))
    if not gold:
        return {"available": False, "reason": "Эталонная разметка для набора не найдена (data/gold/*.json)",
                "gold_files": 0, "annotation_progress": {"filled": 0, "total": 0}}
    convs = {c.conversation_id: c for c in db.list_conversations(group_id)}
    default_cl = db.get_checklist(checklist_id)
    evals: dict[str, dict] = {}
    cls: dict[tuple, Checklist] = {}
    for g in gold:
        ev = db.get_current_evaluation(group_id, g["conversation_id"])
        if ev:
            evals[g["conversation_id"]] = ev
            key = (ev["checklist_id"], ev["checklist_version"])
            if key not in cls:
                cls[key] = db.get_checklist(*key)
    result = gold_compare(gold, evals, convs, cls, default_cl)
    result["available"] = True
    result["gold_files"] = len(gold)
    result["annotation_progress"] = {"filled": filled, "total": total_slots}
    return result


@app.get("/api/datasets/{group_id}/stats")
def api_dataset_stats(group_id: str) -> dict:
    return db.dataset_stats(group_id)


class ImpactRequest(BaseModel):
    checklist: dict                             # черновик из редактора — можно ещё не сохранённый
    conversation_ids: list[str] | None = None    # None — весь набор
    samples: int = 1
    provider: str = "default"


@app.post("/api/datasets/{group_id}/impact")
def api_impact(group_id: str, req: ImpactRequest) -> dict:
    """Предпросмотр влияния правки чек-листа на набор БЕЗ вызова LLM и без сохранения: сколько диалогов
    уже полностью из кэша, сколько критериев реально уйдёт в модель и примерная стоимость. Для редактора
    чек-листа — проверить правку до того, как тратить вызовы на весь набор (контрольный час, раздел 9)."""
    cl = _parse_checklist(req.checklist)
    try:
        profile = settings.get_profile(req.provider)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if req.conversation_ids:
        convs = [c for c in (db.get_conversation(group_id, cid) for cid in req.conversation_ids) if c]
    else:
        convs = db.list_conversations(group_id)
    if not convs:
        raise HTTPException(status_code=404, detail="Набор пуст или не найден")
    result = estimate_impact(cl, convs, model=profile.model, samples=req.samples,
                             price_input_per_mtok=profile.price_input_per_mtok,
                             price_output_per_mtok=profile.price_output_per_mtok)
    saved = db.get_checklist(cl.id)   # последняя сохранённая версия — для читаемого списка изменений
    result["changes"] = checklist_diff(saved, cl) if saved else []
    return result


# ---------------------------------------------------------------- batches

class BatchRequest(BaseModel):
    group_id: str
    conversation_ids: list[str] | None = None   # None/пусто — весь набор
    checklist_id: str = "default_chat"
    checklist_version: int | None = None        # None — последняя версия
    full: bool = False                          # сбросить кэш критериев
    skip_evaluated: bool = True                 # пропустить уже оценённые этой версией
    samples: int = 1                            # >1 — консенсус нескольких проходов модели
    provider: str = "default"                   # id профиля модели (переключатель в UI)


def _require_llm(profile: str = "default") -> None:
    # для не-основного профиля (например, локальный Ollama без ключа) проверка ключа не применима —
    # конкретную ошибку подключения покажет сам провайдер при вызове (LLMError → 503)
    if profile == "default" and settings.llm_provider == "anthropic" and not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY не задан в .env — оценка недоступна")


@app.post("/api/batches")
async def api_start_batch(req: BatchRequest) -> dict:
    try:
        settings.get_profile(req.provider)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _require_llm(req.provider)
    cl = db.get_checklist(req.checklist_id, req.checklist_version)
    if cl is None:
        raise HTTPException(status_code=404, detail="Чек-лист не найден")
    existing = {c.conversation_id for c in db.list_conversations(req.group_id)}
    if not existing:
        raise HTTPException(status_code=404, detail=f"Набор {req.group_id} не загружен")
    ids = req.conversation_ids or sorted(existing)
    unknown = [i for i in ids if i not in existing]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Нет таких диалогов в наборе: {unknown[:10]}")
    active = batches.active_for(req.group_id)
    if active:
        raise HTTPException(status_code=409, detail=f"Для набора уже идёт пакетная оценка #{active}")
    if not 1 <= req.samples <= 7:
        raise HTTPException(status_code=400, detail="samples: от 1 до 7")
    job_id = batches.submit(req.group_id, cl, ids, full=req.full, skip_evaluated=req.skip_evaluated,
                            samples=req.samples, provider=req.provider)
    return db.get_batch_job(job_id)


@app.get("/api/batches")
def api_list_batches(group_id: str | None = None, limit: int = 10) -> list[dict]:
    return db.list_batch_jobs(group_id, limit)


@app.get("/api/batches/{job_id}")
def api_get_batch(job_id: int) -> dict:
    job = db.get_batch_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    return job


@app.post("/api/batches/{job_id}/cancel")
def api_cancel_batch(job_id: int) -> dict:
    if not batches.cancel(job_id):
        raise HTTPException(status_code=409, detail="Задание не выполняется")
    return {"ok": True}


@app.post("/api/batches/{job_id}/resume")
async def api_resume_batch(job_id: int) -> dict:
    _require_llm()
    job = db.get_batch_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    if job["status"] in ("running", "queued"):
        raise HTTPException(status_code=409, detail="Задание уже выполняется")
    active = batches.active_for(job["group_id"])
    if active:
        raise HTTPException(status_code=409, detail=f"Для набора уже идёт пакетная оценка #{active}")
    batches.resume(job_id)
    return db.get_batch_job(job_id)


# ---------------------------------------------------------------- appeals

class AppealCreate(BaseModel):
    evaluation_id: int
    criterion_id: str
    comment: str
    submitter_label: str | None = None


class AppealResolve(BaseModel):
    decision: str                        # confirmed | changed
    reason: str
    reviewer_label: str | None = None
    new_answer: str | int | float | None = None
    new_gross_types: list[str] | None = None


def _appeal_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except AppealError as e:
        raise HTTPException(status_code=e.status, detail=str(e))


@app.post("/api/appeals")
def api_create_appeal(req: AppealCreate) -> dict:
    return _appeal_call(create_appeal, req.evaluation_id, req.criterion_id, req.comment, req.submitter_label)


@app.get("/api/appeals")
def api_list_appeals(status: str | None = None, group_id: str | None = None) -> list[dict]:
    return db.list_appeals(status=status, group_id=group_id)


@app.get("/api/appeals/summary")
def api_appeals_summary() -> dict:
    return {"open": db.count_open_appeals()}


@app.get("/api/appeals/{appeal_id}")
def api_get_appeal(appeal_id: int) -> dict:
    return _appeal_call(appeal_details, appeal_id)


@app.post("/api/appeals/{appeal_id}/resolve")
def api_resolve_appeal(appeal_id: int, req: AppealResolve) -> dict:
    _appeal_call(resolve_appeal, appeal_id, req.decision, req.reason, req.reviewer_label,
                 new_answer=req.new_answer, new_gross_types=req.new_gross_types)
    return _appeal_call(appeal_details, appeal_id)


# ---------------------------------------------------------------- static

if settings.web_dir.exists():
    app.mount("/static", StaticFiles(directory=settings.web_dir), name="static")
    for sub in ("js", "css"):
        if (settings.web_dir / sub).exists():
            app.mount(f"/{sub}", StaticFiles(directory=settings.web_dir / sub), name=sub)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(settings.web_dir / "index.html")
