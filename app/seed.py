"""Сид предрасчитанных оценок: демо работает без API модели.

data/seed/{group_id}.json содержит версии чек-листа, которыми оценивали, текущие оценки набора
(вместе с сырыми ответами модели — для прозрачности) и кэш критериев (чтобы переоценка после
замены критерия отправляла в LLM только изменённые критерии и на свежей машине).
Транскрипты берутся из data/cache/{group_id}.json.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db
from .config import settings
from .dataset import load_dataset
from .models import Checklist

FORMAT = 1


def seed_dir() -> Path:
    return settings.data_dir / "seed"


def export_seed(group_id: str, out: Path | None = None, *, db_path: Path | None = None) -> Path:
    evs = [db.get_evaluation(e["id"], path=db_path) for e in db.list_current_evaluations(group_id, path=db_path)]
    if not evs:
        raise ValueError(f"в наборе {group_id} нет оценок для экспорта")
    versions = sorted({(e["checklist_id"], e["checklist_version"]) for e in evs})
    checklists = [db.get_checklist(cid, v, path=db_path).model_dump(mode="json") for cid, v in versions]
    with db.connect(db_path) as conn:
        cache = [dict(r) for r in conn.execute("SELECT cache_key, criterion_id, answer_json FROM criterion_cache")]
    keep = ("conversation_id", "checklist_id", "checklist_version", "model", "prompt_version", "status", "total",
            "original_total", "gross_error", "answers", "score", "tokens_in", "tokens_out", "llm_calls", "cost", "created_at",
            "raw_responses")
    doc = {
        "format": FORMAT,
        "group_id": group_id,
        "exported_at": db.now_iso(),
        "checklists": checklists,
        "evaluations": [{k: e.get(k) for k in keep} for e in sorted(evs, key=lambda e: e["conversation_id"])],
        "criterion_cache": [{"cache_key": c["cache_key"], "criterion_id": c["criterion_id"],
                             "answer": json.loads(c["answer_json"])} for c in cache],
    }
    out = out or seed_dir() / f"{group_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def import_seed(path: Path, *, db_path: Path | None = None, cache_dir: Path | None = None) -> dict:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("format") != FORMAT:
        raise ValueError(f"{path}: неизвестный формат сида {doc.get('format')}")
    group = doc["group_id"]
    if not db.list_conversations(group, path=db_path):
        load_dataset(group, refresh=False, cache_dir=cache_dir, db_path=db_path)  # транскрипты из data/cache
    conv_ids = {c.conversation_id for c in db.list_conversations(group, path=db_path)}

    # версии чек-листа — ровно с теми номерами, которыми оценивали
    loaded_versions, conflicts = 0, []
    for body in doc["checklists"]:
        cl = Checklist.model_validate(body)
        existing = db.get_checklist(cl.id, cl.version, path=db_path)
        if existing is None:
            db.insert_checklist_exact(cl, path=db_path)
            loaded_versions += 1
        elif existing.model_dump(mode="json", exclude={"created_at"}) != cl.model_dump(mode="json", exclude={"created_at"}):
            conflicts.append(f"{cl.id} v{cl.version}")
    if conflicts:
        raise ValueError(f"в БД уже есть другие версии с теми же номерами: {conflicts}. Импорт в чистую БД "
                         "(удалите data/cqa.db) или сохраните сид из этой БД заново.")

    imported, skipped = 0, 0
    for e in doc["evaluations"]:
        if e["conversation_id"] not in conv_ids:
            skipped += 1
            continue
        cur = db.get_current_evaluation(group, e["conversation_id"], path=db_path)
        if cur and cur["checklist_id"] == e["checklist_id"] and cur["checklist_version"] == e["checklist_version"]:
            skipped += 1  # уже есть оценка этой версией — не дублируем
            continue
        db.save_evaluation({**e, "group_id": group, "raw_responses": e.get("raw_responses") or [], "origin": "seed"},
                           path=db_path)
        imported += 1
    db.cache_put_many([(c["cache_key"], c["criterion_id"], c["answer"]) for c in doc["criterion_cache"]], path=db_path)
    return {"group_id": group, "checklist_versions": loaded_versions, "evaluations": imported, "skipped": skipped,
            "cache_entries": len(doc["criterion_cache"])}


def autoload(db_path: Path | None = None) -> list[dict]:
    """При старте: если оценок нет совсем — загрузить все сиды из data/seed."""
    with db.connect(db_path) as conn:
        if conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]:
            return []
    out = []
    for f in sorted(seed_dir().glob("*.json")):
        try:
            out.append(import_seed(f, db_path=db_path))
        except Exception as e:  # сид не должен ронять сервер
            out.append({"file": f.name, "error": str(e)})
    return out
