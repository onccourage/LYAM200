"""Пакетная оценка набора: параллелизм (семафор), ретраи с экспоненциальной задержкой,
возобновление после падения (пропуск уже оценённых этой версией), прогресс в БД.

- Ретраи 429/5xx/сеть на уровне одного HTTP-вызова делает SDK (LLM_MAX_RETRIES).
  Здесь — повтор всего диалога, если модель осталась недоступна (status=error), BATCH_RETRIES раз.
- «Уже оценён» = текущая оценка той же checklist_id+version со статусом done. Диалоги со статусом partial
  переоцениваются, но кэш критериев отправит в LLM только критерии needs_review.
- Применение новой версии чек-листа к набору = обычный запуск: неизменённые критерии берутся из кэша.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from pathlib import Path
from typing import Callable

from . import db
from .config import settings
from .evaluator import evaluate_conversation
from .llm import LLMProvider, get_provider
from .models import Checklist

log = logging.getLogger("cqa.batch")


def is_up_to_date(group_id: str, conversation_id: str, checklist: Checklist, path: Path | None = None,
                  not_before: str | None = None) -> bool:
    """Текущая оценка той же версии чек-листа, статус done (и, для полной переоценки, сделана не раньше not_before)."""
    ev = db.get_current_evaluation(group_id, conversation_id, path=path)
    return bool(ev and ev["checklist_id"] == checklist.id and ev["checklist_version"] == checklist.version
                and ev["status"] == "done" and (not_before is None or ev["created_at"] > not_before))


async def run_job(job_id: int, provider: LLMProvider, *, db_path: Path | None = None,
                  concurrency: int | None = None, retries: int | None = None,
                  retry_base_sec: float | None = None, cancel_event: asyncio.Event | None = None) -> dict:
    job = db.get_batch_job(job_id, path=db_path)
    if job is None:
        raise ValueError(f"задание {job_id} не найдено")
    checklist = db.get_checklist(job["checklist_id"], job["checklist_version"], path=db_path)
    if checklist is None:
        db.update_batch_job(job_id, path=db_path, status="failed", errors=[{"error": "чек-лист не найден"}])
        return db.get_batch_job(job_id, path=db_path)

    concurrency = concurrency or settings.batch_concurrency
    retries = settings.batch_retries if retries is None else retries
    retry_base_sec = settings.batch_retry_base_sec if retry_base_sec is None else retry_base_sec
    cancel_event = cancel_event or asyncio.Event()
    sem = asyncio.Semaphore(max(1, concurrency))

    # счётчики начинаются с нуля на каждый запуск (возобновление = новый проход с пропуском готовых)
    state = {"processed": 0, "succeeded": 0, "partial": 0, "failed": 0, "skipped": 0, "llm_calls": 0}
    cost = Decimal(0)
    errors: list[dict] = []
    current: list[str] = []
    db.update_batch_job(job_id, path=db_path, status="running", finished_at=None, errors=[], current=[], cost="0", **state)

    def flush():
        # status=running при каждом сохранении: если сервер при старте пометил задание прерванным, а оно
        # выполняется в другом процессе (CLI), статус восстанавливается
        db.update_batch_job(job_id, path=db_path, status="running", cost=str(cost), errors=errors[-50:],
                            current=list(current), **state)

    async def one(cid: str):
        nonlocal cost
        async with sem:
            if cancel_event.is_set():
                return
            conv = db.get_conversation(job["group_id"], cid, path=db_path)
            if conv is None:
                state["failed"] += 1
                errors.append({"conversation_id": cid, "error": "диалог не найден"})
            elif job["skip_evaluated"] and is_up_to_date(job["group_id"], cid, checklist, db_path,
                                                         not_before=job["created_at"] if job["full"] else None):
                state["skipped"] += 1
            else:
                current.append(cid)
                flush()
                attempt = 0
                while True:
                    try:
                        out = await evaluate_conversation(conv, checklist, provider, use_cache=not job["full"], db_path=db_path)
                        rec = out.record
                    except Exception as e:  # баг или неожиданная ошибка — не роняем весь пакет
                        log.exception("batch %s: %s", job_id, cid)
                        rec = {"status": "error", "llm_error": f"{type(e).__name__}: {e}", "cost": "0", "llm_calls": 0}
                    cost += Decimal(rec.get("cost", "0"))
                    state["llm_calls"] += rec.get("llm_calls", 0)
                    if rec["status"] != "error" or attempt >= retries or cancel_event.is_set():
                        break
                    delay = retry_base_sec * (2 ** attempt)
                    attempt += 1
                    log.warning("batch %s: %s недоступна модель (%s), повтор %s через %.0f с",
                                job_id, cid, rec.get("llm_error"), attempt, delay)
                    await asyncio.sleep(delay)
                if rec["status"] == "done":
                    state["succeeded"] += 1
                elif rec["status"] == "partial":
                    state["partial"] += 1
                else:
                    state["failed"] += 1
                    errors.append({"conversation_id": cid, "error": rec.get("llm_error") or "ошибка оценки"})
                current.remove(cid)
            state["processed"] += 1
            flush()

    await asyncio.gather(*(one(cid) for cid in job["conversation_ids"]))
    final = "cancelled" if cancel_event.is_set() else "done"
    db.update_batch_job(job_id, path=db_path, status=final, finished_at=db.now_iso(), cost=str(cost),
                        errors=errors[-50:], current=[], **state)
    return db.get_batch_job(job_id, path=db_path)


class BatchManager:
    """Фоновые задания внутри процесса FastAPI. Одно активное задание на набор."""

    def __init__(self, provider_factory: Callable[[], LLMProvider] = get_provider, db_path: Path | None = None):
        self.provider_factory = provider_factory
        self.db_path = db_path
        self.tasks: dict[int, asyncio.Task] = {}
        self.cancels: dict[int, asyncio.Event] = {}

    def active_for(self, group_id: str) -> int | None:
        for job_id, t in self.tasks.items():
            if not t.done():
                job = db.get_batch_job(job_id, path=self.db_path)
                if job and job["group_id"] == group_id:
                    return job_id
        return None

    def start(self, job_id: int) -> None:
        cancel = asyncio.Event()
        self.cancels[job_id] = cancel
        provider = self.provider_factory()
        self.tasks[job_id] = asyncio.create_task(run_job(job_id, provider, db_path=self.db_path, cancel_event=cancel))

    def submit(self, group_id: str, checklist: Checklist, conversation_ids: list[str], *, full: bool = False,
               skip_evaluated: bool = True) -> int:
        job_id = db.create_batch_job(group_id, checklist.id, checklist.version, conversation_ids, full, skip_evaluated,
                                     path=self.db_path)
        self.start(job_id)
        return job_id

    def resume(self, job_id: int) -> None:
        """Продолжить: тот же список; готовые этой версией (для полной — после старта задания) пропускаются."""
        db.update_batch_job(job_id, path=self.db_path, status="queued", skip_evaluated=1)
        self.start(job_id)

    def cancel(self, job_id: int) -> bool:
        ev = self.cancels.get(job_id)
        if ev is None:
            return False
        ev.set()
        return True
