"""Оркестрация оценки одного диалога: preprocess → prompt → llm → validate → score → save.

- Кэш на уровне критерия: hash(транскрипт) + hash(определения критерия) + модель + PROMPT_VERSION.
  Новая версия чек-листа → в LLM уходят только изменённые/новые критерии.
- Ошибка валидации → один повторный запрос только по ошибочным критериям; снова ошибка → needs_review.
- requires_audio → правило без LLM (раздел 5.6). Критерии не своего канала → N/A в scoring.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from . import db
from .config import settings
from .llm import LLMError, LLMProvider, LLMUsage
from .models import Checklist, Conversation, Criterion
from .preprocess import PreparedConversation, preprocess
from .prompt_builder import PROMPT_VERSION, build_request
from .scoring import ScoringError, applies_to_channel, score
from .validator import Verdict, needs_review, validate_response


def cache_key(transcript_hash: str, c: Criterion, model: str) -> str:
    parts = [transcript_hash, c.definition_hash(), model, PROMPT_VERSION,
             settings.explanation_lang, str(settings.long_pause_sec)]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def audio_rule(c: Criterion, conv: Conversation) -> Verdict:
    """Критерии, требующие аудио: по тексту не оцениваем (раздел 5.6)."""
    if conv.channel != "voice call" or not conv.audio_url:
        if c.na_allowed:
            return Verdict(criterion_id=c.id, answer="na", source="rule", na_reason="нет аудиозаписи",
                           rationale=f"{c.title} — N/A. Критерий требует прослушивания, а аудиозаписи нет.")
        return needs_review(c.id, "нет аудиозаписи, а N/A для критерия не разрешён")
    v = needs_review(c.id, "требуется прослушивание записи: по тексту расшифровки звук не оценивается")
    v.source = "rule"
    return v


@dataclass
class EvalOutcome:
    record: dict
    verdicts: dict[str, Verdict]
    usage: LLMUsage
    llm_criteria: list[str] = field(default_factory=list)   # что реально ушло в LLM
    cached_criteria: list[str] = field(default_factory=list)


async def _ask(provider: LLMProvider, prepared: PreparedConversation, criteria: list[Criterion],
               usage: LLMUsage, raw_log: list, retry_errors: dict | None = None):
    req = build_request(prepared, criteria, retry_errors)
    try:
        resp = await provider.complete_json(req["system"], req["user"], req["schema"])
    except LLMError as e:
        usage.add(e.usage)
        raw_log.append({"criteria": [c.id for c in criteria], "error": str(e), "raw": e.raw,
                        "usage": e.usage.as_dict(), "retry": bool(retry_errors)})
        return None, str(e)
    usage.add(resp.usage)
    raw_log.append({"criteria": [c.id for c in criteria], "model": resp.model, "usage": resp.usage.as_dict(),
                    "data": resp.data, "raw": resp.raw, "retry": bool(retry_errors)})
    return resp, None


async def evaluate_conversation(
    conv: Conversation,
    checklist: Checklist,
    provider: LLMProvider,
    *,
    use_cache: bool = True,
    save: bool = True,
    db_path: Path | None = None,
) -> EvalOutcome:
    prepared = preprocess(conv)
    usage = LLMUsage()
    raw_log: list[dict] = []
    verdicts: dict[str, Verdict] = {}
    llm_error: str | None = None

    applicable = [c for c in checklist.enabled_criteria if applies_to_channel(c, conv.channel)]
    for c in applicable:
        if c.requires_audio:
            verdicts[c.id] = audio_rule(c, conv)
    to_llm = [c for c in applicable if not c.requires_audio]

    # --- кэш
    t_hash = conv.transcript_hash()
    keys = {c.id: cache_key(t_hash, c, provider.model) for c in to_llm}
    cached_ids: list[str] = []
    if use_cache:
        hits = db.cache_get_many(list(keys.values()), path=db_path)
        for c in to_llm:
            hit = hits.get(keys[c.id])
            if hit is not None:
                v = Verdict.model_validate(hit)
                v.source = "cache"
                verdicts[c.id] = v
                cached_ids.append(c.id)
    else:
        db.cache_delete_for(list(keys.values()), path=db_path)
    missing = [c for c in to_llm if c.id not in verdicts]

    # --- LLM: основной вызов + один повтор по ошибочным критериям
    raw_items: dict = {}
    if missing:
        resp, err = await _ask(provider, prepared, missing, usage, raw_log)
        if resp is None:
            llm_error = err
            errors = {c.id: [err] for c in missing}
        else:
            raw_items = resp.data if isinstance(resp.data, dict) else {}
            ok, errors = validate_response(missing, resp.data, prepared)
            verdicts.update(ok)
        if errors:
            retry = [c for c in missing if c.id in errors]
            resp2, err2 = await _ask(provider, prepared, retry, usage, raw_log, retry_errors=errors)
            if resp2 is None:
                llm_error = err2
                errors2 = {c.id: errors[c.id] + [err2] for c in retry}
            else:
                ok2, errors2 = validate_response(retry, resp2.data, prepared)
                verdicts.update(ok2)
                if isinstance(resp2.data, dict):
                    raw_items.update(resp2.data)
            for cid, errs in errors2.items():
                verdicts[cid] = needs_review(cid, "ответ модели не прошёл проверку после повторного запроса",
                                             errs, raw_items.get(cid))
        fresh = [(keys[c.id], c.id, verdicts[c.id].model_dump(mode="json", by_alias=True))
                 for c in missing if verdicts[c.id].status == "ok"]
        db.cache_put_many(fresh, path=db_path)

    # --- подсчёт (без LLM)
    answers = {cid: {"answer": v.answer if v.status == "ok" else None, "gross_types": v.gross_types}
               for cid, v in verdicts.items()}
    try:
        result = score(checklist, answers, channel=conv.channel)
        score_json = result.model_dump(mode="json")
        total = str(result.total) if result.total is not None else None
        gross = result.gross_error
        status = "done" if not result.is_partial else "partial"
    except ScoringError as e:  # не должно случаться после валидации — но не роняем диалог
        score_json, total, gross, status = {"error": str(e), "errors": e.errors}, None, False, "error"
    if llm_error and status != "error":
        status = "partial"

    ordered = [verdicts[c.id] for c in checklist.enabled_criteria if c.id in verdicts]
    record = {
        "group_id": conv.group_id,
        "conversation_id": conv.conversation_id,
        "checklist_id": checklist.id,
        "checklist_version": checklist.version,
        "model": provider.model,
        "prompt_version": PROMPT_VERSION,
        "status": status,
        "total": total,
        "gross_error": gross,
        "answers": [v.model_dump(mode="json", by_alias=True) for v in ordered],
        "score": score_json,
        "tokens_in": usage.input_tokens + usage.cache_creation_input_tokens + usage.cache_read_input_tokens,
        "tokens_out": usage.output_tokens,
        "llm_calls": usage.calls,
        "cost": str(usage.cost().quantize(Decimal("0.000001"))),
        "raw_responses": raw_log,
        "llm_error": llm_error,
    }
    if save:
        record["id"] = db.save_evaluation(record, path=db_path)
    return EvalOutcome(record=record, verdicts=verdicts, usage=usage,
                       llm_criteria=[c.id for c in missing], cached_criteria=cached_ids)
