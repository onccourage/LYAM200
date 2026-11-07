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
    original_total     TEXT,               -- итог модели до решений по апелляциям
    updated_at         TEXT,
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

-- Пакетные задания: прогресс хранится в БД → переживает перезапуск, можно продолжить.
CREATE TABLE IF NOT EXISTS batch_jobs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id           TEXT NOT NULL,
    checklist_id       TEXT NOT NULL,
    checklist_version  INTEGER NOT NULL,
    conversation_ids   TEXT NOT NULL,        -- JSON-массив
    full               INTEGER NOT NULL DEFAULT 0,
    skip_evaluated     INTEGER NOT NULL DEFAULT 1,
    status             TEXT NOT NULL,        -- queued | running | done | cancelled | interrupted | failed
    total              INTEGER NOT NULL,
    processed          INTEGER NOT NULL DEFAULT 0,
    succeeded          INTEGER NOT NULL DEFAULT 0,
    partial            INTEGER NOT NULL DEFAULT 0,
    failed             INTEGER NOT NULL DEFAULT 0,
    skipped            INTEGER NOT NULL DEFAULT 0,
    llm_calls          INTEGER NOT NULL DEFAULT 0,
    cost               TEXT NOT NULL DEFAULT '0',
    errors_json        TEXT NOT NULL DEFAULT '[]',
    current_json       TEXT NOT NULL DEFAULT '[]', -- что обрабатывается сейчас
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    finished_at        TEXT
);

CREATE TABLE IF NOT EXISTS appeals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id    INTEGER NOT NULL REFERENCES evaluations(id),
    group_id         TEXT NOT NULL,
    conversation_id  TEXT NOT NULL,
    criterion_id     TEXT NOT NULL,
    comment          TEXT NOT NULL,
    submitter_label  TEXT,
    status           TEXT NOT NULL DEFAULT 'open',   -- open | resolved
    original_answer  TEXT,                            -- NULL = критерий был needs_review
    original_points  TEXT,
    original_total   TEXT,                            -- итог диалога на момент подачи
    decision         TEXT,                            -- confirmed | changed
    new_answer       TEXT,
    new_gross_types  TEXT,                            -- JSON, только для flag
    new_points       TEXT,
    final_total      TEXT,                            -- итог после решения
    reason           TEXT,
    reviewer_label   TEXT,
    created_at       TEXT NOT NULL,
    resolved_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_appeals_eval ON appeals(evaluation_id, criterion_id, status);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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


# колонки, добавленные после первых версий схемы: (таблица, колонка, тип)
MIGRATIONS = [
    ("evaluations", "original_total", "TEXT"), ("evaluations", "updated_at", "TEXT"),
    ("appeals", "group_id", "TEXT"), ("appeals", "conversation_id", "TEXT"), ("appeals", "submitter_label", "TEXT"),
    ("appeals", "original_total", "TEXT"), ("appeals", "new_gross_types", "TEXT"), ("appeals", "new_points", "TEXT"),
    ("appeals", "final_total", "TEXT"),
    ("evaluations", "origin", "TEXT NOT NULL DEFAULT 'live'"),
    ("batch_jobs", "samples", "INTEGER NOT NULL DEFAULT 1"),
    ("evaluations", "samples", "INTEGER NOT NULL DEFAULT 1"),   # live — оценено здесь; seed — загружено из сида
]


