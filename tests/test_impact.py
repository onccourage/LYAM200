"""Предпросмотр влияния чек-листа на набор без вызова LLM (app.evaluator.estimate_impact) —
кнопка «оценить влияние» в редакторе чек-листа: сколько диалогов уже из кэша, сколько критериев
реально уйдёт в модель, примерная стоимость. Числа кэша/вызовов точные (та же логика, что
evaluate_conversation); токены/стоимость — оценка по исторической средней, не фантазия."""
import asyncio
from decimal import Decimal

import pytest

from app.dataset import parse_record
from app.evaluator import estimate_impact, evaluate_conversation
from app.llm import FakeProvider
from app.models import Checklist
from tests.helpers import make_responder


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def cl(default_checklist_dict):
    return Checklist.model_validate(default_checklist_dict)


@pytest.fixture
def convs(mock_payload):
    return [parse_record(r, "MOCK") for r in mock_payload["data"]["records"]]


def test_fresh_checklist_everything_needs_llm(tmp_db, cl, convs):
    r = estimate_impact(cl, convs, model="fake-model", db_path=tmp_db)
    assert r["cached_criteria"] == 0
    assert r["llm_criteria"] == 8 * len(convs)
    assert r["fully_cached_conversations"] == 0
    assert r["estimated_new_calls"] == len(convs)          # 8 критериев, чанк=20 (conftest) -> 1 вызов/диалог
    assert r["estimated_tokens_in"] is None and r["estimated_cost_usd"] is None  # истории оценок нет — не придумываем


def test_after_evaluation_fully_cached(tmp_db, cl, convs):
    p = FakeProvider(make_responder())
    for c in convs:
        run(evaluate_conversation(c, cl, p, db_path=tmp_db))
    r = estimate_impact(cl, convs, model=p.model, db_path=tmp_db)
    assert r["cached_criteria"] == 8 * len(convs)
    assert r["llm_criteria"] == 0
    assert r["fully_cached_conversations"] == len(convs)
    assert r["estimated_new_calls"] == 0
    assert r["estimated_cost_usd"] is None   # ничего нового не оценивается -> нет оценки стоимости


def test_changing_one_criterion_only_it_counts(tmp_db, default_checklist_dict, convs):
    p = FakeProvider(make_responder())
    cl0 = Checklist.model_validate(default_checklist_dict)
    for c in convs:
        run(evaluate_conversation(c, cl0, p, db_path=tmp_db))
    d = dict(default_checklist_dict)
    d["criteria"] = [dict(c, guidance="новое правило") if c["id"] == "c05" else c for c in d["criteria"]]
    cl1 = Checklist.model_validate(d)
    r = estimate_impact(cl1, convs, model=p.model, db_path=tmp_db)
    assert r["llm_criteria"] == len(convs)                  # только c05, по одному на диалог
    assert r["cached_criteria"] == 7 * len(convs)
    assert r["fully_cached_conversations"] == 0
    assert r["estimated_new_calls"] == len(convs)           # 1 критерий -> 1 чанк -> 1 вызов на диалог


def test_cost_estimated_from_history(tmp_db, cl, convs):
    p = FakeProvider(make_responder())
    for c in convs:
        run(evaluate_conversation(c, cl, p, db_path=tmp_db))
    d = dict(cl.model_dump(mode="json"))
    d["criteria"] = [dict(c, guidance="изменилось") if c["id"] == "c01" else c for c in d["criteria"]]
    cl2 = Checklist.model_validate(d)
    r = estimate_impact(cl2, convs, model=p.model, price_input_per_mtok=Decimal("2"),
                        price_output_per_mtok=Decimal("10"), db_path=tmp_db)
    assert r["estimated_new_calls"] == len(convs)
    assert r["estimated_tokens_in"] is not None and r["estimated_tokens_out"] is not None
    assert Decimal(r["estimated_cost_usd"]) > 0


def test_requires_audio_is_rule_based_not_llm(tmp_db, default_checklist_dict, convs):
    d = dict(default_checklist_dict)
    d["criteria"] = [dict(c, requires_audio=True) if c["id"] == "c06" else c for c in d["criteria"]]
    cl = Checklist.model_validate(d)
    r = estimate_impact(cl, convs, model="fake-model", db_path=tmp_db)
    assert r["rule_based_criteria"] == len(convs)
    assert r["llm_criteria"] == 7 * len(convs)
    assert r["cached_criteria"] == 0


def test_channel_restricted_criterion_excluded_for_other_channel(tmp_db, default_checklist_dict, convs):
    d = dict(default_checklist_dict)
    d["criteria"] = [dict(c, channels=["voice call"]) if c["id"] == "c02" else c for c in d["criteria"]]
    cl = Checklist.model_validate(d)
    r = estimate_impact(cl, convs, model="fake-model", db_path=tmp_db)
    chats = sum(1 for c in convs if c.channel == "agent chat")
    calls = sum(1 for c in convs if c.channel == "voice call")
    assert r["llm_criteria"] == 7 * chats + 8 * calls   # c02 просто не применим к чатам — не в LLM, не в кэше


def test_consensus_samples_multiply_calls(tmp_db, cl, convs):
    r = estimate_impact(cl, convs, model="fake-model", samples=3, db_path=tmp_db)
    assert r["estimated_new_calls"] == len(convs) * 3
    assert r["samples"] == 3
