"""Детерминированная предобработка диалога: факты для промпта и рендер транскрипта.

Никакой логики критериев здесь нет — только наблюдаемые факты о диалоге.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import settings
from .models import Conversation, Message

KZ_LETTERS = set("әғқңөұүһіӘҒҚҢӨҰҮҺІ")
CYR_RE = re.compile(r"[А-Яа-яЁё]")
LAT_RE = re.compile(r"[A-Za-z]")


def message_language(text: str) -> str | None:
    """KZ / RU / EN / None по буквам одной реплики."""
    if any(ch in KZ_LETTERS for ch in text):
        return "KZ"
    cyr, lat = len(CYR_RE.findall(text)), len(LAT_RE.findall(text))
    if cyr == 0 and lat == 0:
        return None
    return "RU" if cyr >= lat else "EN"


def detect_client_language(messages: list[Message]) -> dict:
    """Язык клиента по его репликам. Смешанный KZ/RU → "KZ+RU".

    Кириллица без казахских букв в отдельной реплике может быть и казахской (короткие фразы),
    поэтому при наличии хотя бы одной KZ-реплики пишем смешанный вариант — решает модель.
    """
    counts: dict[str, int] = {}
    short_cyr = 0  # короткие кириллические реплики без казахских букв («Рахмет», «Иә») — язык неоднозначен
    for m in messages:
        if m.speaker != "client":
            continue
        lang = message_language(m.text)
        if lang == "RU" and len(m.text.split()) < 3:
            short_cyr += 1
            continue
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    if short_cyr and not counts:
        counts["RU"] = short_cyr
    if not counts:
        detected = "unknown"
    elif "KZ" in counts and "RU" in counts:
        detected = "KZ+RU"
    else:
        detected = max(counts, key=counts.get)
    return {"detected": detected, "by_messages": counts}


def fmt_seconds(sec: float | None) -> str:
    if sec is None:
        return "?"
    s = int(round(sec))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def find_long_pauses(messages: list[Message], threshold_sec: int) -> list[dict]:
    """Паузы «клиент ждёт ответа оператора» ≥ порога.

    Для каждого ответа оператора берём самую раннюю реплику клиента после предыдущей
    реплики оператора (с этого момента клиент ждёт) и считаем разницу offset_sec.
    """
    pauses = []
    waiting_since: Message | None = None
    for m in messages:
        if m.offset_sec is None:
            continue
        if m.speaker == "client":
            if waiting_since is None:
                waiting_since = m
        elif m.speaker == "operator":
            if waiting_since is not None:
                gap = m.offset_sec - waiting_since.offset_sec
                if gap >= threshold_sec:
                    pauses.append({
                        "client_message_id": waiting_since.id,
                        "operator_message_id": m.id,
                        "pause_sec": round(gap, 1),
                    })
            waiting_since = None
    return pauses


@dataclass
class PreparedConversation:
    conversation: Conversation
    facts: dict
    message_index: dict[str, Message] = field(default_factory=dict)

    @property
    def messages(self) -> list[Message]:
        return self.conversation.messages

    def render_transcript(self) -> str:
        lines = []
        for m in self.messages:
            text = " ".join(m.text.split())  # реплика — одна строка: нельзя «закрыть» блок переводом строки
            lines.append(f"[{m.id}][{m.timestamp}][{m.speaker}] {text}")
        return "\n".join(lines)

    def render_facts(self) -> str:
        f = self.facts
        lines = [
            f"- Канал: {f['channel']}; аудиозапись: {'есть' if f['has_audio'] else 'нет'}.",
            f"- Язык в метаданных записи: {f['metadata_language'] or 'не указан'}; "
            f"язык клиента по его репликам (эвристика): {f['client_language']['detected']}.",
            f"- Первая реплика ОПЕРАТОРА (не бота): {f['first_operator_message_id'] or 'оператор не писал'}.",
            f"- Реплик: всего {f['counts']['total']}, оператор {f['counts'].get('operator', 0)}, "
            f"клиент {f['counts'].get('client', 0)}, бот {f['counts'].get('bot', 0)}, "
            f"система {f['counts'].get('system', 0)}.",
        ]
        if f["long_pauses"]:
            items = "; ".join(
                f"{p['client_message_id']}→{p['operator_message_id']} ({fmt_seconds(p['pause_sec'])})"
                for p in f["long_pauses"]
            )
            lines.append(f"- Паузы ожидания ответа оператора ≥ {f['long_pause_threshold_sec']} с: {items}.")
        else:
            lines.append(f"- Пауз ожидания ответа оператора ≥ {f['long_pause_threshold_sec']} с не обнаружено.")
        if f["system_events"]:
            items = "; ".join(f"{e['message_id']}: {e['text']}" for e in f["system_events"])
            lines.append(f"- Системные события: {items}.")
        else:
            lines.append("- Системных событий нет.")
        return "\n".join(lines)


def preprocess(conv: Conversation, long_pause_sec: int | None = None) -> PreparedConversation:
    threshold = long_pause_sec if long_pause_sec is not None else settings.long_pause_sec
    msgs = conv.messages
    counts: dict[str, int] = {"total": len(msgs)}
    for m in msgs:
        counts[m.speaker] = counts.get(m.speaker, 0) + 1
    first_op = next((m.id for m in msgs if m.speaker == "operator"), None)
    facts = {
        "channel": conv.channel,
        "has_audio": bool(conv.audio_url),
        "metadata_language": conv.language,
        "client_language": detect_client_language(msgs),
        "first_operator_message_id": first_op,
        "long_pause_threshold_sec": threshold,
        "long_pauses": find_long_pauses(msgs, threshold),
        "system_events": [{"message_id": m.id, "text": " ".join(m.text.split())} for m in msgs if m.speaker == "system"],
        "counts": counts,
    }
    return PreparedConversation(conversation=conv, facts=facts, message_index={m.id: m for m in msgs})
