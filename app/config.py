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


@dataclass
class Settings:
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "anthropic"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "claude-sonnet-5-5"))
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"))  # для openai_compatible
    llm_effort: str = field(default_factory=lambda: _env("LLM_EFFORT", "medium"))  # low|medium|high|"" (не передавать)
    llm_max_tokens: int = field(default_factory=lambda: int(_env("LLM_MAX_TOKENS", "16000")))
    llm_server_fallback: str = field(default_factory=lambda: _env("LLM_SERVER_FALLBACK", "auto"))  # auto|on|off
    llm_timeout_sec: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT_SEC", "300")))
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

    seed_on_start: bool = field(default_factory=lambda: _env("SEED_ON_START", "1") not in ("0", "false", "no"))
    db_path: Path = field(default_factory=lambda: _path(_env("DB_PATH", "data/cqa.db")))
    data_dir: Path = ROOT_DIR / "data"
    web_dir: Path = ROOT_DIR / "web"

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
        }


settings = Settings()
