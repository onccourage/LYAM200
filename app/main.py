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
from .appeals import AppealError, appeal_details, create_appeal, resolve_appeal
from .batch import BatchManager
from .checklists import diff as checklist_diff
from .evaluator import evaluate_conversation
from .llm import LLMError, get_provider
from .models import Checklist, Conversation


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    db.seed_checklists()
    db.mark_running_jobs_interrupted()
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
                       checklist_version: int | None = None, full: bool = False) -> dict:
    """Оценить один диалог. full=true — сбросить кэш критериев для этого диалога."""
    conv = db.get_conversation(group_id, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Диалог не найден")
    cl = db.get_checklist(checklist_id, checklist_version)
    if cl is None:
        raise HTTPException(status_code=404, detail="Чек-лист не найден")
    _require_llm()
    try:
        provider = get_provider()
    except LLMError as e:
        raise HTTPException(status_code=503, detail=str(e))
    out = await evaluate_conversation(conv, cl, provider, use_cache=not full)
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


@app.get("/api/datasets/{group_id}/evaluations")
def api_list_evaluations(group_id: str) -> list[dict]:
    return db.list_current_evaluations(group_id)


@app.get("/api/datasets/{group_id}/stats")
def api_dataset_stats(group_id: str) -> dict:
    return db.dataset_stats(group_id)


# ---------------------------------------------------------------- batches

class BatchRequest(BaseModel):
    group_id: str
    conversation_ids: list[str] | None = None   # None/пусто — весь набор
    checklist_id: str = "default_chat"
    checklist_version: int | None = None        # None — последняя версия
    full: bool = False                          # сбросить кэш критериев
    skip_evaluated: bool = True                 # пропустить уже оценённые этой версией


def _require_llm() -> None:
    if settings.llm_provider == "anthropic" and not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY не задан в .env — оценка недоступна")


@app.post("/api/batches")
async def api_start_batch(req: BatchRequest) -> dict:
    _require_llm()
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
    job_id = batches.submit(req.group_id, cl, ids, full=req.full, skip_evaluated=req.skip_evaluated)
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
