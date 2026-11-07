"""Эталонная разметка и согласие модели с ней.

Формат файла data/gold/{conversation_id}.json:
{
  "conversation_id": "p1-ru-001", "group_id": "DEV", "annotator": "Аня",
  "checklist_id": "default_chat",
  "answers": {"c01": "yes", "c04": 15, "c03": "na", "c08": {"answer": "yes", "gross_types": ["insult"]}},
  "notes": {"c04": "свободный комментарий"}           # необязательно
}
Ответ null / отсутствует — критерий не размечен (в метриках не участвует).

Метрики по критерию: доля совпадений, каппа Коэна (binary/flag и N/A — категориально), для шкалы —
средняя абсолютная ошибка ступени (по парам без N/A) и доля совпадений N/A; совпадение флага.
Итог: средняя абсолютная разница итогового балла (эталонный итог считается тем же scoring.py).
"""
from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from .models import Checklist, Conversation
from .scoring import ScoringError, applies_to_channel, normalize_answer, score


def load_gold(gold_dir: Path) -> list[dict]:
    out = []
    for f in sorted(gold_dir.glob("*.json")):
        if f.name.startswith("_") or f.name.endswith("report.json"):
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        if "conversation_id" in d and "answers" in d:
            d["_file"] = f.name
            out.append(d)
    return out


def _norm(c, raw) -> tuple[str | None, list[str], str | None]:
    """→ (метка, gross_types, ошибка). Метка: yes/no/na или ступень строкой."""
    gross: list[str] = []
    if isinstance(raw, dict):
        gross = list(raw.get("gross_types") or [])
        raw = raw.get("answer")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, [], None
    try:
        kind, value = normalize_answer(c, raw)
    except ValueError as e:
        return None, [], str(e)
    if kind == "scale":
        v = value.normalize()
        return (f"{v:f}" if v == v.to_integral_value() else str(v)), [], None
    return kind, gross if kind == "yes" else [], None


def cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Каппа Коэна для категориальных меток. None — если не определена (нет пар)."""
    n = len(pairs)
    if not n:
        return None
    agree = sum(1 for a, b in pairs if a == b)
    ca, cb = defaultdict(int), defaultdict(int)
    for a, b in pairs:
        ca[a] += 1
        cb[b] += 1
    expected = sum(ca[k] * cb[k] for k in set(ca) | set(cb))   # pe * n²
    if expected == n * n:  # обе стороны всегда ставили одну и ту же метку
        return 1.0 if agree == n else 0.0
    po, pe = agree / n, expected / (n * n)
    return round((po - pe) / (1 - pe), 4)


def _metrics(c, pairs: list[tuple[str, str]], gross_pairs: list[tuple[list, list]]) -> dict:
    n = len(pairs)
    m: dict = {"n": n, "agreement": round(sum(a == b for a, b in pairs) / n, 4) if n else None}
    if c.type == "scale":
        num = [(Decimal(a), Decimal(b)) for a, b in pairs if a != "na" and b != "na"]
        m["mae_step"] = round(float(sum(abs(a - b) for a, b in num) / len(num)), 4) if num else None
        m["scale_max"] = float(c.scale_max)
        na_pairs = [(a == "na", b == "na") for a, b in pairs]
        m["na_agreement"] = round(sum(x == y for x, y in na_pairs) / n, 4) if n else None
        m["kappa_na"] = cohen_kappa([(str(x), str(y)) for x, y in na_pairs]) if c.na_allowed else None
    else:
        m["kappa"] = cohen_kappa(pairs)
    if c.type == "flag":
        both = [(set(g), set(mo)) for g, mo in gross_pairs]
        m["gross_types_match"] = round(sum(g == mo for g, mo in both) / len(both), 4) if both else None
    # матрица ошибок: gold → model
    conf: dict = defaultdict(int)
    for a, b in pairs:
        conf[f"{a}→{b}"] += 1
    m["confusion"] = dict(sorted(conf.items()))
    return m


def compare(gold: list[dict], evaluations: dict[str, dict], conversations: dict[str, Conversation],
            checklists: dict[tuple[str, int], Checklist], default_checklist: Checklist) -> dict:
    """gold — список файлов; evaluations — текущие оценки по conversation_id (с answers и checklist_id/version)."""
    per_crit: dict[str, list] = defaultdict(list)          # cid → [(gold, model, lang, channel)]
    per_crit_gross: dict[str, list] = defaultdict(list)
    totals: list[dict] = []
    issues: list[str] = []
    missing_eval, unlabeled, needs_review = [], 0, defaultdict(int)

    for g in gold:
        cid_conv = g["conversation_id"]
        conv = conversations.get(cid_conv)
        ev = evaluations.get(cid_conv)
        if conv is None:
            issues.append(f"{g['_file']}: диалог {cid_conv} не найден в наборе")
            continue
        if ev is None:
            missing_eval.append(cid_conv)
            continue
        cl = checklists.get((ev["checklist_id"], ev["checklist_version"]), default_checklist)
        model = {v["criterion_id"]: v for v in ev["answers"]}
        gold_answers: dict[str, dict] = {}
        for c in cl.enabled_criteria:
            if not applies_to_channel(c, conv.channel):
                continue
            label, gross, err = _norm(c, g["answers"].get(c.id))
            if err:
                issues.append(f"{g['_file']}: {err}")
                continue
            if label is None:
                unlabeled += 1
                continue
            gold_answers[c.id] = {"answer": label, "gross_types": gross}
            mv = model.get(c.id)
            if mv is None or mv.get("status") != "ok" or mv.get("answer") is None:
                needs_review[c.id] += 1
                continue
            per_crit[c.id].append((label, str(mv["answer"]), conv.language or "?", conv.channel))
            if c.type == "flag" and label == "yes" and mv["answer"] == "yes":
                per_crit_gross[c.id].append((gross, mv.get("gross_types") or []))
        # итог по эталону — тем же scoring.py (если размечены все применимые критерии)
        try:
            gold_score = score(cl, gold_answers, channel=conv.channel)
            gt = gold_score.total
        except ScoringError:
            gt = None
        flags = [c for c in cl.enabled_criteria if c.type == "flag" and applies_to_channel(c, conv.channel)]
        gold_gross = (any(gold_answers[c.id]["answer"] == "yes" for c in flags)
                      if flags and all(c.id in gold_answers for c in flags) else None)
        mt = Decimal(ev["total"]) if ev.get("total") is not None else None
        totals.append({"conversation_id": cid_conv, "language": conv.language, "channel": conv.channel,
                       "gold_total": str(gt) if gt is not None else None, "model_total": str(mt) if mt is not None else None,
                       "abs_diff": float(abs(gt - mt)) if gt is not None and mt is not None else None,
                       "gold_gross": gold_gross,
                       "model_gross": ev.get("gross_error")})

    crit_meta = {c.id: c for c in default_checklist.criteria}
    for cl in checklists.values():
        for c in cl.criteria:
            crit_meta.setdefault(c.id, c)

    criteria = {}
    for cid, rows in sorted(per_crit.items()):
        c = crit_meta[cid]
        pairs = [(a, b) for a, b, _, _ in rows]
        m = _metrics(c, pairs, per_crit_gross.get(cid, []))
        by_lang = {}
        for lang in sorted({r[2] for r in rows}):
            lp = [(a, b) for a, b, l, _ in rows if l == lang]
            by_lang[lang] = {"n": len(lp), "agreement": round(sum(a == b for a, b in lp) / len(lp), 4)}
        by_channel = {}
        for ch in sorted({r[3] for r in rows}):
            cp = [(a, b) for a, b, _, x in rows if x == ch]
            by_channel[ch] = {"n": len(cp), "agreement": round(sum(a == b for a, b in cp) / len(cp), 4)}
        criteria[cid] = {"title": c.title, "type": c.type, **m, "needs_review": needs_review.get(cid, 0),
                         "by_language": by_lang, "by_channel": by_channel}

    all_pairs = [(a, b, l) for rows in per_crit.values() for a, b, l, _ in rows]
    by_language = {}
    for lang in sorted({l for _, _, l in all_pairs}):
        lp = [(a, b) for a, b, l in all_pairs if l == lang]
        lt = [t["abs_diff"] for t in totals if t["language"] == lang and t["abs_diff"] is not None]
        by_language[lang] = {"pairs": len(lp), "agreement": round(sum(a == b for a, b in lp) / len(lp), 4),
                             "dialogs": len([t for t in totals if t["language"] == lang]),
                             "total_mae": round(sum(lt) / len(lt), 2) if lt else None}
    diffs = [t["abs_diff"] for t in totals if t["abs_diff"] is not None]
    gross_rows = [t for t in totals if t["gold_gross"] is not None]
    return {
        "dialogs": len(totals),
        "pairs": len(all_pairs),
        "overall_agreement": round(sum(a == b for a, b, _ in all_pairs) / len(all_pairs), 4) if all_pairs else None,
        "total_mae": round(sum(diffs) / len(diffs), 2) if diffs else None,
        "total_within_5": round(sum(d <= 5 for d in diffs) / len(diffs), 4) if diffs else None,
        "gross_flag_agreement": round(sum(t["gold_gross"] == t["model_gross"] for t in gross_rows) / len(gross_rows), 4) if gross_rows else None,
        "criteria": criteria,
        "by_language": by_language,
        "totals": totals,
        "missing_evaluations": missing_eval,
        "unlabeled_answers": unlabeled,
        "issues": issues,
    }


def suggest(conversations: list[Conversation], n: int = 18) -> list[Conversation]:
    """Подобрать разнообразный набор для разметки: все пары (канал, язык), затем поровну по языкам."""
    from .preprocess import detect_client_language
    buckets: dict[tuple, list] = defaultdict(list)
    for c in conversations:
        mixed = detect_client_language(c.messages)["detected"] == "KZ+RU"
        buckets[(c.channel, c.language, mixed)].append(c)
    picked: list[Conversation] = []
    keys = sorted(buckets, key=lambda k: (k[0] != "voice call", k))
    while len(picked) < n and any(buckets.values()):
        for k in keys:
            if buckets[k] and len(picked) < n:
                # длинные диалоги информативнее (больше критериев применимо)
                buckets[k].sort(key=lambda c: -len(c.messages))
                picked.append(buckets[k].pop(0))
    return picked
