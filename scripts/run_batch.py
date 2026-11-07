"""Пакетная оценка из консоли (без сервера): python scripts/run_batch.py DEV [--full] [--ids a b c]

Возобновляемо: повторный запуск пропускает диалоги, уже оценённые этой версией чек-листа.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.batch import run_job  # noqa: E402
from app.config import settings  # noqa: E402
from app.llm import get_provider  # noqa: E402


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("group_id")
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--checklist", default="default_chat")
    ap.add_argument("--version", type=int)
    ap.add_argument("--full", action="store_true", help="сбросить кэш критериев")
    ap.add_argument("--concurrency", type=int)
    ap.add_argument("--samples", type=int, default=1, help="консенсус: число проходов модели на диалог")
    a = ap.parse_args()

    db.init_db()
    db.seed_checklists()
    if settings.llm_provider == "anthropic" and not settings.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY не задан в .env")
    cl = db.get_checklist(a.checklist, a.version)
    ids = a.ids or [c.conversation_id for c in db.list_conversations(a.group_id)]
    if not ids:
        sys.exit(f"набор {a.group_id} не загружен: make fetch GROUP={a.group_id}")
    job_id = db.create_batch_job(a.group_id, cl.id, cl.version, ids, a.full, True, samples=a.samples)
    print(f"Задание #{job_id}: {len(ids)} диалогов, {cl.id} v{cl.version}, параллельно {a.concurrency or settings.batch_concurrency}")

    task = asyncio.create_task(run_job(job_id, get_provider(), concurrency=a.concurrency))
    while not task.done():
        await asyncio.sleep(2)
        j = db.get_batch_job(job_id)
        print(f"\r  {j['processed']}/{j['total']}  ok={j['succeeded']} partial={j['partial']} "
              f"failed={j['failed']} skipped={j['skipped']}  ${float(j['cost']):.4f}   ", end="", flush=True)
    j = task.result()
    print(f"\nГотово: {j['status']}. Вызовов LLM: {j['llm_calls']}, стоимость ${float(j['cost']):.4f}")
    for e in j["errors"]:
        print(f"  ! {e['conversation_id']}: {e['error']}")
    s = db.dataset_stats(a.group_id)
    print(f"Набор: оценено {s['evaluated']}/{s['conversations']}, средний итог {s['avg_total']}, "
          f"средняя стоимость диалога ${s['avg_cost_per_conversation']}, всего потрачено ${s['spent']}")


if __name__ == "__main__":
    asyncio.run(main())
