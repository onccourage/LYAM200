import asyncio

import pytest

from app import db
from app.dataset import parse_record
from app.evaluator import evaluate_conversation
from app.llm import FakeProvider, LLMError
from app.models import Checklist
from tests.helpers import make_responder


@pytest.fixture
def cl(default_checklist_dict):
    return Checklist.model_validate(default_checklist_dict)


@pytest.fixture
def conv(mock_payload):
    return parse_record(mock_payload["data"]["records"][0], "MOCK")


def run(coro):
    return asyncio.run(coro)


def test_happy_path_saves_evaluation(tmp_db, cl, conv):
    p = FakeProvider(make_responder())
    out = run(evaluate_conversation(conv, cl, p, db_path=tmp_db))
    r = out.record
    assert r["status"] == "done" and r["total"] == "100.00" and r["llm_calls"] == 1
    assert len(p.requests) == 1 and list(p.requests[0]["schema"]["properties"]) == [f"c0{i}" for i in range(1, 9)]
    saved = db.get_current_evaluation("MOCK", conv.conversation_id, path=tmp_db)
    assert saved["id"] == r["id"] and saved["checklist_version"] == 1 and saved["prompt_version"] == "v1"
    assert saved["answers"][0]["evidence"][0]["timestamp"] == "00:40"
    assert saved["raw_responses"][0]["data"]["c01"]["answer"] == "yes"
    assert float(saved["cost"]) > 0


def test_cache_second_run_no_llm(tmp_db, cl, conv):
    p = FakeProvider(make_responder())
    run(evaluate_conversation(conv, cl, p, db_path=tmp_db))
    out = run(evaluate_conversation(conv, cl, p, db_path=tmp_db))
    assert len(p.requests) == 1 and out.record["llm_calls"] == 0 and out.record["cost"] == "0.000000"
    assert set(out.cached_criteria) == {f"c0{i}" for i in range(1, 9)}
    assert out.record["total"] == "100.00"
    # full re-eval ignores cache
    out = run(evaluate_conversation(conv, cl, p, use_cache=False, db_path=tmp_db))
    assert len(p.requests) == 2 and out.cached_criteria == []


def test_changed_criterion_only_it_goes_to_llm(tmp_db, default_checklist_dict, conv):
    p = FakeProvider(make_responder())
    run(evaluate_conversation(conv, Checklist.model_validate(default_checklist_dict), p, db_path=tmp_db))
    d = dict(default_checklist_dict)
    d["criteria"] = [dict(c, guidance="новое правило") if c["id"] == "c05" else
                     dict(c, weight=25) if c["id"] == "c01" else c for c in d["criteria"]]
    out = run(evaluate_conversation(conv, Checklist.model_validate(d), p, db_path=tmp_db))
    assert out.llm_criteria == ["c05"]           # вес c01 изменился → только пересчёт, без LLM
    assert list(p.requests[1]["schema"]["properties"]) == ["c05"]


def test_retry_fixes_bad_quote(tmp_db, cl, conv):
    calls = {"n": 0}
    good = make_responder()

    def responder(system, user, schema):
        calls["n"] += 1
        data = good(system, user, schema)
        if calls["n"] == 1:
            data["c03"]["evidence"] = [{"message_id": "m2", "quote": "выдуманная цитата"}]
        return data

    p = FakeProvider(responder)
    out = run(evaluate_conversation(conv, cl, p, db_path=tmp_db))
    assert out.record["llm_calls"] == 2 and out.record["status"] == "done"
    assert list(p.requests[1]["schema"]["properties"]) == ["c03"]
    assert "цитата «выдуманная цитата» не найдена" in p.requests[1]["user"]


