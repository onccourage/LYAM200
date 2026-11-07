"""Записи звонков.

В наборе организаторов audio_url — это HTML-страница с плеером, а не аудиофайл. Внутри неё
<source src="..."> — ссылка на mp3 в Dynamics, действующая ~1 час (поддерживает Range → перемотка работает).
Бэкенд по запросу берёт свежую ссылку со страницы и перенаправляет на неё плеер.
Если audio_url сразу отдаёт аудио — используется как есть.
"""
from __future__ import annotations

import html
import re
import time

import httpx

from .config import settings

SOURCE_RE = re.compile(r"<source[^>]+src=\"([^\"]+)\"", re.I)
AUDIO_RE = re.compile(r"<audio[^>]+src=\"([^\"]+)\"", re.I)
TTL_SEC = 45 * 60          # ссылка Dynamics живёт ~1 час
_cache: dict[str, tuple[float, str]] = {}


class AudioError(Exception):
    pass


def extract_media_url(page: str) -> str | None:
    m = SOURCE_RE.search(page) or AUDIO_RE.search(page)
    return html.unescape(m.group(1)) if m else None


def resolve_audio_url(audio_url: str, client: httpx.Client | None = None) -> str:
    now = time.time()
    hit = _cache.get(audio_url)
    if hit and hit[0] > now:
        return hit[1]
    own = client is None
    client = client or httpx.Client(timeout=settings.dataset_timeout_sec, follow_redirects=True)
    try:
        with client.stream("GET", audio_url) as resp:
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if ctype.startswith("audio/") or ctype == "application/octet-stream":
                media = str(resp.url)
            else:
                media = extract_media_url(resp.read().decode("utf-8", errors="replace"))
    except httpx.HTTPError as e:
        raise AudioError(f"страница записи недоступна: {e}") from e
    finally:
        if own:
            client.close()
    if not media:
        raise AudioError("на странице записи не найдена ссылка на аудио")
    _cache[audio_url] = (now + TTL_SEC, media)
    return media
