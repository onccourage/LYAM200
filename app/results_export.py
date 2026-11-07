"""Экспорт в формат результатов хакатона (CQA_Student_Guide_Appendix_Results, JSON 1.0).

Формат организаторов ФИКСИРОВАН: 8 критериев с внешними кодами (contact..critical), поле
true/false/null или ступень шкалы, evidence_turns — 1-based по ИСХОДНОМУ массиву транскрипта
(клиент+бот+система+оператор вместе). Это не наш внутренний формат — отдельный слой конвертации,
который не должен просачиваться в остальной код.

Критично для контрольного часа: внешние коды закреплены за нашими ВНУТРЕННИМИ ID критериев
(c01→contact … c08→critical), а не за их текущим содержанием. Поэтому при замене критерия в
редакторе чек-листа правьте СУЩЕСТВУЮЩИЙ критерий на нужном слоте (его ID не меняется при
редактировании), а не выключайте его и не добавляйте новый ID — иначе экспорт не найдёт критерий
на нужном внешнем коде и остановится с понятной ошибкой (лучше, чем молча сдать неполный файл).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from . import db
from .models import Checklist

SCHEMA_VERSION = "1.0"

# внутренний ID критерия → (внешний код, ожидаемый вес, ожидаемый тип) — для проверки перед экспортом
EXPECTED = {
    "c01": ("contact", Decimal(10), "binary"),
    "c02": ("discovery", Decimal(15), "binary"),
    "c03": ("identification", Decimal(20), "binary"),
    "c04": ("consultation", Decimal(20), "scale"),
    "c05": ("empathy", Decimal(15), "binary"),
    "c06": ("dialogue", Decimal(15), "scale"),
    "c07": ("closure", Decimal(5), "binary"),
    "c08": ("critical", Decimal(0), "flag"),
}
EXTERNAL_IDS = {v[0] for v in EXPECTED.values()}


class ExportError(ValueError):
    """Несоответствие чек-листа ожидаемому формату сдачи — лучше остановиться, чем сдать неверный файл."""


@dataclass
class ExportWarning:
    conversation_id: str
    message: str


@dataclass
class ExportResult:
    payload: dict
    warnings: list[ExportWarning] = field(default_factory=list)
    ok_count: int = 0
    error_count: int = 0


def check_checklist_shape(cl: Checklist) -> list[str]:
    """Список расхождений с ожидаемой формой (пусто — всё совпадает). Не бросает исключение."""
    problems = []
    enabled = {c.id: c for c in cl.enabled_criteria}
    missing = set(EXPECTED) - set(enabled)
    extra = set(enabled) - set(EXPECTED)
    if missing:
        problems.append(f"нет включённых критериев с ID {sorted(missing)} (ожидались слоты "
                        f"{[EXPECTED[m][0] for m in sorted(missing)]})")
    if extra:
        problems.append(f"лишние включённые критерии, не входящие в формат сдачи: {sorted(extra)} "
                        "(если это новый критерий взамен старого — выключите его или верните ID на место)")
    for cid, (ext, weight, typ) in EXPECTED.items():
        c = enabled.get(cid)
        if c is None:
            continue
        if c.type != typ:
            problems.append(f"{cid} ({ext}): тип {c.type}, ожидался {typ} — формат сдачи различает true/false "
                            f"и числовую шкалу, менять тип этого слота нельзя")
        if c.weight != weight:
            problems.append(f"{cid} ({ext}): вес {c.weight}, в приложении организаторов указан {weight} "
                            "(это не ошибка подсчёта, final_score пересчитывается по ИХ таблице весов)")
    return problems


def _turn(message_id: str) -> int:
    """m0 (0-based, индекс в исходном массиве) → turn 1 (1-based, "нумерация с 1 по исходному массиву")."""
    return int(message_id[1:]) + 1


def _evidence_turns(verdict: dict) -> list[int]:
    turns = {_turn(e["message_id"]) for e in verdict.get("evidence") or []}
    if not turns and verdict.get("checked_range"):
        cr = verdict["checked_range"]
        turns = {_turn(cr["from"]), _turn(cr["to"])}
    return sorted(turns)


def _value(cid: str, verdict: dict):
    ext, _, typ = EXPECTED[cid]
    answer = verdict.get("answer")
    if answer is None:
        raise ExportError(f"{cid} ({ext}): нет значения (needs_review) — такой критерий нельзя выгрузить как ok")
    if typ == "flag":
        if answer not in ("yes", "no"):
            raise ExportError(f"{cid} ({ext}): флаг не поддерживает значение {answer!r} в формате сдачи")
        return answer == "yes"
    if answer == "na":
        return None
    if typ == "binary":
        if answer not in ("yes", "no"):
            raise ExportError(f"{cid} ({ext}): недопустимое значение {answer!r} для да/нет")
        return answer == "yes"
    # scale
    d = Decimal(str(answer))
    return int(d) if d == d.to_integral_value() else float(d)


def _criterion_record(cid: str, verdict: dict) -> dict:
    ext, _, _ = EXPECTED[cid]
    return {
        "criterion_id": ext,
        "value": _value(cid, verdict),
        "rationale": verdict.get("rationale") or "",
        "evidence_turns": _evidence_turns(verdict),
    }


def _error_record(conversation_id: str, message: str) -> dict:
    return {"conversation_id": conversation_id, "status": "error", "criteria": [], "score": None, "error": message}


def build_results(group_id: str, team_name: str, *, model_mode: str = "external_api", model_name: str | None = None,
                  checklist_id: str = "default_chat", require_count: int | None = None,
                  db_path: Path | None = None) -> ExportResult:
    cl = db.get_checklist(checklist_id, path=db_path)
    if cl is None:
        raise ExportError(f"чек-лист {checklist_id} не найден")
    shape_problems = check_checklist_shape(cl)
    if shape_problems:
        raise ExportError("Чек-лист не соответствует формату сдачи:\n  - " + "\n  - ".join(shape_problems))

    convs = db.list_conversations(group_id, path=db_path)
    if not convs:
        raise ExportError(f"набор {group_id} не загружен")
    if require_count is not None and len(convs) != require_count:
        raise ExportError(f"в наборе {group_id} {len(convs)} диалогов, а для сдачи нужно ровно {require_count}")

    warnings: list[ExportWarning] = []
    results: list[dict] = []
    models_seen: dict[str, int] = {}
    ok_count = error_count = 0

    for conv in convs:
        ev = db.get_current_evaluation(group_id, conv.conversation_id, path=db_path)
        if ev is None:
            results.append(_error_record(conv.conversation_id, "Диалог не оценён"))
            error_count += 1
            continue
        if ev["checklist_id"] != checklist_id:
            results.append(_error_record(conv.conversation_id,
                f"Оценён чек-листом {ev['checklist_id']}, а не {checklist_id}"))
            error_count += 1
            continue
        by_id = {a["criterion_id"]: a for a in ev["answers"]}
        missing_slots = [cid for cid in EXPECTED if cid not in by_id or by_id[cid].get("status") != "ok"]
        if ev["status"] != "done" or ev["total"] is None or missing_slots:
            names = [EXPECTED[m][0] for m in missing_slots] if missing_slots else []
            reason = f"не определены критерии: {', '.join(names)}" if names else f"статус оценки «{ev['status']}»"
            results.append(_error_record(conv.conversation_id, f"Оценка не завершена ({reason})"))
            error_count += 1
            continue
        try:
            criteria = [_criterion_record(cid, by_id[cid]) for cid in EXPECTED]
        except ExportError as e:
            results.append(_error_record(conv.conversation_id, str(e)))
            error_count += 1
            continue
        results.append({
            "conversation_id": conv.conversation_id,
            "status": "ok",
            "criteria": criteria,
            "score": {"final_score": float(ev["total"]), "critical_flag": bool(ev["gross_error"])},
        })
        ok_count += 1
        models_seen[ev["model"]] = models_seen.get(ev["model"], 0) + 1

    ids = [r["conversation_id"] for r in results]
    if len(set(ids)) != len(ids):
        dup = {i for i in ids if ids.count(i) > 1}
        raise ExportError(f"повторяющиеся conversation_id: {sorted(dup)}")

    if model_name is None:
        model_name = ", ".join(sorted(models_seen)) if models_seen else "неизвестно"
    if len(models_seen) > 1:
        warnings.append(ExportWarning("*", f"диалоги оценены разными моделями: {models_seen} — "
                                      f"в model.name записаны все: {model_name!r}"))

    payload = {
        "schema_version": SCHEMA_VERSION,
        "team_name": team_name,
        "model": {"mode": model_mode, "name": model_name},
        "results": results,
    }
    return ExportResult(payload=payload, warnings=warnings, ok_count=ok_count, error_count=error_count)


def write_results(result: ExportResult, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result.payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
