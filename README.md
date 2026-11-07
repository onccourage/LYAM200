# CQA — Conversation Quality Analytics (Freedom Broker Hackathon)

Прототип оценки качества работы операторов поддержки по диалогам (чаты и звонки, RU / KZ / EN).
LLM только классифицирует ответы по критериям; баллы считает детерминированный `scoring.py`.
**Чек-лист — это данные**: критерии, веса, шкалы и тексты хранятся в JSON/БД и меняются через UI
без правки кода и перезапуска.

> Статус: этап 1 (скелет) — загрузка наборов, кэш, модели, сид чек-листа, API. Остальные разделы README
> будут дописаны по мере этапов.

## Архитектура

```
API организаторов ──► dataset.py ──► data/cache/{group_id}.json + SQLite
                                         │
чек-лист (JSON, версии) ──► prompt_builder ─► llm ─► validator ─► scoring ─► evaluations
                                         │                               │
                                       web/ (HTML+JS, без сборщика) ◄── FastAPI
```

## Запуск с нуля

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env   # вписать ANTHROPIC_API_KEY и DATASET_API_URL
```

```bash
make run               # = uvicorn app.main:app --reload; UI: http://localhost:8000, API: /docs
```

```bash
make test
```

## Переменные окружения

См. `.env.example`. Главные: `ANTHROPIC_API_KEY`, `LLM_MODEL` (по умолчанию `claude-sonnet-5-5`),
`DATASET_API_URL`, `DEFAULT_GROUP_ID`, `EXPLANATION_LANG`, `LONG_PAUSE_SEC`, цены за токены.

## Загрузка набора

- UI/API: `POST /api/datasets/{group_id}/load` — запрос к API организаторов идёт только с бэкенда.
  Ответ кэшируется в `data/cache/{group_id}.json`; при недоступности API берётся кэш (с предупреждением).
  `isSuccess=false` показывается как ошибка, кэшем не маскируется.
- CLI: `make fetch GROUP=DEV`.
- Ручная загрузка: `POST /api/conversations/upload` (JSON-файл: запись, массив записей или ответ API).
- Исходные `timestamp` не изменяются; добавляется `offset_sec` (чаты `MM:SS`/`HH:MM:SS`, звонки —
  ISO 8601 от первой реплики; базовая точка — `dataset.call_base_time`).

## Чек-лист

Сид: `data/checklists/default_chat_v1.json` (загружается в БД при первом старте).
Сохранение изменённого чек-листа (`POST /api/checklists`) создаёт новую неизменяемую версию.
`POST /api/checklists/validate` — проверка без сохранения + предупреждения (сумма весов ≠ 100 и т.п.).

## Модель и стоимость

_TBD (этап 5)._

## Ограничения

_TBD._
