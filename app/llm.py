"""LLM-провайдеры за единым интерфейсом. Остальной код знает только LLMProvider.complete_json().

AnthropicProvider — структурированный ответ через output_config.format (JSON-схема из чек-листа).
Принудительный tool_choice и temperature на текущих моделях Claude (Sonnet 5.5 и др.) возвращают 400,
поэтому схема ответа гарантируется structured outputs, а не форсированным инструментом.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Protocol

from .config import settings

# модели, для которых доступен серверный fallback при отказе классификатора безопасности (Claude API)
SERVER_FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}
SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    """Неустранимая ошибка вызова (после ретраев SDK) или отказ модели."""

    def __init__(self, message: str, usage: "LLMUsage | None" = None, raw: Any = None):
        super().__init__(message)
        self.usage = usage or LLMUsage()
        self.raw = raw


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    calls: int = 0

    def add(self, other: "LLMUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.calls += other.calls

    def cost(self) -> Decimal:
        """Стоимость в USD по ценам из конфига."""
        mtok = Decimal(1_000_000)
        p_in, p_out = settings.price_input_per_mtok, settings.price_output_per_mtok
        return (
            Decimal(self.input_tokens) * p_in
            + Decimal(self.cache_creation_input_tokens) * p_in * settings.price_cache_write_mult
            + Decimal(self.cache_read_input_tokens) * p_in * settings.price_cache_read_mult
            + Decimal(self.output_tokens) * p_out
        ) / mtok

    def as_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens, "calls": self.calls,
        }


@dataclass
class LLMResponse:
    data: dict
    text: str
    model: str
    usage: LLMUsage
    stop_reason: str | None = None
    raw: dict = field(default_factory=dict)


class LLMProvider(Protocol):
    model: str

    async def complete_json(self, system: str, user: str, schema: dict) -> LLMResponse: ...


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("в ответе нет JSON-объекта")
    return json.loads(text[start:end + 1])


# ---------------------------------------------------------------- Anthropic

class AnthropicProvider:
    def __init__(self, model: str | None = None, api_key: str | None = None):
        import anthropic

        self._anthropic = anthropic
        self.model = model or settings.llm_model
        self.client = anthropic.AsyncAnthropic(
            api_key=api_key or settings.anthropic_api_key or None,
            timeout=settings.llm_timeout_sec,
            max_retries=settings.llm_max_retries,  # 408/409/429/5xx + сетевые ошибки, экспоненциальная задержка
        )

    def _use_server_fallback(self) -> bool:
        mode = settings.llm_server_fallback.lower()
        return mode == "on" or (mode == "auto" and self.model in SERVER_FALLBACK_MODELS)

    async def complete_json(self, system: str, user: str, schema: dict) -> LLMResponse:
        a = self._anthropic
        output_config: dict = {"format": {"type": "json_schema", "schema": schema}}
        if settings.llm_effort:
            output_config["effort"] = settings.llm_effort
        kwargs: dict = dict(
            model=self.model,
            max_tokens=settings.llm_max_tokens,
            # рамка + критерии одинаковы для всех диалогов набора → кэшируются
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
        )
        if self._use_server_fallback():
            kwargs.update(betas=[SERVER_FALLBACK_BETA], fallbacks="default")
        try:
            if "betas" in kwargs:
                resp = await self.client.beta.messages.create(**kwargs)
            else:
                resp = await self.client.messages.create(**kwargs)
        except a.APIStatusError as e:
            raise LLMError(f"API {e.status_code}: {e.message}") from e
        except a.APIConnectionError as e:
            raise LLMError(f"нет соединения с API: {e}") from e

        u = resp.usage
        usage = LLMUsage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            calls=1,
        )
        raw = resp.to_dict()
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            cat = getattr(details, "category", None) if details else None
            raise LLMError(f"модель отказалась отвечать (refusal, категория: {cat})", usage, raw)
        if resp.stop_reason == "max_tokens":
            raise LLMError("ответ обрезан по max_tokens", usage, raw)
        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            data = _parse_json(text)
        except (ValueError, json.JSONDecodeError) as e:
            raise LLMError(f"ответ не является JSON: {e}", usage, raw) from e
        return LLMResponse(data=data, text=text, model=resp.model, usage=usage, stop_reason=resp.stop_reason, raw=raw)


# ---------------------------------------------------------------- OpenAI-совместимый endpoint (vLLM/Ollama)

class OpenAICompatibleProvider:
    """Локальная модель через /chat/completions с response_format=json_schema."""

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None):
        import httpx

        self.model = model or settings.llm_model
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        if not self.base_url:
            raise LLMError("LLM_BASE_URL не задан для openai_compatible")
        headers = {"Authorization": f"Bearer {api_key or settings.llm_api_key}"} if (api_key or settings.llm_api_key) else {}
        self.client = httpx.AsyncClient(timeout=settings.llm_timeout_sec, headers=headers)
        self._httpx = httpx

    async def complete_json(self, system: str, user: str, schema: dict) -> LLMResponse:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": settings.llm_max_tokens,
            "response_format": {"type": "json_schema", "json_schema": {"name": "evaluation", "schema": schema, "strict": True}},
        }
        last: Exception | None = None
        for attempt in range(settings.llm_max_retries + 1):
            try:
                r = await self.client.post(f"{self.base_url}/chat/completions", json=body)
                if r.status_code == 429 or r.status_code >= 500:
                    raise self._httpx.HTTPStatusError("retryable", request=r.request, response=r)
                r.raise_for_status()
                break
            except self._httpx.HTTPError as e:
                last = e
                if isinstance(e, self._httpx.HTTPStatusError) and e.response.status_code < 500 and e.response.status_code != 429:
                    raise LLMError(f"endpoint {e.response.status_code}: {e.response.text[:300]}") from e
                import asyncio
                await asyncio.sleep(min(2 ** attempt, 30))
        else:
            raise LLMError(f"endpoint недоступен: {last}")
        payload = r.json()
        u = payload.get("usage") or {}
        usage = LLMUsage(input_tokens=u.get("prompt_tokens", 0), output_tokens=u.get("completion_tokens", 0), calls=1)
        text = payload["choices"][0]["message"].get("content") or ""
        try:
            data = _parse_json(text)
        except (ValueError, json.JSONDecodeError) as e:
            raise LLMError(f"ответ не является JSON: {e}", usage, payload) from e
        return LLMResponse(data=data, text=text, model=payload.get("model", self.model), usage=usage,
                           stop_reason=payload["choices"][0].get("finish_reason"), raw=payload)


# ---------------------------------------------------------------- тестовый провайдер

class FakeProvider:
    """Для тестов и офлайн-демо: responder(system, user, schema) -> dict."""

    def __init__(self, responder: Callable[[str, str, dict], dict], model: str = "fake-model"):
        self.responder = responder
        self.model = model
        self.requests: list[dict] = []

    async def complete_json(self, system: str, user: str, schema: dict) -> LLMResponse:
        self.requests.append({"system": system, "user": user, "schema": schema})
        data = self.responder(system, user, schema)
        text = json.dumps(data, ensure_ascii=False)
        usage = LLMUsage(input_tokens=len(system + user) // 4, output_tokens=len(text) // 4, calls=1)
        return LLMResponse(data=data, text=text, model=self.model, usage=usage, stop_reason="end_turn")


def get_provider() -> LLMProvider:
    if settings.llm_provider == "openai_compatible":
        return OpenAICompatibleProvider()
    return AnthropicProvider()
