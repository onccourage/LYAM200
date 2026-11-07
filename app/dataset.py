"""Загрузка наборов из API организаторов, локальный кэш, ручная загрузка.

Исходные timestamps сохраняются БЕЗ изменений; дополнительно вычисляется offset_sec.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import db
from .config import settings
from .models import SPEAKERS, Conversation, Message

GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
CLOCK_RE = re.compile(r"^(\d+):(\d{1,2})(?::(\d{1,2}))?(?:[.,](\d+))?$")


class DatasetError(Exception):
    """Ошибка загрузки набора — текст показывается в UI."""


class DatasetRejected(DatasetError):
    """API ответил isSuccess=false — не маскируем кэшем."""


@dataclass
class LoadResult:
    group_id: str
    source: str                     # api | cache | upload
    conversations: list[Conversation]
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- timestamps

def parse_clock(ts: str) -> float | None:
    """MM:SS / HH:MM:SS (+ дробная часть) → секунды. Иначе None."""
    m = CLOCK_RE.match(ts.strip())
    if not m:
        return None
    a, b, c, frac = m.groups()
    if c is None:
        total = int(a) * 60 + int(b)
    else:
        total = int(a) * 3600 + int(b) * 60 + int(c)
    return total + (float(f"0.{frac}") if frac else 0.0)


def parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def call_base_time(times: list[datetime | None]) -> datetime | None:
    """Базовая точка для звонков: момент, которому соответствует 0 в записи.

    Сейчас = первая реплика звонка. Если на данных выяснится, что запись начинается раньше/позже
    (например, есть отдельное поле начала звонка), поправить нужно только здесь.
    """
    return next((t for t in times if t is not None), None)


def compute_offsets(timestamps: list[str]) -> list[float | None]:
    clocks = [parse_clock(t) if isinstance(t, str) else None for t in timestamps]
    if any(c is not None for c in clocks):
        return clocks
    times = [parse_iso(t) if isinstance(t, str) else None for t in timestamps]
    base = call_base_time(times)
    out: list[float | None] = []
    for t in times:
        if t is None or base is None:
            out.append(None)
            continue
        try:
            out.append(round((t - base).total_seconds(), 3))
        except TypeError:  # смесь naive/aware
            out.append(None)
    return out


# ---------------------------------------------------------------- парсинг записей

def parse_record(record: dict[str, Any], group_id: str) -> Conversation:
    if not isinstance(record, dict):
        raise ValueError("запись должна быть объектом")
    cid = record.get("conversation_id")
    if not cid or not isinstance(cid, str):
        raise ValueError("нет conversation_id")
    transcript = record.get("transcript")
    if not isinstance(transcript, list):
        raise ValueError(f"{cid}: transcript должен быть массивом")

    timestamps = [str(t.get("timestamp", "")) if isinstance(t, dict) else "" for t in transcript]
    offsets = compute_offsets(timestamps)
    messages = []
    for i, turn in enumerate(transcript):
        if not isinstance(turn, dict):
            raise ValueError(f"{cid}: реплика {i} не объект")
        speaker = str(turn.get("speaker", "")).strip().lower()
        if speaker not in SPEAKERS:
            speaker = speaker or "unknown"
        messages.append(Message(
            id=f"m{i}", index=i, timestamp=timestamps[i], offset_sec=offsets[i],
            speaker=speaker, text=str(turn.get("text", "")),
        ))
    return Conversation(
        conversation_id=cid,
        group_id=group_id,
        channel=str(record.get("channel") or "agent chat").strip().lower(),
        language=str(record.get("language") or "").strip().upper(),
        audio_url=record.get("audio_url") or None,
        messages=messages,
        raw=record,
    )


def parse_records(records: list[Any], group_id: str) -> tuple[list[Conversation], list[str]]:
    convs, warnings, seen = [], [], set()
    for i, rec in enumerate(records):
        try:
            c = parse_record(rec, group_id)
        except ValueError as e:
            warnings.append(f"запись #{i} пропущена: {e}")
            continue
        if c.conversation_id in seen:
            warnings.append(f"дубликат {c.conversation_id} — взята последняя версия")
            convs = [x for x in convs if x.conversation_id != c.conversation_id]
        seen.add(c.conversation_id)
        convs.append(c)
    return convs, warnings


def extract_records(payload: Any) -> list[Any]:
    """Достать records из ответа API. Проверяет isSuccess."""
    if not isinstance(payload, dict):
        raise DatasetError("Ответ API не является JSON-объектом")
    if payload.get("isSuccess") is False:
        raise DatasetRejected(f"API вернул ошибку: {payload.get('error') or 'isSuccess=false без описания'}")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise DatasetError("В ответе API нет data.records")
    return data["records"]


# ---------------------------------------------------------------- кэш

def validate_group_id(group_id: str) -> str:
    group_id = (group_id or "").strip()
    if not GROUP_ID_RE.match(group_id):
        raise DatasetError("Недопустимый group_id (разрешены латиница, цифры, _ . -)")
    return group_id


def cache_path(group_id: str, cache_dir: Path | None = None) -> Path:
    return (cache_dir or settings.cache_dir) / f"{validate_group_id(group_id)}.json"


def write_cache(group_id: str, payload: dict, cache_dir: Path | None = None) -> Path:
    p = cache_path(group_id, cache_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return p


def read_cache(group_id: str, cache_dir: Path | None = None) -> dict | None:
    p = cache_path(group_id, cache_dir)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- загрузка

def build_url(base: str, group_id: str) -> str:
    """Подставить group_id в URL. Если в базовом URL уже есть group_id (как в выданной ссылке) — заменить."""
    parts = urlsplit(base.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "group_id"]
    query.append(("group_id", group_id))
    return urlunsplit(parts._replace(query=urlencode(query)))


def fetch_payload(group_id: str, *, url: str | None = None, client: httpx.Client | None = None) -> dict:
    """GET {DATASET_API_URL}?group_id=... — только с бэкенда."""
    url = url if url is not None else settings.dataset_api_url
    if not url:
        raise DatasetError("DATASET_API_URL не задан — доступна только загрузка из кэша")
    own = client is None
    client = client or httpx.Client(timeout=settings.dataset_timeout_sec, follow_redirects=True)
    try:
        resp = client.get(build_url(url, group_id))
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        raise DatasetError(f"API организаторов ответил {e.response.status_code}") from e
    except httpx.HTTPError as e:
        raise DatasetError(f"API организаторов недоступен: {e}") from e
    except ValueError as e:
        raise DatasetError("API организаторов вернул не-JSON") from e
    finally:
        if own:
            client.close()


def load_dataset(
    group_id: str,
    *,
    refresh: bool = True,
    url: str | None = None,
    client: httpx.Client | None = None,
    cache_dir: Path | None = None,
    db_path: Path | None = None,
) -> LoadResult:
    """Загрузить набор: API → кэш-файл + БД. Если API недоступен — из кэша (с предупреждением).

    Ошибка isSuccess=false не маскируется кэшем: её нужно увидеть в UI.
    """
    group_id = validate_group_id(group_id)
    warnings: list[str] = []
    payload: dict | None = None
    source = "api"
    fetch_error = ""

    if refresh:
        try:
            payload = fetch_payload(group_id, url=url, client=client)
            extract_records(payload)  # проверить до записи в кэш
            write_cache(group_id, payload, cache_dir)
        except DatasetRejected:
            raise
        except DatasetError as e:
            payload = None
            fetch_error = str(e)

    if payload is None:
        payload = read_cache(group_id, cache_dir)
        source = "cache"
        if payload is None:
            raise DatasetError(f"Набор {group_id} не найден в кэше. {fetch_error}".strip())
        if fetch_error:
            warnings.append(f"{fetch_error}. Использован локальный кэш.")

    convs, parse_warnings = parse_records(extract_records(payload), group_id)
    warnings.extend(parse_warnings)
    declared = (payload.get("data") or {}).get("total_records")
    if isinstance(declared, int) and declared != len(convs):
        warnings.append(f"total_records={declared}, а загружено {len(convs)}")
    db.save_dataset(group_id, source, convs, path=db_path)
    return LoadResult(group_id, source, convs, warnings)


def load_upload(payload: Any, group_id: str = "MANUAL", db_path: Path | None = None) -> LoadResult:
    """Ручная загрузка: одна запись, массив записей или полный ответ API."""
    group_id = validate_group_id(group_id)
    if isinstance(payload, dict) and "data" in payload:
        records = extract_records(payload)
    elif isinstance(payload, dict):
        records = [payload]
    elif isinstance(payload, list):
        records = payload
    else:
        raise DatasetError("Ожидается JSON-объект записи, массив записей или ответ API")
    convs, warnings = parse_records(records, group_id)
    if not convs:
        raise DatasetError("Не удалось разобрать ни одной записи: " + "; ".join(warnings))
    db.add_conversations(group_id, convs, path=db_path)
    return LoadResult(group_id, "upload", convs, warnings)
