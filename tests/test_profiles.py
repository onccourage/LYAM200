"""Переключатель профиля модели (провайдер/ключ/цены) — для демонстрации на разных подходах
(облачная модель / локальная через Ollama и т.п.), без секретов в браузере."""
import asyncio
from decimal import Decimal

import pytest

from app import db
from app.batch import BatchManager
from app.config import LLMProfile, Settings, _load_extra_profiles
from app.dataset import load_upload
from app.evaluator import evaluate_conversation
from app.llm import AnthropicProvider, FakeProvider, OpenAICompatibleProvider, get_provider
from app.models import Checklist
from tests.helpers import make_responder


def test_load_extra_profiles_from_numbered_env(monkeypatch):
    monkeypatch.setenv("LLM_PROFILE_2_MODEL", "qwen3:14b")
    monkeypatch.setenv("LLM_PROFILE_2_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_PROFILE_2_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_PROFILE_2_LABEL", "Локальная Qwen (Ollama)")
    monkeypatch.setenv("LLM_PROFILE_3_MODEL", "gemini-3.5-flash")
    monkeypatch.setenv("LLM_PROFILE_3_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_PROFILE_3_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
    monkeypatch.setenv("LLM_PROFILE_3_PRICE_INPUT_PER_MTOK", "0")
    monkeypatch.delenv("LLM_PROFILE_4_MODEL", raising=False)

    profiles = _load_extra_profiles()
    assert [p.id for p in profiles] == ["profile2", "profile3"]
    assert profiles[0].label == "Локальная Qwen (Ollama)" and profiles[0].base_url == "http://localhost:11434/v1"
    assert profiles[1].model == "gemini-3.5-flash" and profiles[1].price_input_per_mtok == Decimal(0)


def test_undefined_slot_is_skipped(monkeypatch):
    monkeypatch.delenv("LLM_PROFILE_2_MODEL", raising=False)
    assert _load_extra_profiles() == []


def test_settings_llm_profiles_includes_default_first():
    extra = [LLMProfile(id="local", label="Local", provider="openai_compatible", model="qwen3",
                        base_url="http://x", api_key="")]
    s = Settings(extra_llm_profiles=extra)
    ids = [p.id for p in s.llm_profiles]
    assert ids[0] == "default" and ids[1:] == ["local"]
    assert s.get_profile(None).id == "default" and s.get_profile("default").id == "default"
    assert s.get_profile("local").model == "qwen3"
    with pytest.raises(KeyError, match="local-typo"):
        s.get_profile("local-typo")


def test_profile_public_has_no_secrets():
    p = LLMProfile(id="x", label="X", provider="openai_compatible", model="m", base_url="http://x", api_key="supersecret")
    pub = p.public()
    assert "api_key" not in pub and "supersecret" not in str(pub) and set(pub) == {"id", "label", "provider", "model"}


def test_get_provider_builds_from_profile(monkeypatch):
    import app.config as config_mod
    extra = [LLMProfile(id="oc", label="OC", provider="openai_compatible", model="m1", base_url="http://x",
                        api_key="k", price_input_per_mtok=Decimal("1"), price_output_per_mtok=Decimal("2"))]
    monkeypatch.setattr(config_mod.settings, "extra_llm_profiles", extra)
    p = get_provider("oc")
    assert isinstance(p, OpenAICompatibleProvider)
    assert p.model == "m1" and p.base_url == "http://x" and p.price_input_per_mtok == Decimal("1")

    monkeypatch.setattr(config_mod.settings, "anthropic_api_key", "sk-ant-test")
    d = get_provider("default")
    assert isinstance(d, AnthropicProvider) and d.model == config_mod.settings.llm_model


def test_cost_uses_providers_own_prices():
    from app.llm import LLMUsage
    u = LLMUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert u.cost(Decimal("0"), Decimal("0")) == Decimal("0")
    assert u.cost(Decimal("3"), Decimal("7")) == Decimal("10")


def test_evaluation_cost_follows_selected_provider(tmp_db, mock_payload, default_checklist_dict):
    load_upload(mock_payload, "MOCK", db_path=tmp_db)
    cl = db.save_checklist_version(Checklist.model_validate(default_checklist_dict), path=tmp_db)
    conv = db.get_conversation("MOCK", "mock-ru-001", path=tmp_db)
    p = FakeProvider(make_responder())
    p.price_input_per_mtok = Decimal("0")
    p.price_output_per_mtok = Decimal("0")
    out = asyncio.run(evaluate_conversation(conv, cl, p, db_path=tmp_db))
    assert out.record["cost"] == "0.000000"   # «бесплатный» профиль — не берётся цена из .env


def test_batch_routes_to_profile_not_default_factory(tmp_db, mock_payload, default_checklist_dict, monkeypatch):
    import app.batch as batch_mod
    load_upload(mock_payload, "MOCK", db_path=tmp_db)
    cl = db.save_checklist_version(Checklist.model_validate(default_checklist_dict), path=tmp_db)
    ids = [c.conversation_id for c in db.list_conversations("MOCK", path=tmp_db)]

    default_provider = FakeProvider(make_responder())
    profile_provider = FakeProvider(make_responder(), model="profile-model")
    seen = []

    def fake_get_provider(profile=None):
        seen.append(profile)
        return profile_provider

    monkeypatch.setattr(batch_mod, "get_provider", fake_get_provider)
    monkeypatch.setattr(batch_mod.settings, "extra_llm_profiles",
                        [LLMProfile(id="alt", label="Alt", provider="openai_compatible", model="alt-model", base_url="http://x")])

    m = BatchManager(provider_factory=lambda: default_provider, db_path=tmp_db)

    async def scenario():
        job_id = m.submit("MOCK", cl, ids, provider="alt")
        await m.tasks[job_id]
        return job_id

    job_id = asyncio.run(scenario())
    j = db.get_batch_job(job_id, path=tmp_db)
    assert j["status"] == "done" and seen == ["alt"]
    assert len(profile_provider.requests) > 0 and len(default_provider.requests) == 0

    with pytest.raises(KeyError):
        m.submit("MOCK", cl, ids, provider="does-not-exist")