def test_two_failures_mark_needs_review_not_crash(tmp_db, cl, conv):
    p = FakeProvider(make_responder({"c04": {"answer": "20", "evidence": [{"message_id": "m1", "quote": "Не могу"}]}}))
    out = run(evaluate_conversation(conv, cl, p, db_path=tmp_db))
    r = out.record
    v = next(a for a in r["answers"] if a["criterion_id"] == "c04")
    assert v["status"] == "needs_review" and v["answer"] is None and v["validation_errors"]
    assert r["status"] == "partial" and r["score"]["pending"] == ["c04"] and r["total"] == "100.00"
    # needs_review не кэшируется → следующий запуск снова спросит только c04
    run(evaluate_conversation(conv, cl, p, db_path=tmp_db))
    assert list(p.requests[-1]["schema"]["properties"]) == ["c04"]


def test_llm_error_is_partial(tmp_db, cl, conv):
    class Broken(FakeProvider):
        async def complete_json(self, *a):
            raise LLMError("API 529: overloaded")

    out = run(evaluate_conversation(conv, cl, Broken(lambda *a: {}), db_path=tmp_db))
    r = out.record
    assert r["status"] == "error" and r["llm_error"] and r["total"] is None
    assert all(a["status"] == "needs_review" for a in r["answers"])
    assert "id" not in r and db.get_current_evaluation("MOCK", conv.conversation_id, path=tmp_db) is None


def test_audio_and_channel_rules(tmp_db, default_checklist_dict, mock_payload):
    d = dict(default_checklist_dict)
    d["criteria"] = d["criteria"] + [
        {"id": "a1", "section": "conduct_closing", "title": "Дикция", "type": "binary", "weight": 5,
         "na_allowed": True, "na_condition": "нет записи", "requires_audio": True},
        {"id": "v1", "section": "conduct_closing", "title": "Только звонки", "type": "binary", "weight": 5,
         "channels": ["voice call"]},
    ]
    cl = Checklist.model_validate(d)
    chat = parse_record(mock_payload["data"]["records"][0], "MOCK")
    p = FakeProvider(make_responder())
    r = run(evaluate_conversation(chat, cl, p, db_path=tmp_db)).record
    assert "a1" not in p.requests[0]["schema"]["properties"] and "v1" not in p.requests[0]["schema"]["properties"]
    a1 = next(a for a in r["answers"] if a["criterion_id"] == "a1")
    assert a1["answer"] == "na" and a1["source"] == "rule" and a1["na_reason"] == "нет аудиозаписи"
    v1 = next(x for x in r["score"]["criteria"] if x["criterion_id"] == "v1")
    assert v1["status"] == "na_channel"
    assert r["total"] == "100.00" and r["status"] == "done"

    call = parse_record(mock_payload["data"]["records"][3], "MOCK")
    r = run(evaluate_conversation(call, cl, FakeProvider(make_responder()), db_path=tmp_db)).record
    a1 = next(a for a in r["answers"] if a["criterion_id"] == "a1")
    assert a1["status"] == "needs_review" and "прослушивание" in a1["review_reason"]
    assert any(a["criterion_id"] == "v1" for a in r["answers"])


def test_gross_flag_flows_to_score(tmp_db, cl, conv):
    p = FakeProvider(make_responder({"c08": {"answer": "yes", "gross_types": ["bypass_service"],
                                             "evidence": [{"message_id": "m5", "quote": "Загрузите документ"}]}}))
    r = run(evaluate_conversation(conv, cl, p, db_path=tmp_db)).record
    assert r["gross_error"] is True and r["total"] == "100.00"
    assert r["score"]["gross_types"] == ["bypass_service"]


def test_new_eval_replaces_current_keeps_history(tmp_db, cl, conv):
    p = FakeProvider(make_responder())
    a = run(evaluate_conversation(conv, cl, p, db_path=tmp_db)).record["id"]
    b = run(evaluate_conversation(conv, cl, p, db_path=tmp_db)).record["id"]
    assert db.get_current_evaluation("MOCK", conv.conversation_id, path=tmp_db)["id"] == b
    assert db.get_evaluation(a, path=tmp_db)["is_current"] is False
