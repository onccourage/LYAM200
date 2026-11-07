"""Формат сдачи результатов (CQA_Student_Guide_Appendix_Results, JSON 1.0): конвертация
нашего внутреннего представления в фиксированный внешний формат организаторов.
"""
import asyncio

import pytest

from app import db
from app.dataset import load_upload
from app.evaluator import evaluate_conversation
from app.llm import FakeProvider
from app.models import Checklist
from app.results_export import ExportError, build_results, check_checklist_shape
from tests.helpers import make_responder

MAX = {"c01": "yes", "c02": "yes", "c03": "yes", "c04": 20, "c05": "yes", "c06": 10, "c07": "yes", "c08": "no"}
EXTERNAL_IDS = {"contact", "discovery", "identification", "consultation", "empathy", "dialogue", "closure", "critical"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def world(tmp_db, mock_payload, default_checklist_dict):
    load_upload(mock_payload, "MOCK", db_path=tmp_db)
    cl = db.save_checklist_version(Checklist.model_validate(default_checklist_dict), path=tmp_db)
    return tmp_db, cl


def test_checklist_shape_matches_default(default_checklist_dict):
    cl = Checklist.model_validate(default_checklist_dict)
    assert check_checklist_shape(cl) == []


def test_shape_detects_weight_and_type_drift(default_checklist_dict):
    import copy
    d = copy.deepcopy(default_checklist_dict)
    d["criteria"] = [dict(c, weight=99) if c["id"] == "c01" else c for c in d["criteria"]]
    problems = check_checklist_shape(Checklist.model_validate(d))
    assert any("c01" in p and "вес" in p for p in problems)

    d2 = copy.deepcopy(default_checklist_dict)
    d2["criteria"] = [c for c in d2["criteria"] if c["id"] != "c05"]
    problems2 = check_checklist_shape(Checklist.model_validate(d2))
    assert any("c05" in p for p in problems2)


def test_happy_path_structure_and_mapping(world):
    tmp_db, cl = world
    convs = db.list_conversations("MOCK", path=tmp_db)
    for c in convs:
        run(evaluate_conversation(c, cl, FakeProvider(make_responder()), db_path=tmp_db))

    r = build_results("MOCK", "Команда Икс", model_name="claude-sonnet-5-5", db_path=tmp_db)
    assert r.ok_count == len(convs) and r.error_count == 0
    p = r.payload
    assert p["schema_version"] == "1.0" and p["team_name"] == "Команда Икс"
    assert p["model"] == {"mode": "external_api", "name": "claude-sonnet-5-5"}
    assert len(p["results"]) == len(convs)

    rec = next(x for x in p["results"] if x["conversation_id"] == "mock-ru-001")
    assert rec["status"] == "ok"
    ids = [c["criterion_id"] for c in rec["criteria"]]
    assert set(ids) == EXTERNAL_IDS and len(ids) == 8   # каждый ровно один раз
    by_ext = {c["criterion_id"]: c for c in rec["criteria"]}
    assert by_ext["contact"]["value"] is True
    assert by_ext["consultation"]["value"] == 20 and isinstance(by_ext["consultation"]["value"], int)
    assert by_ext["critical"]["value"] is False
    assert rec["score"]["final_score"] == 100.0 and rec["score"]["critical_flag"] is False
    for c in rec["criteria"]:
        assert isinstance(c["rationale"], str) and c["rationale"]
        assert isinstance(c["evidence_turns"], list)


def test_evidence_turns_are_1_based_over_full_transcript(world):
    tmp_db, cl = world
    conv = db.get_conversation("MOCK", "mock-ru-001", path=tmp_db)
    # m2 — первая реплика оператора (после m0 бот, m1 клиент) → ожидаем turn 3
    p = FakeProvider(make_responder({"c01": {"evidence": [{"message_id": "m2", "quote": "Здравствуйте"}]}}))
    run(evaluate_conversation(conv, cl, p, db_path=tmp_db))
    r = build_results("MOCK", "T", db_path=tmp_db)
    rec = next(x for x in r.payload["results"] if x["conversation_id"] == "mock-ru-001")
    contact = next(c for c in rec["criteria"] if c["criterion_id"] == "contact")
    assert contact["evidence_turns"] == [3]


def test_evidence_turns_are_sorted_and_deduped(world):
    tmp_db, cl = world
    conv = db.get_conversation("MOCK", "mock-ru-001", path=tmp_db)
    p = FakeProvider(make_responder({"c02": {"evidence": [
        {"message_id": "m5", "quote": "Вывод заблокирован"},
        {"message_id": "m2", "quote": "Здравствуйте"},
        {"message_id": "m5", "quote": "Загрузите документ"},   # две цитаты из одной реплики
    ]}}))
    run(evaluate_conversation(conv, cl, p, db_path=tmp_db))
    r = build_results("MOCK", "T", db_path=tmp_db)
    rec = next(x for x in r.payload["results"] if x["conversation_id"] == "mock-ru-001")
    discovery = next(c for c in rec["criteria"] if c["criterion_id"] == "discovery")
    assert discovery["evidence_turns"] == [3, 6]   # отсортировано, без повтора turn 6


def test_checked_range_falls_back_to_turn_endpoints(world):
    tmp_db, cl = world
    conv = db.get_conversation("MOCK", "mock-ru-001", path=tmp_db)
    p = FakeProvider(make_responder({"c07": {"answer": "no", "evidence": [],
                                             "checked_range": {"from": "m5", "to": "m7"}}}))
    run(evaluate_conversation(conv, cl, p, db_path=tmp_db))
    r = build_results("MOCK", "T", db_path=tmp_db)
    rec = next(x for x in r.payload["results"] if x["conversation_id"] == "mock-ru-001")
    closure = next(c for c in rec["criteria"] if c["criterion_id"] == "closure")
    assert closure["value"] is False and closure["evidence_turns"] == [6, 8]


def test_na_is_null_and_scale_is_number(world):
    tmp_db, cl = world
    conv = db.get_conversation("MOCK", "mock-kz-001", path=tmp_db)
    p = FakeProvider(make_responder({"c03": {"answer": "na", "na_reason": "x", "evidence": []},
                                     "c06": {"answer": "7"}}))
    run(evaluate_conversation(conv, cl, p, db_path=tmp_db))
    r = build_results("MOCK", "T", db_path=tmp_db)
    rec = next(x for x in r.payload["results"] if x["conversation_id"] == "mock-kz-001")
    by_ext = {c["criterion_id"]: c for c in rec["criteria"]}
    assert by_ext["identification"]["value"] is None
    assert by_ext["dialogue"]["value"] == 7


def test_unevaluated_and_needs_review_become_error_not_fabricated(world):
    tmp_db, cl = world
    convs = db.list_conversations("MOCK", path=tmp_db)
    # mock-ru-001 не оценён вовсе; остальные — с одним needs_review критерием
    for c in convs:
        if c.conversation_id == "mock-ru-001":
            continue
        p = FakeProvider(make_responder({"c04": {"answer": "bad"}}))
        run(evaluate_conversation(c, cl, p, db_path=tmp_db))

    r = build_results("MOCK", "T", db_path=tmp_db)
    assert r.error_count == len(convs) and r.ok_count == 0
    for rec in r.payload["results"]:
        assert rec["status"] == "error" and rec["criteria"] == [] and rec["score"] is None
        assert rec["error"]
    not_evaluated = next(x for x in r.payload["results"] if x["conversation_id"] == "mock-ru-001")
    assert "не оценён" in not_evaluated["error"].lower()
    needs_review = next(x for x in r.payload["results"] if x["conversation_id"] == "mock-kz-001")
    assert "consultation" in needs_review["error"]   # явно назван незаполненный слот


def test_gross_flag_sets_critical_true_without_zeroing_score(world):
    tmp_db, cl = world
    conv = db.get_conversation("MOCK", "mock-ru-001", path=tmp_db)
    p = FakeProvider(make_responder({"c08": {"answer": "yes", "gross_types": ["insult"],
                                             "evidence": [{"message_id": "m2", "quote": "Здравствуйте"}]}}))
    run(evaluate_conversation(conv, cl, p, db_path=tmp_db))
    r = build_results("MOCK", "T", db_path=tmp_db)
    rec = next(x for x in r.payload["results"] if x["conversation_id"] == "mock-ru-001")
    assert rec["score"]["critical_flag"] is True and rec["score"]["final_score"] == 100.0
    critical = next(c for c in rec["criteria"] if c["criterion_id"] == "critical")
    assert critical["value"] is True


def test_require_count_enforced_for_prod(world):
    tmp_db, cl = world
    with pytest.raises(ExportError, match="ровно 10"):
        build_results("MOCK", "T", require_count=10, db_path=tmp_db)


def test_wrong_checklist_shape_raises_before_any_record(world):
    tmp_db, cl = world
    import copy
    d = cl.model_dump(mode="json")
    d["criteria"] = [c for c in d["criteria"] if c["id"] != "c08"]
    bad = db.save_checklist_version(Checklist.model_validate(d), path=tmp_db)
    conv = db.get_conversation("MOCK", "mock-ru-001", path=tmp_db)
    run(evaluate_conversation(conv, bad, FakeProvider(make_responder()), db_path=tmp_db))
    with pytest.raises(ExportError, match="critical"):
        build_results("MOCK", "T", db_path=tmp_db)


def test_evaluated_with_old_checklist_version_is_error(world):
    tmp_db, cl = world
    conv = db.get_conversation("MOCK", "mock-ru-001", path=tmp_db)
    run(evaluate_conversation(conv, cl, FakeProvider(make_responder()), db_path=tmp_db))
    # вторую версию чек-листа не оцениваем этим диалогом — but same checklist_id so this branch
    # exercises the "iterate conversations not in current checklist_id/version" path differently:
    # here we simulate evaluation done under a DIFFERENT checklist_id entirely.
    other = db.save_checklist_version(cl.model_copy(update={"id": "other_chat"}), path=tmp_db)
    run(evaluate_conversation(conv, other, FakeProvider(make_responder()), db_path=tmp_db))
    r = build_results("MOCK", "T", checklist_id="default_chat", db_path=tmp_db)
    rec = next(x for x in r.payload["results"] if x["conversation_id"] == "mock-ru-001")
    assert rec["status"] == "error" and "other_chat" in rec["error"]


def test_duplicate_conversation_ids_rejected(world, monkeypatch):
    tmp_db, cl = world
    conv = db.get_conversation("MOCK", "mock-ru-001", path=tmp_db)
    run(evaluate_conversation(conv, cl, FakeProvider(make_responder()), db_path=tmp_db))

    import app.results_export as mod
    real_list = db.list_conversations
    def dup_list(group_id, path=None):
        rows = real_list(group_id, path=path)
        return rows + [rows[0]]
    monkeypatch.setattr(mod.db, "list_conversations", dup_list)
    with pytest.raises(ExportError, match="повторяющиеся"):
        build_results("MOCK", "T", db_path=tmp_db)


def test_json_is_valid_utf8_round_trip(world, tmp_path):
    import json
    from app.results_export import write_results
    tmp_db, cl = world
    conv = db.get_conversation("MOCK", "mock-ru-001", path=tmp_db)
    run(evaluate_conversation(conv, cl, FakeProvider(make_responder()), db_path=tmp_db))
    r = build_results("MOCK", "Команда «Альфа»", db_path=tmp_db)
    out = tmp_path / "results.json"
    write_results(r, out)
    text = out.read_text(encoding="utf-8")
    assert "Команда «Альфа»" in text
    loaded = json.loads(text)
    assert loaded == r.payload
