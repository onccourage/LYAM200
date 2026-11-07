"""Проверка ответа модели против чек-листа и транскрипта.

Модели не доверяем: значения, ID реплик, цитаты и правила N/A проверяются детерминированно.
Ошибки собираются по каждому критерию отдельно — для повторного запроса только по ним.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import Criterion, Message
from .preprocess import PreparedConversation
from .prompt_builder import answer_options

ACTOR_SPEAKERS = {"operator", "system"}  # чьи реплики могут подтверждать действие оператора/событие
_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…)\s*")
_WS = re.compile(r"\s+")
_QUOTES = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "’": "'", "‘": "'", "ё": "е", "Ё": "Е"})


class Evidence(BaseModel):
    message_id: str
    quote: str
    speaker: str | None = None
    timestamp: str | None = None
    offset_sec: float | None = None


class CheckedRange(BaseModel):
    from_id: str = Field(alias="from")
    to_id: str = Field(alias="to")

    model_config = {"populate_by_name": True}


class Verdict(BaseModel):
    """Проверенный ответ по критерию (то, что хранится в оценке и показывается в UI)."""
    criterion_id: str
    status: Literal["ok", "needs_review"] = "ok"
    answer: str | None = None            # yes|no|na|ступень строкой; None — needs_review
    rationale: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    checked_range: CheckedRange | None = None
    na_reason: str | None = None
    confidence: float | None = None
    gross_types: list[str] = Field(default_factory=list)
    source: Literal["llm", "cache", "rule", "appeal"] = "llm"
    review_reason: str | None = None
    validation_errors: list[str] = Field(default_factory=list)
    # консенсус нескольких проходов модели
    votes: dict[str, int] | None = None
    samples: int | None = None
    agreement: float | None = None


def normalize_text(s: str) -> str:
    return _WS.sub(" ", s.translate(_QUOTES)).strip().lower()


def quote_in_text(quote: str, text: str) -> bool:
    """Цитата — подстрока реплики после нормализации. Допускается «...» между фрагментами (по порядку)."""
    t = normalize_text(text)
    parts = [normalize_text(p).strip(" \"'") for p in _ELLIPSIS.split(quote)]
    parts = [p for p in parts if p]
    if not parts:
        return False
    pos = 0
    for p in parts:
        i = t.find(p, pos)
        if i < 0:
            return False
        pos = i + len(p)
    return True


def _msg_num(mid: str) -> int:
    return int(mid[1:]) if re.fullmatch(r"m\d+", mid or "") else -1


def validate_criterion(c: Criterion, item: Any, prepared: PreparedConversation) -> tuple[Verdict | None, list[str]]:
    errors: list[str] = []
    if not isinstance(item, dict):
        return None, ["нет объекта ответа"]
    idx: dict[str, Message] = prepared.message_index

    raw_answer = item.get("answer")
    answer = str(raw_answer).strip().lower() if raw_answer is not None else ""
    if c.type == "scale" and answer not in ("na",):
        # «15.0» → «15»
        try:
            from decimal import Decimal
            d = Decimal(answer).normalize()
            answer = f"{d:f}" if d == d.to_integral_value() else str(d)
        except Exception:
            pass
    allowed = answer_options(c)
    if answer not in allowed:
        if answer == "na" and not c.na_allowed:
            errors.append("N/A для этого критерия не разрешён")
        else:
            errors.append(f"недопустимый ответ {raw_answer!r}; допустимо: {', '.join(allowed)}")

    evidence: list[Evidence] = []
    for i, ev in enumerate(item.get("evidence") or []):
        if not isinstance(ev, dict):
            errors.append(f"evidence[{i}] не объект")
            continue
        mid, quote = str(ev.get("message_id", "")).strip(), str(ev.get("quote", ""))
        msg = idx.get(mid)
        if msg is None:
            errors.append(f"реплики {mid!r} нет в диалоге")
            continue
        if not quote.strip():
            errors.append(f"пустая цитата для {mid}")
            continue
        if not quote_in_text(quote, msg.text):
            errors.append(f"цитата «{quote[:80]}» не найдена в реплике {mid} — нужна точная подстрока")
            continue
        evidence.append(Evidence(message_id=mid, quote=quote, speaker=msg.speaker,
                                 timestamp=msg.timestamp, offset_sec=msg.offset_sec))

    checked_range = None
    cr = item.get("checked_range")
    if isinstance(cr, dict) and cr.get("from") and cr.get("to"):
        f, t = str(cr["from"]), str(cr["to"])
        if f not in idx or t not in idx:
            errors.append(f"checked_range {f}–{t} ссылается на несуществующие реплики")
        elif _msg_num(f) > _msg_num(t):
            errors.append(f"checked_range: начало {f} позже конца {t}")
        else:
            checked_range = CheckedRange(from_id=f, to_id=t)

    na_reason = (item.get("na_reason") or "").strip() or None
    rationale = (item.get("rationale") or "").strip()
    if not rationale:
        errors.append("пустое обоснование (rationale)")

    gross_types = [g for g in (item.get("gross_types") or [])]
    if answer == "na":
        if not na_reason:
            errors.append("для N/A обязательна na_reason — какое условие неприменимости выполнено")
    elif c.type == "flag":
        if answer == "yes":
            if c.flag_types:
                bad = [g for g in gross_types if g not in c.flag_type_ids]
                if bad:
                    errors.append(f"неизвестные gross_types: {bad}; допустимо: {c.flag_type_ids}")
                if not gross_types:
                    errors.append("при флаге yes нужно указать gross_types")
            else:
                gross_types = []
            if not any(e.speaker == "operator" for e in evidence):
                errors.append("флаг нарушения требует цитаты из реплики оператора")
        else:
            gross_types = []
    elif answer == "no":
        if not evidence and checked_range is None:
            errors.append('для "no" без цитат обязателен checked_range — какой диапазон реплик проверен')
    elif answer in allowed:  # yes или ступень шкалы
        positive = answer == "yes" or (c.type == "scale" and answer not in ("0",))
        if positive and not any(e.speaker in ACTOR_SPEAKERS for e in evidence):
            errors.append("положительный ответ требует хотя бы одной цитаты из реплики оператора (или системного события)")
    if evidence and all(e.speaker == "bot" for e in evidence) and answer not in ("na",):
        errors.append("доказательства только из реплик бота — бот не оператор")

    conf = item.get("confidence")
    try:
        confidence = max(0.0, min(1.0, float(conf))) if conf is not None else None
    except (TypeError, ValueError):
        confidence = None

    if errors:
        return None, errors
    return Verdict(
        criterion_id=c.id, answer=answer, rationale=rationale, evidence=evidence, checked_range=checked_range,
        na_reason=na_reason if answer == "na" else None, confidence=confidence,
        gross_types=gross_types if c.type == "flag" else [],
    ), []


def validate_response(criteria: list[Criterion], data: Any, prepared: PreparedConversation) -> tuple[dict[str, Verdict], dict[str, list[str]]]:
    """→ (валидные вердикты, ошибки по критериям). Лишние ключи игнорируются и логируются в ошибках '_extra'."""
    ok: dict[str, Verdict] = {}
    errors: dict[str, list[str]] = {}
    if not isinstance(data, dict):
        return {}, {c.id: ["ответ модели не является объектом"] for c in criteria}
    for c in criteria:
        if c.id not in data:
            errors[c.id] = ["критерий отсутствует в ответе"]
            continue
        v, errs = validate_criterion(c, data[c.id], prepared)
        if v is not None:
            ok[c.id] = v
        else:
            errors[c.id] = errs
    return ok, errors


def needs_review(criterion_id: str, reason: str, errors: list[str] | None = None, raw: Any = None) -> Verdict:
    rationale = ""
    if isinstance(raw, dict):
        rationale = str(raw.get("rationale") or "")
    return Verdict(criterion_id=criterion_id, status="needs_review", answer=None, rationale=rationale,
                   review_reason=reason, validation_errors=errors or [])
