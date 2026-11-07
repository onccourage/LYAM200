"""Аналитика по набору (раздел 7.5): все числа считает бэкенд, фронтенд только рисует.

Источник — текущие оценки диалогов (с учётом решений по апелляциям). Если набор оценён разными
версиями чек-листа, критерии сводятся по стабильному ID; в ответе указано, какие версии участвуют.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from . import db

BUCKETS = [(0, 50), (50, 60), (60, 70), (70, 80), (80, 90), (90, 100.01)]


def _pct(part: int | Decimal, whole: int | Decimal) -> str | None:
    if not whole:
        return None
    return str((Decimal(part) / Decimal(whole) * 100).quantize(Decimal("0.1"), ROUND_HALF_UP))


def _avg(values: list[Decimal], digits: str = "0.01") -> str | None:
    if not values:
        return None
    return str((sum(values, Decimal(0)) / len(values)).quantize(Decimal(digits), ROUND_HALF_UP))


def compute(group_id: str, checklist_id: str | None = None, db_path: Path | None = None) -> dict:
    rows = db.list_current_evaluations(group_id, path=db_path)
    if checklist_id:
        rows = [r for r in rows if r["checklist_id"] == checklist_id]
    evals = [db.get_evaluation(r["id"], path=db_path) for r in rows]
    convs = {c.conversation_id: c for c in db.list_conversations(group_id, path=db_path)}

    # метаданные критериев — из самой свежей версии чек-листа среди оценок
    versions = Counter((e["checklist_id"], e["checklist_version"]) for e in evals)
    meta: dict[str, dict] = {}
    order: list[str] = []
    flag_labels: dict[str, str] = {}
    for (cid, ver) in sorted(versions, key=lambda k: -k[1]):
        cl = db.get_checklist(cid, ver, path=db_path)
        if cl is None:
            continue
        for c in cl.criteria:
            if c.id not in meta:
                meta[c.id] = {"title": c.title, "code": c.code or c.id, "type": c.type,
                              "scale_max": str(c.scale_max) if c.scale_max is not None else None}
                order.append(c.id)
            for f in c.flag_types:
                flag_labels.setdefault(f.id, f.label or f.id)

    # --- по критериям
    stats: dict[str, dict] = defaultdict(lambda: {"yes": 0, "no": 0, "na": 0, "pending": 0, "steps": Counter(),
                                                  "pct_points": [], "low_conf": 0})
    for e in evals:
        rows_by_id = {r["criterion_id"]: r for r in (e["score"] or {}).get("criteria", [])}
        for v in e["answers"]:
            s = stats[v["criterion_id"]]
            if v.get("status") != "ok" or v.get("answer") is None:
                s["pending"] += 1
                continue
            a = v["answer"]
            if a in ("yes", "no", "na"):
                s[a] += 1
            else:
                s["steps"][a] += 1
            if v.get("confidence") is not None and v["confidence"] < 0.7:
                s["low_conf"] += 1
            row = rows_by_id.get(v["criterion_id"])
            if row and row.get("status") == "scored" and row.get("max_points") not in (None, "0"):
                s["pct_points"].append(Decimal(row["points"]) / Decimal(row["max_points"]) * 100)

    criteria = []
    for cid in order:
        if cid not in stats:
            continue
        s, m = stats[cid], meta[cid]
        if m["type"] == "scale":
            scored = sum(s["steps"].values())
            top = Decimal(m["scale_max"]) if m["scale_max"] else None
            below = sum(n for step, n in s["steps"].items() if top is None or Decimal(step) < top)
            problem, base, label = below, scored, "не максимум"
        elif m["type"] == "flag":
            problem, base, label = s["yes"], s["yes"] + s["no"], "нарушение"
        else:
            problem, base, label = s["no"], s["yes"] + s["no"], "«Нет»"
        evaluated = base + s["na"] + s["pending"]
        criteria.append({
            "criterion_id": cid, "code": m["code"], "title": m["title"], "type": m["type"],
            "problem_label": label, "problem": problem, "applicable": base, "problem_rate": _pct(problem, base),
            "na": s["na"], "na_rate": _pct(s["na"], evaluated), "needs_review": s["pending"],
            "low_confidence": s["low_conf"], "avg_points_pct": _avg(s["pct_points"], "0.1"),
            "steps": {k: s["steps"][k] for k in sorted(s["steps"], key=lambda x: -Decimal(x))},
            "yes": s["yes"], "no": s["no"],
        })

    # --- срезы по языку и каналу
    def slice_by(key) -> list[dict]:
        groups: dict[str, list] = defaultdict(list)
        for e in evals:
            c = convs.get(e["conversation_id"])
            groups[key(c) if c else "?"].append(e)
        out = []
        for k in sorted(groups):
            es = groups[k]
            totals = [Decimal(e["total"]) for e in es if e["total"] is not None]
            out.append({"key": k, "dialogs": len(es), "avg_total": _avg(totals),
                        "min_total": str(min(totals)) if totals else None, "max_total": str(max(totals)) if totals else None,
                        "gross": sum(1 for e in es if e["gross_error"]), "gross_rate": _pct(sum(1 for e in es if e["gross_error"]), len(es)),
                        "needs_review": sum(1 for e in es if e["status"] == "partial")})
        return out

    # --- распределение итогов
    totals = [Decimal(e["total"]) for e in evals if e["total"] is not None]
    histogram = []
    for lo, hi in BUCKETS:
        n = sum(1 for t in totals if Decimal(str(lo)) <= t < Decimal(str(hi)))
        histogram.append({"label": f"{lo}–{min(int(hi), 100)}" if lo else f"<{hi}", "count": n, "share": _pct(n, len(totals))})
    not_calc = sum(1 for e in evals if e["total"] is None)

    # --- грубые ошибки
    gross_types = Counter(g for e in evals for g in ((e["score"] or {}).get("gross_types") or []))
    gross_n = sum(1 for e in evals if e["gross_error"])

    # --- апелляции (удовлетворённые = ошибки модели, подтверждённые проверяющим)
    eval_ids = {e["id"] for e in evals}
    appeals = [a for a in db.list_appeals(group_id=group_id, path=db_path)
               if not checklist_id or a["evaluation_id"] in eval_ids]
    resolved = [a for a in appeals if a["status"] == "resolved"]
    changed = [a for a in resolved if a["decision"] == "changed"]
    by_crit: dict[str, dict] = defaultdict(lambda: {"resolved": 0, "changed": 0})
    for a in resolved:
        by_crit[a["criterion_id"]]["resolved"] += 1
        by_crit[a["criterion_id"]]["changed"] += a["decision"] == "changed"
    shifts = [Decimal(a["final_total"]) - Decimal(a["original_total"]) for a in changed
              if a.get("final_total") is not None and a.get("original_total") is not None]

    stats_cost = db.dataset_stats(group_id, path=db_path)
    return {
        "group_id": group_id,
        "dialogs_total": len(convs),
        "evaluated": len(evals),
        "checklist_versions": [{"checklist_id": k[0], "version": k[1], "dialogs": n} for k, n in sorted(versions.items())],
        "summary": {
            "avg_total": _avg(totals), "median_total": str(sorted(totals)[len(totals) // 2]) if totals else None,
            "not_calculated": not_calc,
            "gross_dialogs": gross_n, "gross_rate": _pct(gross_n, len(evals)),
            "needs_review_dialogs": sum(1 for e in evals if e["status"] == "partial"),
        },
        "criteria": criteria,
        "by_language": slice_by(lambda c: c.language or "?"),
        "by_channel": slice_by(lambda c: c.channel),
        "histogram": histogram,
        "gross": {"dialogs": gross_n, "rate": _pct(gross_n, len(evals)),
                  "by_type": [{"type": t, "label": flag_labels.get(t, t), "count": n} for t, n in gross_types.most_common()]},
        "appeals": {
            "total": len(appeals), "open": len(appeals) - len(resolved), "resolved": len(resolved),
            "changed": len(changed), "confirmed": len(resolved) - len(changed),
            "changed_rate": _pct(len(changed), len(resolved)),
            "avg_total_shift": _avg(shifts),
            "by_criterion": [{"criterion_id": k, "title": meta.get(k, {}).get("title", k), **v,
                              "changed_rate": _pct(v["changed"], v["resolved"])} for k, v in sorted(by_crit.items())],
        },
        "cost": {k: stats_cost[k] for k in ("spent", "avg_cost_per_conversation", "avg_cost_per_llm_evaluation",
                                             "avg_tokens_in", "avg_tokens_out", "evaluations_total")},
    }
