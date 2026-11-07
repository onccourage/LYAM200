"""Диагностика подключения к модели: python scripts/check_llm.py

Показывает версии и настройки (ключ — только маской), делает один короткий запрос оценки на тестовом
диалоге и печатает ПОЛНЫЙ текст ошибки, если что-то не так. Стоимость — доли цента.
"""
import asyncio
import json
import platform
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def mask(v: str) -> str:
    if not v:
        return "(пусто)"
    return f"{v[:7]}…{v[-4:]} (длина {len(v)})" if len(v) > 12 else "(слишком короткий — проверьте)"


def main() -> int:
    print(f"Python: {platform.python_version()} ({sys.executable})")
    if sys.version_info < (3, 11):
        print("  ! Нужен Python 3.11+")
    try:
        import anthropic
        print(f"anthropic: {anthropic.__version__}")
        major = int(anthropic.__version__.split(".")[0])
        if major < 2:
            print("  ! Старая версия SDK — выполните: pip install -U -r requirements.txt")
    except ImportError:
        print("  ! библиотека anthropic не установлена: pip install -r requirements.txt")

    from app.config import settings
    env = ROOT / ".env"
    print(f".env: {'найден' if env.exists() else 'НЕ НАЙДЕН — скопируйте .env.example в .env'} ({env})")
    print(f"LLM_PROVIDER={settings.llm_provider}  LLM_MODEL={settings.llm_model}  LLM_EFFORT={settings.llm_effort}")
    if settings.llm_provider == "anthropic":
        print(f"ANTHROPIC_API_KEY={mask(settings.anthropic_api_key)}  ANTHROPIC_API_BASE={settings.anthropic_api_base}")
        if not settings.anthropic_api_key.startswith("sk-ant-"):
            print("  ! Ключ Anthropic обычно начинается с sk-ant-")
        if settings.llm_base_url or settings.llm_api_key:
            print("  ! В .env остались LLM_BASE_URL / LLM_API_KEY от другого провайдера — для anthropic они не нужны")
    else:
        print(f"LLM_BASE_URL={settings.llm_base_url}  LLM_API_KEY={mask(settings.llm_api_key)}")
    print(f"цены: вход {settings.price_input_per_mtok} / выход {settings.price_output_per_mtok} $ за 1M токенов")

    from app.dataset import parse_record
    from app.evaluator import evaluate_conversation
    from app.llm import LLMError, get_provider
    from app.models import Checklist

    rec = json.loads((ROOT / "tests/fixtures/mock_dataset.json").read_text(encoding="utf-8"))["data"]["records"][2]
    conv = parse_record(rec, "CHECK")
    cl = Checklist.model_validate_json((ROOT / "data/checklists/default_chat_v1.json").read_text(encoding="utf-8"))
    print("\nТестовый запрос (короткий EN-диалог, 8 критериев)…")
    try:
        provider = get_provider()
        out = asyncio.run(evaluate_conversation(conv, cl, provider, use_cache=False, save=False))
    except LLMError as e:
        print(f"ОШИБКА МОДЕЛИ: {e}")
        return 1
    except Exception:
        print("НЕОЖИДАННАЯ ОШИБКА:")
        traceback.print_exc()
        return 1
    r = out.record
    for x in r["raw_responses"]:
        if x.get("error"):
            print(f"ОШИБКА ВЫЗОВА: {x['error']}")
    if r["status"] == "error":
        print(f"\nИТОГ: модель недоступна — {r['llm_error']}")
        return 1
    bad = [a["criterion_id"] for a in r["answers"] if a["status"] != "ok"]
    print(f"\nOK: итог {r['total']}, вызовов {r['llm_calls']}, токены {r['tokens_in']}/{r['tokens_out']}, стоимость ${r['cost']}")
    for a in r["answers"]:
        print(f"  {a['criterion_id']} {a['answer'] if a['status'] == 'ok' else 'NEEDS_REVIEW'} — {a['rationale'][:90]}")
    if bad:
        print(f"  ! Критерии не прошли проверку: {bad}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
