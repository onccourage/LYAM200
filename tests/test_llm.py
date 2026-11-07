"""Проверка формы запроса к Anthropic API на уровне HTTP (без ключа и сети)."""
import asyncio
import json
from decimal import Decimal

import httpx2
import pytest

from app import config
from app.llm import AnthropicProvider, LLMError, LLMUsage


def _provider(handler, model="claude-sonnet-5-5"):
    import anthropic
    p = AnthropicProvider(model=model, api_key="test-key")
    p.client = anthropic.AsyncAnthropic(api_key="test-key", base_url="http://api.test", max_retries=0,
                                        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    return p


def _reply(content, stop_reason="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 3000}}


def test_request_shape_and_parsing(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_effort", "medium")
    monkeypatch.setattr(config.settings, "llm_server_fallback", "auto")
    seen = {}

    def handler(req: httpx2.Request):
        seen["body"] = json.loads(req.content)
        seen["beta"] = req.headers.get("anthropic-beta")
        return httpx2.Response(200, json=_reply([{"type": "text", "text": '{"c01": {"answer": "yes"}}'}]))

    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    r = asyncio.run(_provider(handler).complete_json("SYS", "USER", schema))
    b = seen["body"]
    assert b["model"] == "claude-sonnet-5-5"
    assert b["output_config"] == {"format": {"type": "json_schema", "schema": schema}, "effort": "medium"}
    assert b["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert b["messages"] == [{"role": "user", "content": "USER"}]
    assert b["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in seen["beta"]
    assert "temperature" not in b and "tool_choice" not in b
    assert r.data == {"c01": {"answer": "yes"}}
    assert r.usage.cache_read_input_tokens == 3000


def test_no_fallback_for_haiku(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_server_fallback", "auto")
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx2.Response(200, json=_reply([{"type": "text", "text": "{}"}]))

    asyncio.run(_provider(handler, model="claude-haiku-5-5").complete_json("S", "U", {"type": "object"}))
    assert "fallbacks" not in seen["body"]


def test_refusal_raises_with_usage():
    def handler(req):
        return httpx2.Response(200, json=_reply([], stop_reason="refusal"))

    with pytest.raises(LLMError, match="refusal") as e:
        asyncio.run(_provider(handler).complete_json("S", "U", {"type": "object"}))
    assert e.value.usage.calls == 1


def test_api_error_raises():
    def handler(req):
        return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}})

    with pytest.raises(LLMError, match="400"):
        asyncio.run(_provider(handler).complete_json("S", "U", {"type": "object"}))


def test_cost_from_config(monkeypatch):
    monkeypatch.setattr(config.settings, "price_input_per_mtok", Decimal("2"))
    monkeypatch.setattr(config.settings, "price_output_per_mtok", Decimal("10"))
    u = LLMUsage(input_tokens=1_000_000, output_tokens=100_000, cache_read_input_tokens=1_000_000,
                 cache_creation_input_tokens=1_000_000)
    assert u.cost() == Decimal("2") + Decimal("1") + Decimal("0.2") + Decimal("2.5")


def test_openai_compatible_counts_reasoning_tokens(monkeypatch):
    import httpx

    from app.llm import OpenAICompatibleProvider
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={
            "model": "gemini-3.5-flash",
            "choices": [{"finish_reason": "stop", "message": {"content": '{"c01": {"answer": "yes"}}'}}],
            "usage": {"prompt_tokens": 2713, "completion_tokens": 1029, "total_tokens": 5802},
        })

    p = OpenAICompatibleProvider(model="gemini-3.5-flash", base_url="https://x.test/v1beta/openai", api_key="k")
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer k"})
    r = asyncio.run(p.complete_json("S", "U", {"type": "object"}))
    assert r.data == {"c01": {"answer": "yes"}}
    assert r.usage.input_tokens == 2713 and r.usage.output_tokens == 5802 - 2713   # «размышления» учтены
    assert seen["body"]["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert seen["auth"] == "Bearer k"
