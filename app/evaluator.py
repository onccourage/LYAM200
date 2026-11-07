"""Оркестрация оценки одного диалога: preprocess → prompt → llm → validate → score → save.

- Кэш на уровне критерия: hash(транскрипт) + hash(определения критерия) + модель + PROMPT_VERSION.
  Новая версия чек-листа → в LLM уходят только изменённые/новые критерии.
- Ошибка валидации → один повторный запрос только по ошибочным критериям; снова ошибка → needs_review.
- requires_audio → правило без LLM (раздел 5.6). Критерии не своего канала → N/A в scoring.
"""
from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
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


def cache_key(transcript_hash: str, c: Criterion, model: str, samples: int = 1) -> str:
    parts = [transcript_hash, c.definition_hash(), model, PROMPT_VERSION,
             settings.explanation_lang, str(settings.long_pause_sec)]
    if samples > 1:
        parts.append(f"consensus{samples}")
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


def merge_votes(cands: list[Verdict], samples: int) -> Verdict:
    """Консенсус: ответ большинства; ничья — по средней уверенности; обоснование — самого уверенного голоса."""
    if samples <= 1:
        return cands[0]
    votes = Counter(v.answer for v in cands)
    conf = {a: sum((v.confidence or 0.5) for v in cands if v.answer == a) / n for a, n in votes.items()}
    top = max(votes, key=lambda a: (votes[a], conf[a]))
    rep = max((v for v in cands if v.answer == top), key=lambda v: v.confidence or 0.5).model_copy(deep=True)
    rep.votes = dict(votes)
    rep.samples = samples
    rep.agreement = round(votes[top] / samples, 3)
    if rep.confidence is not None:   # итоговая уверенность не выше доли согласных проходов
        rep.confidence = round(min(rep.confidence, rep.agreement), 3)
    return rep


async def _ask(provider: LLMProvider, prepared: PreparedConversation, criteria: list[Criterion],
               usage: LLMUsage, raw_log: list, retry_errors: dict | None = None, sample: int | None = None):
    req = build_request(prepared, criteria, retry_errors)
    try:
        if sample is None:
            resp = await provider.complete_json(req["system"], req["user"], req["schema"])
        else:
            resp = await provider.complete_json(req["system"], req["user"], req["schema"], sample=sample)
    except LLMError as e:
        usage.add(e.usage)
        raw_log.append({"criteria": [c.id for c in criteria], "error": str(e), "raw": e.raw, "fatal": e.fatal,
                        "usage": e.usage.as_dict(), "retry": bool(retry_errors)})
        return None, str(e)
    usage.add(resp.usage)
    raw_log.append({"criteria": [c.id for c in criteria], "model": resp.model, "usage": resp.usage.as_dict(),
                    "data": resp.data, "raw": resp.raw, "retry": bool(retry_errors), "sample": sample or 0})
    return resp, None


async def evaluate_conversation(
    conv: Conversation,
    checklist: Checklist,
    provider: LLMProvider,
    *,
    use_cache: bool = True,
    save: bool = True,
    db_path: Path | None = None,
    samples: int | None = None,
) -> EvalOutcome:
    samples = max(1, min(7, samples or settings.llm_samples))
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
    keys = {c.id: cache_key(t_hash, c, provider.model, samples) for c in to_llm}
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

    # --- LLM: основной вызов (или N проходов консенсуса) + один повтор по ошибочным критериям
    raw_items: dict = {}
    if missing:
        if samples == 1:
            results = [await _ask(provider, prepared, missing, usage, raw_log)]
        else:
            results = await asyncio.gather(*(_ask(provider, prepared, missing, usage, raw_log, sample=i)
                                             for i in range(samples)))
        per_sample: list[dict[str, Verdict]] = []
        errors: dict[str, list[str]] = {}
        call_errors: list[str] = []
        for resp, err in results:
            if resp is None:
                call_errors.append(err)
                continue
            if isinstance(resp.data, dict):
                for k, v in resp.data.items():
                    raw_items.setdefault(k, v)
            ok, errs = validate_response(missing, resp.data, prepared)
            per_sample.append(ok)
            for cid, e in errs.items():
                errors.setdefault(cid, []).extend(e)
        if not per_sample:
            llm_error = call_errors[0]
            errors = {c.id: [llm_error] for c in missing}
        for c in missing:
            cands = [s[c.id] for s in per_sample if c.id in s]
            if cands:
                verdicts[c.id] = merge_votes(cands, samples)
        errors = {cid: e for cid, e in errors.items() if cid not in verdicts}
        fatal = any(x.get("fatal") for x in raw_log)
        if errors and not fatal:
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
        elif errors:
            errors2 = errors
        if errors:
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
    llm_failed_all = bool(missing) and all(verdicts[c.id].status != "ok" for c in missing)
    if llm_error and llm_failed_all and not cached_ids:
        status = "error"  # модель недоступна: такую оценку не сохраняем, текущая остаётся прежней
    elif llm_error and status != "error":
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
        "llm_fatal": any(x.get("fatal") for x in raw_log),
        "samples": samples,
    }
    if save and status != "error":
        record["id"] = db.save_evaluation(record, path=db_path)
    return EvalOutcome(record=record, verdicts=verdicts, usage=usage,
                       llm_criteria=[c.id for c in missing], cached_criteria=cached_ids)
