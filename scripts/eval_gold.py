"""Согласие модели с эталонной разметкой: python scripts/eval_gold.py DEV [--evaluate] [--json out.json]

Берёт текущие оценки диалогов из БД (той версии чек-листа, которой они оценены). --evaluate сначала оценит
размеченные диалоги, у которых оценки нет. Печатает таблицу по критериям и языкам и пишет JSON для слайда.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.gold import compare, load_gold  # noqa: E402


def pct(x):
    return "—" if x is None else f"{x * 100:.0f}%"


def num(x, d=2):
    return "—" if x is None else f"{x:.{d}f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("group_id")
    ap.add_argument("--gold-dir", default=str(settings.data_dir / "gold"))
    ap.add_argument("--checklist", default="default_chat")
    ap.add_argument("--evaluate", action="store_true", help="оценить размеченные диалоги без оценки")
    ap.add_argument("--json", default=None, help="куда записать отчёт (по умолчанию data/gold/_report.json)")
    a = ap.parse_args()

    db.init_db()
    db.seed_checklists()
    gold = [g for g in load_gold(Path(a.gold_dir)) if g.get("group_id", a.group_id) == a.group_id]
    if not gold:
        sys.exit(f"нет эталонных файлов для {a.group_id} в {a.gold_dir}: python scripts/gold_template.py {a.group_id}")
    convs = {c.conversation_id: c for c in db.list_conversations(a.group_id)}
    default_cl = db.get_checklist(a.checklist)

    if a.evaluate:
        from app.evaluator import evaluate_conversation
        from app.llm import get_provider
        provider = get_provider()
        todo = [g["conversation_id"] for g in gold if g["conversation_id"] in convs
                and db.get_current_evaluation(a.group_id, g["conversation_id"]) is None]

        async def run():
            for cid in todo:
                out = await evaluate_conversation(convs[cid], default_cl, provider)
                print(f"  оценён {cid}: {out.record['total']} (${out.record['cost']})")
        asyncio.run(run())

    evals, cls = {}, {}
    for g in gold:
        ev = db.get_current_evaluation(a.group_id, g["conversation_id"])
        if ev:
            evals[g["conversation_id"]] = ev
            key = (ev["checklist_id"], ev["checklist_version"])
            if key not in cls:
                cls[key] = db.get_checklist(*key)
    r = compare(gold, evals, convs, cls, default_cl)

    print(f"\nЭталон: {len(gold)} файлов, сравнено диалогов {r['dialogs']}, пар ответов {r['pairs']}")
    print(f"Совпадение ответов: {pct(r['overall_agreement'])} · MAE итога: {num(r['total_mae'])} б. · "
          f"итог в пределах 5 б.: {pct(r['total_within_5'])} · совпадение флага грубой ошибки: {pct(r['gross_flag_agreement'])}\n")
    head = f"{'крит.':6}{'тип':7}{'n':>4} {'совп.':>6} {'каппа':>6} {'MAE ступ.':>10} {'N/A совп.':>10} {'needs_rev':>9}  по языкам"
    print(head)
    print("-" * len(head))
    for cid, m in r["criteria"].items():
        langs = "  ".join(f"{l}:{pct(v['agreement'])}({v['n']})" for l, v in m["by_language"].items())
        mae = f"{num(m.get('mae_step'))}/{m['scale_max']:g}" if m["type"] == "scale" else "—"
        print(f"{cid:6}{m['type']:7}{m['n']:>4} {pct(m['agreement']):>6} {num(m.get('kappa')):>6} {mae:>10} "
              f"{pct(m.get('na_agreement')):>10} {m['needs_review']:>9}  {langs}")
    print("\nПо языкам:")
    for lang, v in r["by_language"].items():
        print(f"  {lang:6} диалогов {v['dialogs']:>3}  совпадение {pct(v['agreement']):>5}  MAE итога {num(v['total_mae'])}")
    worst = sorted((m["agreement"], cid) for cid, m in r["criteria"].items() if m["agreement"] is not None and m["agreement"] < 1)[:3]
    if worst:
        print("\nСлабые места (ниже всего совпадение):")
        for agr, cid in worst:
            conf = {k: v for k, v in r["criteria"][cid]["confusion"].items() if k.split("→")[0] != k.split("→")[1]}
            print(f"  {cid} {pct(agr)} — ошибки эталон→модель: {conf}")
    if r["missing_evaluations"]:
        print(f"\nНет оценки (запустите с --evaluate): {r['missing_evaluations']}")
    if r["issues"]:
        print("\nПроблемы в разметке:")
        for i in r["issues"]:
            print(f"  ! {i}")
    out = Path(a.json) if a.json else Path(a.gold_dir) / "_report.json"
    out.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nJSON: {out}")


if __name__ == "__main__":
    main()
