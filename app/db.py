"""SQLite через стандартный sqlite3. JSON-поля хранятся как TEXT.

Версии чек-листов неизменяемы: сохранение = новая строка (checklist_id, version).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import settings
from .models import Checklist, Conversation

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    group_id       TEXT PRIMARY KEY,
    source         TEXT NOT NULL,          -- api | cache | upload
    total_records  INTEGER,
    fetched_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    group_id         TEXT NOT NULL,
    conversation_id  TEXT NOT NULL,
    channel          TEXT NOT NULL,
    language         TEXT NOT NULL,
    audio_url        TEXT,
    messages_json    TEXT NOT NULL,
    raw_json         TEXT NOT NULL,
    transcript_hash  TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (group_id, conversation_id)
);

CREATE TABLE IF NOT EXISTS checklists (
    checklist_id  TEXT NOT NULL,
    version       INTEGER NOT NULL,
    name          TEXT NOT NULL,
    body_json     TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (checklist_id, version)
);

-- Оценки (заполняются начиная с этапа 3). Ссылаются на конкретную версию чек-листа.
CREATE TABLE IF NOT EXISTS evaluations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id           TEXT NOT NULL,
    conversation_id    TEXT NOT NULL,
    checklist_id       TEXT NOT NULL,
    checklist_version  INTEGER NOT NULL,
    model              TEXT,
    prompt_version     TEXT,
    status             TEXT NOT NULL,      -- done | partial | error
    total              TEXT,               -- Decimal строкой; NULL = не рассчитывается
    gross_error        INTEGER NOT NULL DEFAULT 0,
    answers_json       TEXT NOT NULL,      -- ответы по критериям (после валидации)
    score_json         TEXT,               -- ScoreResult
    tokens_in          INTEGER NOT NULL DEFAULT 0,
    tokens_out         INTEGER NOT NULL DEFAULT 0,
    llm_calls          INTEGER NOT NULL DEFAULT 0,
    cost               TEXT NOT NULL DEFAULT '0',
    raw_responses_json TEXT,
    is_current         INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_eval_conv ON evaluations(group_id, conversation_id, is_current);

-- Кэш на уровне критерия (раздел 5.5)
CREATE TABLE IF NOT EXISTS criterion_cache (
    cache_key        TEXT PRIMARY KEY,     -- hash(транскрипт)+hash(критерий)+модель+PROMPT_VERSION
    criterion_id     TEXT NOT NULL,
    answer_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS appeals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id    INTEGER NOT NULL REFERENCES evaluations(id),
    criterion_id     TEXT NOT NULL,
    comment          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open',   -- open | resolved
    original_answer  TEXT,
    original_points  TEXT,
    decision         TEXT,                            -- confirmed | changed
    new_answer       TEXT,
    reason           TEXT,
    reviewer_label   TEXT,
    created_at       TEXT NOT NULL,
    resolved_at      TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    db_path = Path(path or settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------- наборы и диалоги

def save_dataset(group_id: str, source: str, conversations: list[Conversation], path: Path | None = None) -> None:
    ts = now_iso()
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO datasets(group_id, source, total_records, fetched_at) VALUES (?,?,?,?) "
            "ON CONFLICT(group_id) DO UPDATE SET source=excluded.source, "
            "total_records=excluded.total_records, fetched_at=excluded.fetched_at",
            (group_id, source, len(conversations), ts),
        )
        for c in conversations:
            _upsert_conversation(conn, c, ts)


def add_conversations(group_id: str, conversations: list[Conversation], path: Path | None = None) -> None:
    """Добавить диалоги в набор (ручная загрузка), не затирая остальные."""
    ts = now_iso()
    with connect(path) as conn:
        for c in conversations:
            _upsert_conversation(conn, c, ts)
        n = conn.execute("SELECT COUNT(*) FROM conversations WHERE group_id=?", (group_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO datasets(group_id, source, total_records, fetched_at) VALUES (?,?,?,?) "
            "ON CONFLICT(group_id) DO UPDATE SET total_records=excluded.total_records, fetched_at=excluded.fetched_at",
            (group_id, "upload", n, ts),
        )


def _upsert_conversation(conn: sqlite3.Connection, c: Conversation, ts: str) -> None:
    conn.execute(
        "INSERT INTO conversations(group_id, conversation_id, channel, language, audio_url, "
        "messages_json, raw_json, transcript_hash, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(group_id, conversation_id) DO UPDATE SET channel=excluded.channel, "
        "language=excluded.language, audio_url=excluded.audio_url, messages_json=excluded.messages_json, "
        "raw_json=excluded.raw_json, transcript_hash=excluded.transcript_hash, updated_at=excluded.updated_at",
        (
            c.group_id, c.conversation_id, c.channel, c.language, c.audio_url,
            json.dumps([m.model_dump() for m in c.messages], ensure_ascii=False),
            json.dumps(c.raw, ensure_ascii=False),
            c.transcript_hash(), ts,
        ),
    )


def list_datasets(path: Path | None = None) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute("SELECT * FROM datasets ORDER BY fetched_at DESC").fetchall()
    return [dict(r) for r in rows]


def _row_to_conversation(r: sqlite3.Row) -> Conversation:
    return Conversation(
        conversation_id=r["conversation_id"], group_id=r["group_id"], channel=r["channel"],
        language=r["language"], audio_url=r["audio_url"],
        messages=json.loads(r["messages_json"]), raw=json.loads(r["raw_json"]),
    )


def list_conversations(group_id: str, path: Path | None = None) -> list[Conversation]:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE group_id=? ORDER BY conversation_id", (group_id,)
        ).fetchall()
    return [_row_to_conversation(r) for r in rows]


def get_conversation(group_id: str, conversation_id: str, path: Path | None = None) -> Conversation | None:
    with connect(path) as conn:
        r = conn.execute(
            "SELECT * FROM conversations WHERE group_id=? AND conversation_id=?", (group_id, conversation_id)
        ).fetchone()
    return _row_to_conversation(r) if r else None


# ---------------------------------------------------------------- чек-листы (версии неизменяемы)

def save_checklist_version(checklist: Checklist, path: Path | None = None) -> Checklist:
    """Сохранить как НОВУЮ версию. Номер версии назначается здесь, а не берётся из входа."""
    with connect(path) as conn:
        row = conn.execute(
            "SELECT MAX(version) FROM checklists WHERE checklist_id=?", (checklist.id,)
        ).fetchone()
        next_version = (row[0] or 0) + 1
        saved = checklist.model_copy(update={"version": next_version, "created_at": now_iso()})
        conn.execute(
            "INSERT INTO checklists(checklist_id, version, name, body_json, created_at) VALUES (?,?,?,?,?)",
            (saved.id, saved.version, saved.name, saved.model_dump_json(), saved.created_at),
        )
    return saved


def get_checklist(checklist_id: str, version: int | None = None, path: Path | None = None) -> Checklist | None:
    with connect(path) as conn:
        if version is None:
            r = conn.execute(
                "SELECT body_json FROM checklists WHERE checklist_id=? ORDER BY version DESC LIMIT 1",
                (checklist_id,),
            ).fetchone()
        else:
            r = conn.execute(
                "SELECT body_json FROM checklists WHERE checklist_id=? AND version=?", (checklist_id, version)
            ).fetchone()
    return Checklist.model_validate_json(r["body_json"]) if r else None


def list_checklists(path: Path | None = None) -> list[dict]:
    """Последняя версия каждого чек-листа + число версий."""
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT checklist_id, MAX(version) AS latest_version, COUNT(*) AS versions_count, "
            "MAX(created_at) AS updated_at FROM checklists GROUP BY checklist_id ORDER BY checklist_id"
        ).fetchall()
        out = []
        for r in rows:
            name = conn.execute(
                "SELECT name FROM checklists WHERE checklist_id=? AND version=?",
                (r["checklist_id"], r["latest_version"]),
            ).fetchone()["name"]
            out.append({**dict(r), "name": name})
    return out


def list_checklist_versions(checklist_id: str, path: Path | None = None) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT checklist_id, version, name, created_at FROM checklists WHERE checklist_id=? ORDER BY version",
            (checklist_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def seed_checklists(path: Path | None = None) -> list[str]:
    """Загрузить сид-чек-листы из data/checklists/*.json, если их ещё нет в БД."""
    seeded = []
    for f in sorted(settings.checklists_dir.glob("*.json")):
        cl = Checklist.model_validate_json(f.read_text(encoding="utf-8"))
        if get_checklist(cl.id, path=path) is None:
            save_checklist_version(cl, path=path)
            seeded.append(cl.id)
    return seeded


# ---------------------------------------------------------------- кэш критериев

def cache_get_many(keys: list[str], path: Path | None = None) -> dict[str, dict]:
    if not keys:
        return {}
    with connect(path) as conn:
        q = ",".join("?" * len(keys))
        rows = conn.execute(f"SELECT cache_key, answer_json FROM criterion_cache WHERE cache_key IN ({q})", keys).fetchall()
    return {r["cache_key"]: json.loads(r["answer_json"]) for r in rows}


def cache_put_many(items: list[tuple[str, str, dict]], path: Path | None = None) -> None:
    """items: (cache_key, criterion_id, verdict_dict)."""
    ts = now_iso()
    with connect(path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO criterion_cache(cache_key, criterion_id, answer_json, created_at) VALUES (?,?,?,?)",
            [(k, cid, json.dumps(v, ensure_ascii=False), ts) for k, cid, v in items],
        )


def cache_delete_for(keys: list[str], path: Path | None = None) -> None:
    if not keys:
        return
    with connect(path) as conn:
        conn.execute(f"DELETE FROM criterion_cache WHERE cache_key IN ({','.join('?' * len(keys))})", keys)


# ---------------------------------------------------------------- оценки

EVAL_JSON_FIELDS = ("answers_json", "score_json", "raw_responses_json")


def save_evaluation(rec: dict, path: Path | None = None) -> int:
    """Сохранить оценку как текущую для диалога; прежние помечаются is_current=0 (история сохраняется)."""
    with connect(path) as conn:
        conn.execute(
            "UPDATE evaluations SET is_current=0 WHERE group_id=? AND conversation_id=?",
            (rec["group_id"], rec["conversation_id"]),
        )
        cur = conn.execute(
            "INSERT INTO evaluations(group_id, conversation_id, checklist_id, checklist_version, model, prompt_version, "
            "status, total, gross_error, answers_json, score_json, tokens_in, tokens_out, llm_calls, cost, "
            "raw_responses_json, is_current, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
            (
                rec["group_id"], rec["conversation_id"], rec["checklist_id"], rec["checklist_version"],
                rec.get("model"), rec.get("prompt_version"), rec["status"],
                rec.get("total"), int(bool(rec.get("gross_error"))),
                json.dumps(rec["answers"], ensure_ascii=False),
                json.dumps(rec.get("score"), ensure_ascii=False),
                rec.get("tokens_in", 0), rec.get("tokens_out", 0), rec.get("llm_calls", 0), str(rec.get("cost", "0")),
                json.dumps(rec.get("raw_responses", []), ensure_ascii=False), now_iso(),
            ),
        )
        return cur.lastrowid


def _eval_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["answers"] = json.loads(d.pop("answers_json"))
    d["score"] = json.loads(d.pop("score_json") or "null")
    d["raw_responses"] = json.loads(d.pop("raw_responses_json") or "[]")
    d["gross_error"] = bool(d["gross_error"])
    d["is_current"] = bool(d["is_current"])
    return d


def get_evaluation(evaluation_id: int, path: Path | None = None) -> dict | None:
    with connect(path) as conn:
        r = conn.execute("SELECT * FROM evaluations WHERE id=?", (evaluation_id,)).fetchone()
    return _eval_row(r) if r else None


def get_current_evaluation(group_id: str, conversation_id: str, path: Path | None = None) -> dict | None:
    with connect(path) as conn:
        r = conn.execute(
            "SELECT * FROM evaluations WHERE group_id=? AND conversation_id=? AND is_current=1 ORDER BY id DESC LIMIT 1",
            (group_id, conversation_id),
        ).fetchone()
    return _eval_row(r) if r else None


def list_current_evaluations(group_id: str, path: Path | None = None) -> list[dict]:
    """Без сырых ответов — для списков."""
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT id, group_id, conversation_id, checklist_id, checklist_version, model, prompt_version, status, "
            "total, gross_error, tokens_in, tokens_out, llm_calls, cost, created_at FROM evaluations "
            "WHERE group_id=? AND is_current=1", (group_id,),
        ).fetchall()
    return [dict(r, gross_error=bool(r["gross_error"])) for r in rows]