def init_db(path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


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


def insert_checklist_exact(checklist: Checklist, path: Path | None = None) -> None:
    """Вставить версию с её номером (импорт сида). Существующие версии не перезаписываются."""
    with connect(path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO checklists(checklist_id, version, name, body_json, created_at) VALUES (?,?,?,?,?)",
            (checklist.id, checklist.version, checklist.name, checklist.model_dump_json(),
             checklist.created_at or now_iso()),
        )


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
            "raw_responses_json, is_current, created_at, origin, samples) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
            (
                rec["group_id"], rec["conversation_id"], rec["checklist_id"], rec["checklist_version"],
                rec.get("model"), rec.get("prompt_version"), rec["status"],
                rec.get("total"), int(bool(rec.get("gross_error"))),
                json.dumps(rec["answers"], ensure_ascii=False),
                json.dumps(rec.get("score"), ensure_ascii=False),
                rec.get("tokens_in", 0), rec.get("tokens_out", 0), rec.get("llm_calls", 0), str(rec.get("cost", "0")),
                json.dumps(rec.get("raw_responses", []), ensure_ascii=False), now_iso(), rec.get("origin", "live"),
                int(rec.get("samples") or 1),
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
    """Без сырых ответов — для списков. pending_count — критерии на ручной проверке."""
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT id, group_id, conversation_id, checklist_id, checklist_version, model, prompt_version, status, "
            "total, original_total, gross_error, tokens_in, tokens_out, llm_calls, cost, created_at, origin, samples, score_json, "
            "(SELECT COUNT(*) FROM appeals a WHERE a.evaluation_id=e.id AND a.status='open') AS open_appeals, "
            "(SELECT COUNT(*) FROM appeals a WHERE a.evaluation_id=e.id) AS appeals_count "
            "FROM evaluations e WHERE group_id=? AND is_current=1", (group_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        score = json.loads(d.pop("score_json") or "null") or {}
        d["gross_error"] = bool(d["gross_error"])
        d["gross_types"] = score.get("gross_types", [])
        d["pending_count"] = len(score.get("pending", []))
        out.append(d)
    return out


# ---------------------------------------------------------------- пакетные задания

def create_batch_job(group_id: str, checklist_id: str, checklist_version: int, conversation_ids: list[str],
                     full: bool, skip_evaluated: bool, path: Path | None = None, samples: int = 1) -> int:
    ts = now_iso()
    with connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO batch_jobs(group_id, checklist_id, checklist_version, conversation_ids, full, skip_evaluated, "
            "status, total, created_at, updated_at, samples) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (group_id, checklist_id, checklist_version, json.dumps(conversation_ids), int(full), int(skip_evaluated),
             "queued", len(conversation_ids), ts, ts, int(samples)),
        )
        return cur.lastrowid


def update_batch_job(job_id: int, path: Path | None = None, **fields) -> None:
    if not fields:
        return
    for k in ("errors", "current"):
        if k in fields:
            fields[f"{k}_json"] = json.dumps(fields.pop(k), ensure_ascii=False)
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect(path) as conn:
        conn.execute(f"UPDATE batch_jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def _job_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["conversation_ids"] = json.loads(d["conversation_ids"])
    d["errors"] = json.loads(d.pop("errors_json"))
    d["current"] = json.loads(d.pop("current_json"))
    d["full"] = bool(d["full"])
    d["skip_evaluated"] = bool(d["skip_evaluated"])
    return d


def get_batch_job(job_id: int, path: Path | None = None) -> dict | None:
    with connect(path) as conn:
        r = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
    return _job_row(r) if r else None


def list_batch_jobs(group_id: str | None = None, limit: int = 20, path: Path | None = None) -> list[dict]:
    with connect(path) as conn:
        if group_id:
            rows = conn.execute("SELECT * FROM batch_jobs WHERE group_id=? ORDER BY id DESC LIMIT ?", (group_id, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM batch_jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_job_row(r) for r in rows]


def mark_running_jobs_interrupted(path: Path | None = None) -> int:
    """При старте сервера: задания, оборванные падением процесса, можно продолжить."""
    with connect(path) as conn:
        cur = conn.execute("UPDATE batch_jobs SET status='interrupted', current_json='[]', updated_at=? "
                           "WHERE status IN ('running','queued')", (now_iso(),))
        return cur.rowcount


def dataset_stats(group_id: str, path: Path | None = None) -> dict:
    """Стоимость: по ВСЕЙ истории оценок набора (включая ретраи и инкрементальные переоценки)."""
    with connect(path) as conn:
        hist = conn.execute(
            "SELECT conversation_id, cost, tokens_in, tokens_out, llm_calls FROM evaluations WHERE group_id=?", (group_id,)
        ).fetchall()
        cur = conn.execute(
            "SELECT total, gross_error, status FROM evaluations WHERE group_id=? AND is_current=1", (group_id,)
        ).fetchall()
        n_conv = conn.execute("SELECT COUNT(*) FROM conversations WHERE group_id=?", (group_id,)).fetchone()[0]
    from decimal import ROUND_HALF_UP, Decimal
    spent = sum((Decimal(r["cost"]) for r in hist), Decimal(0))
    evaluated_convs = {r["conversation_id"] for r in hist}
    per_conv = (spent / len(evaluated_convs)) if evaluated_convs else None
    full_runs = [r for r in hist if r["llm_calls"] > 0]
    totals = [Decimal(r["total"]) for r in cur if r["total"] is not None]
    return {
        "conversations": n_conv,
        "evaluated": len(cur),
        "needs_review": sum(1 for r in cur if r["status"] == "partial"),
        "gross_errors": sum(1 for r in cur if r["gross_error"]),
        "avg_total": str((sum(totals, Decimal(0)) / len(totals)).quantize(Decimal("0.01"), ROUND_HALF_UP)) if totals else None,
        "spent": str(spent.quantize(Decimal("0.0001"), ROUND_HALF_UP)),
        "avg_cost_per_conversation": str(per_conv.quantize(Decimal("0.0001"), ROUND_HALF_UP)) if per_conv is not None else None,
        "avg_cost_per_llm_evaluation": str((sum((Decimal(r["cost"]) for r in full_runs), Decimal(0)) / len(full_runs)).quantize(Decimal("0.0001"), ROUND_HALF_UP)) if full_runs else None,
        "avg_tokens_in": round(sum(r["tokens_in"] for r in full_runs) / len(full_runs)) if full_runs else None,
        "avg_tokens_out": round(sum(r["tokens_out"] for r in full_runs) / len(full_runs)) if full_runs else None,
        "evaluations_total": len(hist),
    }


# ---------------------------------------------------------------- апелляции

def update_evaluation_result(evaluation_id: int, *, answers: list, score: dict, total: str | None, gross_error: bool,
                             status: str, original_total: str | None, path: Path | None = None,
                             conn: sqlite3.Connection | None = None) -> None:
    sql = ("UPDATE evaluations SET answers_json=?, score_json=?, total=?, gross_error=?, status=?, "
           "original_total=COALESCE(original_total, ?), updated_at=? WHERE id=?")
    args = (json.dumps(answers, ensure_ascii=False), json.dumps(score, ensure_ascii=False), total,
            int(bool(gross_error)), status, original_total, now_iso(), evaluation_id)
    if conn is not None:
        conn.execute(sql, args)
    else:
        with connect(path) as c:
            c.execute(sql, args)


def _appeal_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["new_gross_types"] = json.loads(d["new_gross_types"]) if d.get("new_gross_types") else []
    return d


def insert_appeal(rec: dict, path: Path | None = None) -> int:
    with connect(path) as conn:
        dup = conn.execute("SELECT id FROM appeals WHERE evaluation_id=? AND criterion_id=? AND status='open'",
                           (rec["evaluation_id"], rec["criterion_id"])).fetchone()
        if dup:
            raise ValueError(f"по этому критерию уже есть открытая апелляция #{dup['id']}")
        cur = conn.execute(
            "INSERT INTO appeals(evaluation_id, group_id, conversation_id, criterion_id, comment, submitter_label, status, "
            "original_answer, original_points, original_total, created_at) VALUES (?,?,?,?,?,?,'open',?,?,?,?)",
            (rec["evaluation_id"], rec["group_id"], rec["conversation_id"], rec["criterion_id"], rec["comment"],
             rec.get("submitter_label"), rec.get("original_answer"), rec.get("original_points"),
             rec.get("original_total"), now_iso()),
        )
        return cur.lastrowid


def get_appeal(appeal_id: int, path: Path | None = None) -> dict | None:
    with connect(path) as conn:
        r = conn.execute("SELECT * FROM appeals WHERE id=?", (appeal_id,)).fetchone()
    return _appeal_row(r) if r else None


def list_appeals(status: str | None = None, group_id: str | None = None, evaluation_id: int | None = None,
                 path: Path | None = None) -> list[dict]:
    q, args = "SELECT * FROM appeals WHERE 1=1", []
    if status:
        q += " AND status=?"; args.append(status)
    if group_id:
        q += " AND group_id=?"; args.append(group_id)
    if evaluation_id is not None:
        q += " AND evaluation_id=?"; args.append(evaluation_id)
    q += " ORDER BY (status='open') DESC, id DESC"
    with connect(path) as conn:
        rows = conn.execute(q, args).fetchall()
    return [_appeal_row(r) for r in rows]


def count_open_appeals(path: Path | None = None) -> int:
    with connect(path) as conn:
        return conn.execute("SELECT COUNT(*) FROM appeals WHERE status='open'").fetchone()[0]
