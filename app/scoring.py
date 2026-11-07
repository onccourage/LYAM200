"""Детерминированный подсчёт баллов. ЧИСТАЯ функция: без LLM, без БД, без float.

score(checklist, answers, channel) -> ScoreResult

Алгоритм (CLAUDE.md, раздел 4):
1. Применимость: N/A допустим только при na_allowed; критерии, не подходящие по channels,
   исключаются как N/A с причиной (системная неприменимость, na_allowed не требуется).
2. binary: yes → полный вес, no → 0. scale: значение обязано быть ступенью.
3. scale: points = weight * value / max(scale).
4. total = sum(points_applicable) / sum(weight_applicable) * 100;
   effective_weight = weight / sum_applicable * 100, effective_points = points / sum_applicable * 100.
5. Округление ROUND_HALF_UP только итога и итогов секций (Decimal, промежуточное — без округления).
6. Нет применимых критериев с весом → total = None («не рассчитывается»), НЕ 0.
7. flag не даёт баллов и не обнуляет итог: gross_error + gross_types.
8. Итоги секций: points, applicable_weight, pct (или None).

Ответ со значением None — критерий на ручной проверке (needs_review): он не входит в расчёт,
итог помечается is_partial=True, а список таких критериев возвращается в pending.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import Checklist, Criterion

GROSS_TYPES = ("insult", "bypass_service", "confidential_disclosure")
HUNDRED = Decimal(100)

Status = Literal["scored", "na", "na_channel", "pending", "flag"]


class ScoringError(ValueError):
    """Ответы не согласуются с чек-листом. Содержит список всех проблем."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class CriterionAnswer(BaseModel):
    """Минимум, нужный для подсчёта. answer: "yes" | "no" | "na" | число-ступень | None (needs_review)."""
    criterion_id: str
    answer: Any = None
    gross_types: list[str] = Field(default_factory=list)


class CriterionScore(BaseModel):
    criterion_id: str
    section: str
    type: str
    status: Status
    answer: str | None                 # нормализованный: yes/no/na или ступень строкой
    weight: Decimal                    # исходный вес
    points: Decimal | None = None      # баллы в исходных весах (точно)
    max_points: Decimal | None = None
    effective_weight: Decimal | None = None   # вес после перераспределения N/A (сумма = 100)
    effective_points: Decimal | None = None   # вклад в итог из 100 (точно)
    effective_weight_display: str | None = None
    effective_points_display: str | None = None
    points_display: str | None = None          # баллы в исходных весах, округлённые для показа
    na_reason: str | None = None       # для na_channel — системная причина
    gross_types: list[str] = Field(default_factory=list)


class SectionScore(BaseModel):
    section_id: str
    title: str
    points: Decimal | None             # округлено
    applicable_weight: Decimal
    pct: Decimal | None                # округлено; None — нет применимых весов
    has_weights: bool                  # False для секций только с flag
    gross_error: bool = False


class ScoreResult(BaseModel):
    checklist_id: str
    checklist_version: int
    total: Decimal | None              # округлено; None = «не рассчитывается»
    total_status: Literal["calculated", "not_calculated"]
    is_partial: bool                   # есть критерии на ручной проверке
    applicable_weight: Decimal
    points: Decimal                    # сумма баллов применимых (точно)
    gross_error: bool
    gross_types: list[str]
    gross_criteria: list[str]
    pending: list[str]
    criteria: list[CriterionScore]
    sections: list[SectionScore]


# ---------------------------------------------------------------- helpers

def _q(value: Decimal, digits: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)


def _to_decimal(v: Any) -> Decimal | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, Decimal)):
        return Decimal(v)
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, str):
        try:
            d = Decimal(v.strip().replace(",", "."))
        except InvalidOperation:
            return None
        return d if d.is_finite() else None
    return None


def _fmt(d: Decimal) -> str:
    """Ступень шкалы строкой без лишних нулей: 7, 10.5."""
    d = d.normalize()
    return f"{d:f}" if d == d.to_integral_value() else str(d)


def normalize_answer(c: Criterion, raw: Any) -> tuple[str, Decimal | None]:
    """→ ("yes"|"no"|"na"|"scale", value). Бросает ValueError с понятным текстом."""
    if isinstance(raw, str) and raw.strip().lower() in ("na", "n/a"):
        if not c.na_allowed:
            raise ValueError(f"{c.id}: N/A не разрешён для этого критерия")
        return "na", None
    if c.type in ("binary", "flag"):
        if raw is True or (isinstance(raw, str) and raw.strip().lower() in ("yes", "да")):
            return "yes", None
        if raw is False or (isinstance(raw, str) and raw.strip().lower() in ("no", "нет")):
            return "no", None
        raise ValueError(f"{c.id}: для типа {c.type} допустимы yes/no" + ("/na" if c.na_allowed else "") + f", получено {raw!r}")
    # scale
    value = _to_decimal(raw)
    if value is None:
        raise ValueError(f"{c.id}: для шкалы нужно число-ступень, получено {raw!r}")
    if value not in c.scale_values:
        allowed = ", ".join(_fmt(v) for v in c.scale_values)
        raise ValueError(f"{c.id}: значение {_fmt(value)} не является ступенью шкалы ({allowed})")
    return "scale", value


