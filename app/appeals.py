"""Апелляции: подача контролёром, решение проверяющего, пересчёт итога БЕЗ вызова LLM.

- Одна открытая апелляция на критерий оценки; комментарий обязателен.
- Решение: confirmed (оценка остаётся) или changed (новое значение — только допустимое для типа критерия,
  N/A — только если разрешён) + обязательная причина.
- После решения ответы оценки обновляются, итог пересчитывается scoring.score().
  Исходный ответ модели хранится в апелляции и в вердикте (original_answer), исходный итог — в
  evaluations.original_total. История: кто/когда/было/стало/почему — в таблице appeals.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db
from .models import Checklist
from .scoring import GROSS_TYPES, ScoringError, normalize_answer, score


class AppealError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _fmt_answer(kind: str, value) -> str:
    if kind == "scale":
        v = value.normalize()
        return f"{v:f}" if v == v.to_integral_value() else str(v)
    return kind


def _context(evaluation_id: int, criterion_id: str, path: Path | None):
    ev = db.get_evaluation(evaluation_id, path=path)
    if ev is None:
        raise AppealError("Оценка не найдена", 404)
    cl = db.get_checklist(ev["checklist_id"], ev["checklist_version"], path=path)
    crit = cl.criterion(criterion_id) if cl else None
    if crit is None or not crit.enabled:
        raise AppealError(f"Критерия {criterion_id} нет в чек-листе этой оценки", 404)
    verdict = next((v for v in ev["answers"] if v["criterion_id"] == criterion_id), None)
    row = next((r for r in (ev["score"] or {}).get("criteria", []) if r["criterion_id"] == criterion_id), None)
    return ev, cl, crit, verdict, row


def create_appeal(evaluation_id: int, criterion_id: str, comment: str, submitter_label: str | None = None,
                  path: Path | None = None) -> dict:
    comment = (comment or "").strip()
    if not comment:
        raise AppealError("Комментарий к апелляции обязателен")
    ev, cl, crit, verdict, row = _context(evaluation_id, criterion_id, path)
    if not ev["is_current"]:
        raise AppealError("Эта оценка устарела — подайте апелляцию на текущую оценку диалога", 409)
    if verdict is None:
        reason = row.get("na_reason") if row else None
        raise AppealError(f"Критерий не оценивался для этого диалога{': ' + reason if reason else ''}", 409)
    try:
        appeal_id = db.insert_appeal({
            "evaluation_id": evaluation_id, "group_id": ev["group_id"], "conversation_id": ev["conversation_id"],
            "criterion_id": criterion_id, "comment": comment, "submitter_label": (submitter_label or "").strip() or None,
            "original_answer": verdict["answer"] if verdict.get("status") == "ok" else None,
            "original_points": (row or {}).get("points_display"),
            "original_total": ev["total"],
        }, path=path)
    except ValueError as e:
        raise AppealError(str(e), 409) from e
    return db.get_appeal(appeal_id, path=path)


def _rescore(cl: Checklist, answers: list[dict], channel: str):
    amap = {v["criterion_id"]: {"answer": v["answer"] if v.get("status") == "ok" else None,
                                "gross_types": v.get("gross_types") or []} for v in answers}
    return score(cl, amap, channel=channel)


def resolve_appeal(appeal_id: int, decision: str, reason: str, reviewer_label: str | None = None,
                   new_answer=None, new_gross_types: list[str] | None = None, path: Path | None = None) -> dict:
    appeal = db.get_appeal(appeal_id, path=path)
    if appeal is None:
        raise AppealError("Апелляция не найдена", 404)
    if appeal["status"] != "open":
        raise AppealError("Апелляция уже рассмотрена", 409)
    reason = (reason or "").strip()
    if not reason:
        raise AppealError("Причина решения обязательна")
    if decision not in ("confirmed", "changed"):
        raise AppealError("Решение: confirmed или changed")

    ev, cl, crit, verdict, _ = _context(appeal["evaluation_id"], appeal["criterion_id"], path)
    conv = db.get_conversation(ev["group_id"], ev["conversation_id"], path=path)
    original = verdict["answer"] if verdict.get("status") == "ok" else None

    if decision == "confirmed":
        if original is None:
            raise AppealError("Оценка модели не прошла проверку (needs_review) — подтверждать нечего, выберите «Изменить»")
        final_answer, gross = original, verdict.get("gross_types") or []
    else:
        if new_answer is None or str(new_answer).strip() == "":
            raise AppealError("Укажите новое значение")
        try:
            kind, value = normalize_answer(crit, new_answer)
        except ValueError as e:
            raise AppealError(str(e)) from e
        final_answer = _fmt_answer(kind, value)
        gross = []
        if crit.type == "flag" and final_answer == "yes":
            gross = list(dict.fromkeys(new_gross_types or verdict.get("gross_types") or []))
            bad = [g for g in gross if g not in GROSS_TYPES]
            if bad or not gross:
                raise AppealError(f"Для флага «Да» укажите тип нарушения: {', '.join(GROSS_TYPES)}")
        if final_answer == original and gross == (verdict.get("gross_types") or []):
            raise AppealError("Новое значение совпадает с текущим — выберите «Подтвердить»")

    answers = [dict(v) for v in ev["answers"]]
    if decision == "changed":
        for v in answers:
            if v["criterion_id"] == crit.id:
                v.setdefault("original_answer", original)
                v.update(answer=final_answer, status="ok", source="appeal", gross_types=gross,
                         appeal_id=appeal_id, appeal_reason=reason, review_reason=None, validation_errors=[])
                if final_answer == "na":
                    v["na_reason"] = v.get("na_reason") or f"По апелляции: {reason}"
                break
    try:
        result = _rescore(cl, answers, conv.channel if conv else None)
    except ScoringError as e:
        raise AppealError(f"Пересчёт невозможен: {e}") from e
    score_json = result.model_dump(mode="json")
    total = str(result.total) if result.total is not None else None
    status = "partial" if result.is_partial else "done"
    new_row = next((r for r in score_json["criteria"] if r["criterion_id"] == crit.id), {})

    with db.connect(path) as conn:
        db.update_evaluation_result(ev["id"], answers=answers, score=score_json, total=total,
                                    gross_error=result.gross_error, status=status,
                                    original_total=ev["total"], conn=conn)
        conn.execute(
            "UPDATE appeals SET status='resolved', decision=?, new_answer=?, new_gross_types=?, new_points=?, "
            "final_total=?, reason=?, reviewer_label=?, resolved_at=? WHERE id=?",
            (decision, final_answer, json.dumps(gross), new_row.get("points_display"), total, reason,
             (reviewer_label or "").strip() or None, db.now_iso(), appeal_id),
        )
    return db.get_appeal(appeal_id, path=path)


def appeal_details(appeal_id: int, path: Path | None = None) -> dict:
    """Всё для экрана проверяющего: апелляция, критерий, вердикт модели, баллы, диалог."""
    appeal = db.get_appeal(appeal_id, path=path)
    if appeal is None:
        raise AppealError("Апелляция не найдена", 404)
    ev, cl, crit, verdict, row = _context(appeal["evaluation_id"], appeal["criterion_id"], path)
    conv = db.get_conversation(ev["group_id"], ev["conversation_id"], path=path)
    ev.pop("raw_responses", None)
    return {
        "appeal": appeal,
        "criterion": crit.model_dump(mode="json"),
        "verdict": verdict,
        "score_row": row,
        "evaluation": {k: ev[k] for k in ("id", "group_id", "conversation_id", "checklist_id", "checklist_version",
                                          "model", "total", "original_total", "status", "gross_error", "is_current",
                                          "created_at")},
        "conversation": conv.model_dump(exclude={"raw"}) if conv else None,
        "history": db.list_appeals(evaluation_id=ev["id"], path=path),
    }
