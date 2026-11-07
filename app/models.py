"""Pydantic-модели: чек-лист (конфигурация-данные) и диалоги.

Здесь задаётся только СТРУКТУРА чек-листа. Конкретные критерии, веса и шкалы живут
в JSON (data/checklists/*.json и в БД) и меняются через UI без правки кода.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator, model_validator

CriterionType = Literal["binary", "scale", "flag"]
KNOWN_CHANNELS = ("agent chat", "voice call")
SPEAKERS = ("client", "operator", "bot", "system")


def _num_out(v: Decimal) -> int | float:
    """Decimal → число в JSON (целое, если без дробной части)."""
    return int(v) if v == v.to_integral_value() else float(v)


# Числа чек-листа храним как Decimal (точная арифметика в scoring), в JSON отдаём числом.
Number = Annotated[Decimal, PlainSerializer(_num_out, return_type=Any, when_used="always")]


# ---------------------------------------------------------------- чек-лист

class ScaleStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Number
    label: str = ""


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    title: str = ""
    order: int = 0


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_\-]+$")
    code: str = ""
    section: str
    title: str = Field(min_length=1)
    description: str = ""
    type: CriterionType
    weight: Number = Decimal(0)
    na_allowed: bool = False
    na_condition: str = ""
    scale: list[ScaleStep] = Field(default_factory=list)
    guidance: str = ""
    channels: list[str] = Field(default_factory=lambda: list(KNOWN_CHANNELS))
    requires_audio: bool = False
    enabled: bool = True

    @field_validator("channels")
    @classmethod
    def _channels(cls, v: list[str]) -> list[str]:
        v = [c.strip() for c in v if c and c.strip()]
        if not v:
            raise ValueError("нужен хотя бы один канал")
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _type_rules(self) -> "Criterion":
        if self.weight < 0:
            raise ValueError(f"{self.id}: вес не может быть отрицательным")
        if self.type == "flag" and self.weight != 0:
            raise ValueError(f"{self.id}: у критерия типа flag вес всегда 0")
        if self.type == "scale":
            values = [s.value for s in self.scale]
            if len(values) < 2:
                raise ValueError(f"{self.id}: у шкалы должно быть минимум 2 ступени")
            if len(set(values)) != len(values):
                raise ValueError(f"{self.id}: ступени шкалы должны быть уникальны")
            if any(v < 0 for v in values):
                raise ValueError(f"{self.id}: ступени шкалы не могут быть отрицательными")
            if max(values) <= 0:
                raise ValueError(f"{self.id}: верхняя ступень шкалы должна быть > 0")
            # храним по убыванию — верхняя ступень первой
            self.scale = sorted(self.scale, key=lambda s: s.value, reverse=True)
        elif self.scale:
            raise ValueError(f"{self.id}: шкала задаётся только для type=scale")
        return self

    @property
    def scale_values(self) -> list[Decimal]:
        return [s.value for s in self.scale]

    @property
    def scale_max(self) -> Decimal | None:
        return max(self.scale_values) if self.scale else None

    def definition_hash(self) -> str:
        """Хэш смыслового определения критерия (для кэша оценок, раздел 5.5).

        `enabled` и `weight` не влияют на ответ модели → в хэш не входят:
        смена веса пересчитывает баллы без нового вызова LLM.
        """
        payload = self.model_dump(mode="json", exclude={"enabled", "weight", "code"})
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Checklist(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str = Field(min_length=1)
    version: int = 1
    sections: list[Section]
    criteria: list[Criterion]
    rounding: int = Field(default=2, ge=0, le=6)
    created_at: str | None = None
    description: str = ""

    @model_validator(mode="after")
    def _integrity(self) -> "Checklist":
        sec_ids = [s.id for s in self.sections]
        if len(set(sec_ids)) != len(sec_ids):
            raise ValueError("ID секций должны быть уникальны")
        crit_ids = [c.id for c in self.criteria]
        dup = {i for i in crit_ids if crit_ids.count(i) > 1}
        if dup:
            raise ValueError(f"ID критериев должны быть уникальны: {sorted(dup)}")
        unknown = sorted({c.section for c in self.criteria} - set(sec_ids))
        if unknown:
            raise ValueError(f"критерии ссылаются на несуществующие секции: {unknown}")
        return self

    @property
    def enabled_criteria(self) -> list[Criterion]:
        return [c for c in self.criteria if c.enabled]

    def criterion(self, criterion_id: str) -> Criterion | None:
        return next((c for c in self.criteria if c.id == criterion_id), None)

    def sorted_sections(self) -> list[Section]:
        return sorted(self.sections, key=lambda s: s.order)

    def total_weight(self) -> Decimal:
        return sum((c.weight for c in self.enabled_criteria), Decimal(0))

    def warnings(self) -> list[str]:
        """Не блокирующие предупреждения для редактора чек-листа."""
        out: list[str] = []
        total = self.total_weight()
        if total != 100:
            out.append(
                f"Сумма весов включённых критериев = {_num_out(total)}, а не 100. "
                "Итог всё равно нормируется к 100."
            )
        for c in self.enabled_criteria:
            if c.na_allowed and not c.na_condition.strip():
                out.append(f"{c.id}: N/A разрешён, но не задано условие неприменимости.")
            if c.type != "flag" and c.weight == 0:
                out.append(f"{c.id}: весовой критерий с весом 0 не влияет на итог.")
            unknown = [ch for ch in c.channels if ch not in KNOWN_CHANNELS]
            if unknown:
                out.append(f"{c.id}: неизвестные каналы {unknown} (известны: {list(KNOWN_CHANNELS)}).")
        if not any(c.type != "flag" and c.weight > 0 for c in self.enabled_criteria):
            out.append("Нет ни одного включённого критерия с весом > 0 — итог не будет рассчитываться.")
        used = {c.section for c in self.criteria}
        for s in self.sections:
            if s.id not in used:
                out.append(f"Секция «{s.title or s.id}» пустая.")
        return out


# ---------------------------------------------------------------- диалоги

class Message(BaseModel):
    id: str                      # m0, m1, ... = индекс в исходном массиве
    index: int
    timestamp: str               # ИСХОДНЫЙ timestamp без изменений
    offset_sec: float | None     # нормализованное смещение (для плеера / пауз)
    speaker: str
    text: str


class Conversation(BaseModel):
    conversation_id: str
    group_id: str
    channel: str
    language: str
    audio_url: str | None = None
    messages: list[Message]
    raw: dict[str, Any]          # исходная запись как пришла от API

    def transcript_hash(self) -> str:
        payload = [(m.speaker, m.timestamp, m.text) for m in self.messages]
        raw = json.dumps(payload, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