def _as_map(answers: Any) -> dict[str, CriterionAnswer]:
    if isinstance(answers, dict):
        items = []
        for k, v in answers.items():
            if isinstance(v, CriterionAnswer):
                items.append(v)
            elif isinstance(v, dict) and "answer" in v:
                items.append(CriterionAnswer(criterion_id=k, **{x: y for x, y in v.items() if x != "criterion_id"}))
            else:
                items.append(CriterionAnswer(criterion_id=k, answer=v))
    else:
        items = [a if isinstance(a, CriterionAnswer) else CriterionAnswer.model_validate(a) for a in answers]
    return {a.criterion_id: a for a in items}


def applies_to_channel(c: Criterion, channel: str | None) -> bool:
    return channel is None or channel in c.channels


# ---------------------------------------------------------------- score

def score(checklist: Checklist, answers: Any, channel: str | None = None) -> ScoreResult:
    """answers: {criterion_id: answer | {"answer":..., "gross_types": [...]}} или список CriterionAnswer.

    channel=None — без фильтра по каналу.
    """
    amap = _as_map(answers)
    digits = checklist.rounding
    errors: list[str] = []
    enabled = checklist.enabled_criteria
    enabled_ids = {c.id for c in enabled}

    unknown = sorted(set(amap) - enabled_ids)
    if unknown:
        errors.append(f"ответы на неизвестные или выключенные критерии: {unknown}")

    rows: list[CriterionScore] = []
    for c in enabled:
        base = dict(criterion_id=c.id, section=c.section, type=c.type, weight=c.weight)
        if not applies_to_channel(c, channel):
            rows.append(CriterionScore(**base, status="na_channel", answer="na",
                                       na_reason=f"критерий не применяется к каналу «{channel}»"))
            continue
        a = amap.get(c.id)
        if a is None:
            errors.append(f"{c.id}: нет ответа")
            continue
        if a.answer is None:
            rows.append(CriterionScore(**base, status="pending", answer=None))
            continue
        try:
            kind, value = normalize_answer(c, a.answer)
        except ValueError as e:
            errors.append(str(e))
            continue

        if c.type == "flag":
            gtypes: list[str] = []
            if kind == "yes":
                bad = [g for g in a.gross_types if g not in GROSS_TYPES]
                if bad:
                    errors.append(f"{c.id}: неизвестные gross_types {bad}")
                gtypes = list(dict.fromkeys(a.gross_types))
            rows.append(CriterionScore(**base, status="na" if kind == "na" else "flag",
                                       answer=kind, gross_types=gtypes))
            continue

        if kind == "na":
            rows.append(CriterionScore(**base, status="na", answer="na"))
            continue
        if kind == "yes":
            points = c.weight
        elif kind == "no":
            points = Decimal(0)
        else:
            points = c.weight * value / c.scale_max
        rows.append(CriterionScore(**base, status="scored", answer=_fmt(value) if kind == "scale" else kind,
                                   points=points, max_points=c.weight,
                                   points_display=_fmt(_q(points, digits))))

    if errors:
        raise ScoringError(errors)

    scored = [r for r in rows if r.status == "scored"]
    applicable_weight = sum((r.weight for r in scored), Decimal(0))
    points = sum((r.points for r in scored), Decimal(0))

    if applicable_weight > 0:
        total = _q(points / applicable_weight * HUNDRED, digits)
        for r in scored:
            r.effective_weight = r.weight / applicable_weight * HUNDRED
            r.effective_points = r.points / applicable_weight * HUNDRED
            r.effective_weight_display = str(_q(r.effective_weight, digits))
            r.effective_points_display = str(_q(r.effective_points, digits))
    else:
        total = None

    flagged = [r for r in rows if r.type == "flag" and r.answer == "yes"]
    gross_types = list(dict.fromkeys(g for r in flagged for g in r.gross_types))

    sections: list[SectionScore] = []
    for s in checklist.sorted_sections():
        s_rows = [r for r in rows if r.section == s.id]
        s_scored = [r for r in s_rows if r.status == "scored"]
        s_weight = sum((r.weight for r in s_scored), Decimal(0))
        s_points = sum((r.points for r in s_scored), Decimal(0))
        has_weights = any(c.type != "flag" for c in enabled if c.section == s.id)
        sections.append(SectionScore(
            section_id=s.id, title=s.title,
            points=_q(s_points, digits) if has_weights else None,
            applicable_weight=s_weight,
            pct=_q(s_points / s_weight * HUNDRED, digits) if s_weight > 0 else None,
            has_weights=has_weights,
            gross_error=any(r.criterion_id in {f.criterion_id for f in flagged} for r in s_rows),
        ))

    return ScoreResult(
        checklist_id=checklist.id,
        checklist_version=checklist.version,
        total=total,
        total_status="calculated" if total is not None else "not_calculated",
        is_partial=any(r.status == "pending" for r in rows),
        applicable_weight=applicable_weight,
        points=points,
        gross_error=bool(flagged),
        gross_types=gross_types,
        gross_criteria=[r.criterion_id for r in flagged],
        pending=[r.criterion_id for r in rows if r.status == "pending"],
        criteria=rows,
        sections=sections,
    )
