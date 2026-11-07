"""Конфигурация из переменных окружения (.env). Никаких критериев здесь нет — только инфраструктура."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    # допускаем комментарии в .env после значения
    return value.split(" #")[0].strip() if value else default


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else ROOT_DIR / p


@dataclass(frozen=True)
class LLMProfile:
    """Один именованный профиль модели (провайдер+ключ+цены) — для переключателя в UI
    («разнообразие подходов»: облачная модель / локальная через Ollama и т.п.). Ключи задаются
    только на сервере в .env, в браузер уходят лишь id/label/model/provider — без секретов."""
    id: str
    label: str
    provider: str                # "anthropic" | "openai_compatible"
    model: str
    base_url: str = ""
    api_key: str = ""
    price_input_per_mtok: Decimal = Decimal(0)
    price_output_per_mtok: Decimal = Decimal(0)

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "provider": self.provider, "model": self.model}


def _load_extra_profiles(max_slots: int = 5) -> list[LLMProfile]:
    """LLM_PROFILE_2_* .. LLM_PROFILE_{max_slots}_* — дополнительные профили (профиль 1 — основной,
    из обычных ANTHROPIC_*/LLM_* переменных). Числовые суффиксы, а не JSON — проще редактировать в .env руками.
    Слот учитывается, только если задан LLM_PROFILE_N_MODEL."""
    profiles = []
    for n in range(2, max_slots + 1):
        model = _env(f"LLM_PROFILE_{n}_MODEL")
        if not model:
            continue
        provider = _env(f"LLM_PROFILE_{n}_PROVIDER", "openai_compatible")
        price_in = _env(f"LLM_PROFILE_{n}_PRICE_INPUT_PER_MTOK", "0")
        price_out = _env(f"LLM_PROFILE_{n}_PRICE_OUTPUT_PER_MTOK", "0")
        profiles.append(LLMProfile(
            id=_env(f"LLM_PROFILE_{n}_ID", f"profile{n}"),
            label=_env(f"LLM_PROFILE_{n}_LABEL", model),
            provider=provider,
            model=model,
            base_url=_env(f"LLM_PROFILE_{n}_BASE_URL"),
            api_key=_env(f"LLM_PROFILE_{n}_API_KEY"),
            price_input_per_mtok=Decimal(price_in or "0"),
            price_output_per_mtok=Decimal(price_out or "0"),
        ))
    return profiles


@dataclass
class Settings:
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    # явный адрес Anthropic API: посторонняя ANTHROPIC_BASE_URL в окружении не уведёт запросы
    anthropic_api_base: str = field(default_factory=lambda: _env("ANTHROPIC_API_BASE", "https://api.anthropic.com"))
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "anthropic"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "claude-sonnet-5-5"))
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"))  # для openai_compatible
    llm_effort: str = field(default_factory=lambda: _env("LLM_EFFORT", "medium"))  # low|medium|high|"" (не передавать)
    llm_max_tokens: int = field(default_factory=lambda: int(_env("LLM_MAX_TOKENS", "16000")))
    llm_server_fallback: str = field(default_factory=lambda: _env("LLM_SERVER_FALLBACK", "auto"))  # auto|on|off
    llm_timeout_sec: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT_SEC", "300")))
    # Anthropic отклоняет запрос ("compiled grammar is too large") при ~6+ разнородных критериях
    # в одной JSON-схеме ответа — проверено на реальном API: 4-5 стабильно проходят, 6 падает независимо
    # от состава (шкалы/да-нет/флаг). Критерии дробятся на группы этого размера; на другие провайдеры
    # (Gemini, OpenAI-совместимые) лимит не похоже распространяется, но дробление безопасно и для них.
    llm_criteria_chunk_size: int = field(default_factory=lambda: int(_env("LLM_CRITERIA_PER_CALL", "4")))
    llm_samples: int = field(default_factory=lambda: int(_env("LLM_SAMPLES", "1")))  # >1 — консенсус N проходов
    llm_sample_temperature: float = field(default_factory=lambda: float(_env("LLM_SAMPLE_TEMPERATURE", "0.7")))
    llm_max_retries: int = field(default_factory=lambda: int(_env("LLM_MAX_RETRIES", "4")))

    dataset_api_url: str = field(default_factory=lambda: _env("DATASET_API_URL"))
    default_group_id: str = field(default_factory=lambda: _env("DEFAULT_GROUP_ID", "DEV"))
    dataset_timeout_sec: float = field(default_factory=lambda: float(_env("DATASET_TIMEOUT_SEC", "30")))

    explanation_lang: str = field(default_factory=lambda: _env("EXPLANATION_LANG", "ru"))
    long_pause_sec: int = field(default_factory=lambda: int(_env("LONG_PAUSE_SEC", "120")))
    batch_concurrency: int = field(default_factory=lambda: int(_env("BATCH_CONCURRENCY", "5")))
    batch_retries: int = field(default_factory=lambda: int(_env("BATCH_RETRIES", "2")))  # поверх ретраев SDK
    batch_retry_base_sec: float = field(default_factory=lambda: float(_env("BATCH_RETRY_BASE_SEC", "5")))

    price_input_per_mtok: Decimal = field(default_factory=lambda: Decimal(_env("PRICE_INPUT_PER_MTOK", "2") or "0"))
    price_output_per_mtok: Decimal = field(default_factory=lambda: Decimal(_env("PRICE_OUTPUT_PER_MTOK", "10") or "0"))
    # множители к цене входа для кэша промпта (запись / чтение)
    price_cache_write_mult: Decimal = field(default_factory=lambda: Decimal(_env("PRICE_CACHE_WRITE_MULT", "1.25")))
    price_cache_read_mult: Decimal = field(default_factory=lambda: Decimal(_env("PRICE_CACHE_READ_MULT", "0.1")))

    seed_on_start: bool = field(default_factory=lambda: _env("SEED_ON_START", "0") in ("1", "true", "yes"))
    db_path: Path = field(default_factory=lambda: _path(_env("DB_PATH", "data/cqa.db")))
    data_dir: Path = ROOT_DIR / "data"
    web_dir: Path = ROOT_DIR / "web"
    extra_llm_profiles: list[LLMProfile] = field(default_factory=_load_extra_profiles)

    @property
    def llm_profiles(self) -> list[LLMProfile]:
        """Основной профиль (из ANTHROPIC_*/LLM_* переменных) + дополнительные LLM_PROFILE_N_*."""
        default = LLMProfile(
            id="default", label=_env("LLM_PROFILE_1_LABEL", self.llm_model), provider=self.llm_provider,
            model=self.llm_model, base_url=self.llm_base_url,
            api_key=self.llm_api_key if self.llm_provider != "anthropic" else self.anthropic_api_key,
            price_input_per_mtok=self.price_input_per_mtok, price_output_per_mtok=self.price_output_per_mtok,
        )
        return [default, *self.extra_llm_profiles]

    def get_profile(self, profile_id: str | None) -> LLMProfile:
        profiles = self.llm_profiles
        if not profile_id or profile_id == "default":
            return profiles[0]
        for p in profiles:
            if p.id == profile_id:
                return p
        raise KeyError(f"неизвестный профиль модели: {profile_id!r} (доступны: {[p.id for p in profiles]})")

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def checklists_dir(self) -> Path:
        return self.data_dir / "checklists"

    def public(self) -> dict:
        """То, что можно отдать во фронтенд (без ключей)."""
        return {
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "llm_effort": self.llm_effort,
            "default_group_id": self.default_group_id,
            "dataset_api_configured": bool(self.dataset_api_url),
            "llm_configured": bool(self.anthropic_api_key) or self.llm_provider != "anthropic",
            "explanation_lang": self.explanation_lang,
            "long_pause_sec": self.long_pause_sec,
            "llm_profiles": [p.public() for p in self.llm_profiles],
        }


settings = Settings()
