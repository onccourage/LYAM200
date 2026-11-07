"""Оценить несколько диалогов и вывести результат в консоль.

python scripts/run_eval.py DEV                      # по одному диалогу каждого (канал, язык), до 5
python scripts/run_eval.py DEV p1-ru-001 p1-kz-003  # конкретные
python scripts/run_eval.py DEV --limit 3 --full     # без кэша критериев
python scripts/run_eval.py DEV p1-ru-001 --dry-run  # только показать промпт, без вызова LLM
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.evaluator import evaluate_conversation  # noqa: E402
from app.llm import get_provider  # noqa: E402
from app.preprocess import preprocess  # noqa: E402
from app.prompt_builder import build_request  # noqa: E402
from app.scoring import applies_to_channel  # noqa: E402


def pick(convs, ids, limit):
    if ids:
        by_id = {c.conversation_id: c for c in convs}
        missing = [i for i in ids if i not in by_id]
        if missing:
            sys.exit(f"нет таких диалогов: {missing}")
        return [by_id[i] for i in ids]
    seen, out = set(), []
    for c in convs:  # разнообразие: по одному на (канал, язык)
        k = (c.channel, c.language)
        if k not in seen:
            seen.add(k)
            out.append(c)
    for c in convs:
        if len(out) >= limit:
            break
        if c not in out:
            out.append(c)
    return out[:limit]


def show(outcome, checklist):
    r = outcome.record
    s = r["score"] or {}
    print(f"\n=== {r['conversation_id']}  итог: {r['total'] if r['total'] is not None else 'не рассчитывается'}"
          f"{'  ⚠ ГРУБАЯ ОШИБКА ' + ','.join(s.get('gross_types', [])) if r['gross_error'] else ''}"
          f"  [{r['status']}]  {checklist.id} v{checklist.version}  {r['model']}")
    print(f"    вызовов LLM: {r['llm_calls']}, токены in/out: {r['tokens_in']}/{r['tokens_out']}, "
          f"стоимость ${r['cost']}; из кэша: {outcome.cached_criteria or '—'}")
    rows = {x["criterion_id"]: x for x in s.get("criteria", [])}
    for v in r["answers"]:
        cid = v["criterion_id"]
        sc = rows.get(cid, {})
        pts = sc.get("points")
        ew = sc.get("effective_weight_display")
        ans = v["answer"] if v["status"] == "ok" else "NEEDS_REVIEW"
        print(f"  {cid:5} {ans:>12}  баллы={pts if pts is not None else '-':>6}  эфф.вес={ew or '-':>6}  "
              f"conf={v.get('confidence')}  [{v['source']}]")
        if v.get("rationale"):
            print(f"        {v['rationale']}")
        for e in v.get("evidence", []):
            print(f"        ↳ [{e['message_id']} {e.get('timestamp')} {e.get('speaker')}] «{e['quote']}»")
        if v.get("checked_range"):
            print(f"        ↳ проверено {v['checked_range']['from']}–{v['checked_range']['to']}")
        if v.get("na_reason"):
            print(f"        ↳ N/A: {v['na_reason']}")
        if v.get("review_reason"):
            print(f"        ↳ {v['review_reason']}: {v.get('validation_errors')}")
    for row in s.get("criteria", []):
        if row["status"] == "na_channel":
            print(f"  {row['criterion_id']:5} {'na':>12}  ({row['na_reason']})")
    for sec in s.get("sections", []):
        print(f"  секция {sec['section_id']}: {sec['points']} / {sec['applicable_weight']} → {sec['pct']}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("group_id")
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--checklist", default="default_chat")
    ap.add_argument("--full", action="store_true", help="игнорировать кэш критериев")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    db.init_db()
    db.seed_checklists()
    convs = db.list_conversations(a.group_id)
    if not convs:
        sys.exit(f"набор {a.group_id} не загружен: make fetch GROUP={a.group_id}")
    checklist = db.get_checklist(a.checklist)
    targets = pick(convs, a.ids, a.limit)

    if a.dry_run:
        c = targets[0]
        crit = [x for x in checklist.enabled_criteria if applies_to_channel(x, c.channel) and not x.requires_audio]
        req = build_request(preprocess(c), crit)
        print(req["system"], "\n\n----- USER -----\n", req["user"], "\n\n----- SCHEMA -----\n",
              json.dumps(req["schema"], ensure_ascii=False)[:2000])
        return

    if settings.llm_provider == "anthropic" and not settings.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY не задан в .env")
    provider = get_provider()
    total_cost = 0.0
    for c in targets:
        out = await evaluate_conversation(c, checklist, provider, use_cache=not a.full)
        show(out, checklist)
        total_cost += float(out.record["cost"])
    print(f"\nВсего: {len(targets)} диалогов, стоимость ${total_cost:.4f}, средняя ${total_cost / len(targets):.4f}")


if __name__ == "__main__":
    asyncio.run(main())
