"""FastAPI: API наборов, диалогов, чек-листов + раздача статики /web."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import db
from .config import settings
from .dataset import DatasetError, LoadResult, load_dataset, load_upload
from .evaluator import evaluate_conversation
from .llm import LLMError, get_provider
from .models import Checklist, Conversation


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    db.seed_checklists()
    yield


app = FastAPI(title="CQA — Conversation Quality Analytics", lifespan=lifespan)


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
    if settings.llm_provider == "anthropic" and not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY не задан в .env — оценка недоступна")
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
    return ev


@app.get("/api/datasets/{group_id}/evaluations")
def api_list_evaluations(group_id: str) -> list[dict]:
    return db.list_current_evaluations(group_id)


# ---------------------------------------------------------------- static

if settings.web_dir.exists():
    app.mount("/static", StaticFiles(directory=settings.web_dir), name="static")
    for sub in ("js", "css"):
        if (settings.web_dir / sub).exists():
            app.mount(f"/{sub}", StaticFiles(directory=settings.web_dir / sub), name=sub)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(settings.web_dir / "index.html")
