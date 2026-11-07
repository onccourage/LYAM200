"""Раздел 9: замена критерия без правки кода.

Эмулирует контрольный час: набор оценён v1 → через API (как из UI) c05 заменяется новым критерием
другого типа (scale 0/2/4/6, N/A, вес 15) → новая версия применяется к набору → только новый критерий
уходит в LLM, итоги пересчитаны, старые оценки не изменились. Плюс все сценарии замены из списка.
"""
import asyncio
import copy
import time
from decimal import Decimal

import pytest

from app import db
from app.checklists import diff
from app.dataset import load_upload
from app.evaluator import evaluate_conversation
from app.llm import FakeProvider
from app.models import Checklist
from app.preprocess import preprocess
from app.prompt_builder import build_response_schema, build_system_prompt
from app.scoring import ScoringError, score
from app.validator import validate_criterion
from tests.helpers import make_responder

NEW_C09 = {
    "id": "c09", "code": "05", "section": "conduct_closing",
    "title": "Проактивность: предложил полезное по теме",
    "description": "Оператор сам предложил релевантную информацию или следующий шаг сверх прямого вопроса.",
    "type": "scale", "weight": 15, "na_allowed": True,
    "na_condition": "Клиент завершил диалог сразу после ответа на свой вопрос.",
    "scale": [{"value": 6, "label": "Предложил сам, по делу, понятно"},
              {"value": 4, "label": "Предложил, но частично или общо"},
              {"value": 2, "label": "Намёк без конкретики"},
              {"value": 0, "label": "Ничего не предложил при явной возможности"}],
    "guidance": "Оцени, было ли предложение по инициативе оператора, а не в ответ на прямой вопрос.",
    "channels": ["agent chat", "voice call"], "requires_audio": False, "enabled": True,
}


def wait_job(client, job_id, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/batches/{job_id}").json()
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.05)
    raise AssertionError("batch timeout")


def test_control_hour_replace_c05_via_api(api_client):
    c = api_client
    fake = c.fake
    # 1. Набор оценён исходной версией
    j = wait_job(c, c.post("/api/batches", json={"group_id": "MOCK"}).json()["id"])
    assert j["succeeded"] == 4
    old_evals = {e["conversation_id"]: e for e in c.get("/api/datasets/MOCK/evaluations").json()}
    assert {e["checklist_version"] for e in old_evals.values()} == {1}

    # 2. Редактор: получить v1, заменить c05 на новый критерий, проверить и сохранить новую версию
    v1 = c.get("/api/checklists/default_chat").json()["checklist"]
    body = copy.deepcopy(v1)
    body["criteria"] = [NEW_C09 if x["id"] == "c05" else x for x in body["criteria"]]
    val = c.post("/api/checklists/validate", json=body).json()
    assert val["total_weight"] == 100 and val["warnings"] == []
    saved = c.post("/api/checklists", json=body).json()["checklist"]
    assert saved["version"] == 2
    d = c.get("/api/checklists/default_chat/diff", params={"from_version": 1, "to_version": 2}).json()["changes"]
    assert {(x["criterion_id"], x["change"], x["llm"]) for x in d} == {("c09", "added", True), ("c05", "removed", False)}

    # 3. Применить к набору: в LLM уходит только c09
    calls_before = len(fake.requests)
    j = wait_job(c, c.post("/api/batches", json={"group_id": "MOCK", "checklist_version": 2}).json()["id"])
    assert j["succeeded"] == 4 and j["failed"] == 0
    new_requests = fake.requests[calls_before:]
    assert len(new_requests) == 4
    for r in new_requests:
        assert list(r["schema"]["properties"]) == ["c09"]
        assert r["schema"]["properties"]["c09"]["properties"]["answer"]["enum"] == ["6", "4", "2", "0", "na"]
        assert "[c09] Проактивность" in r["system"] and "6 — Предложил сам" in r["system"]
        assert "[c05]" not in r["system"] and "Эмпатия" not in r["system"]

    # 4. Данные для UI: оценка ссылается на v2, c09 в ответах и в подсчёте, c05 нет
    ev = c.get("/api/evaluations/MOCK/mock-ru-001").json()
    assert ev["checklist_version"] == 2 and ev["total"] == "100.00"
    ids = [a["criterion_id"] for a in ev["answers"]]
    assert "c09" in ids and "c05" not in ids
    row = next(x for x in ev["score"]["criteria"] if x["criterion_id"] == "c09")
    assert row["points_display"] == "15" and row["weight"] == "15"
    assert next(a for a in ev["answers"] if a["criterion_id"] == "c09")["source"] == "llm"
    assert next(a for a in ev["answers"] if a["criterion_id"] == "c01")["source"] == "cache"
    ui_cl = c.get("/api/checklists/default_chat", params={"version": 2}).json()["checklist"]
    assert any(x["id"] == "c09" and x["type"] == "scale" for x in ui_cl["criteria"])

    # 5. Старые оценки не изменились задним числом
    old = db.get_evaluation(old_evals["mock-ru-001"]["id"])
    assert old["checklist_version"] == 1 and old["total"] == old_evals["mock-ru-001"]["total"] and not old["is_current"]
    assert any(a["criterion_id"] == "c05" for a in old["answers"])

    # 6. Повторное применение той же версии — ничего не вызывает
    calls = len(fake.requests)
    j = wait_job(c, c.post("/api/batches", json={"group_id": "MOCK", "checklist_version": 2}).json()["id"])
    assert j["skipped"] == 4 and len(fake.requests) == calls


# ---------------------------------------------------------------- сценарии замены (раздел 9, п.5)

@pytest.fixture
def base(default_checklist_dict):
    return copy.deepcopy(default_checklist_dict)


@pytest.fixture
def prepared(mock_payload):
    from app.dataset import parse_record
    return {
        "chat": preprocess(parse_record(mock_payload["data"]["records"][0], "MOCK")),
        "call": preprocess(parse_record(mock_payload["data"]["records"][3], "MOCK")),
    }


def mutate(d, cid, **changes):
    d = copy.deepcopy(d)
    d["criteria"] = [dict(x, **changes) if x["id"] == cid else x for x in d["criteria"]]
    return Checklist.model_validate(d)


MAX = {"c01": "yes", "c02": "yes", "c03": "yes", "c04": 20, "c05": "yes", "c06": 10, "c07": "yes", "c08": "no"}


def item(answer, mid="m2", quote="Меня зовут Айгерим", **kw):
    return {"evidence": [{"message_id": mid, "quote": quote}], "checked_range": None, "rationale": "r",
            "na_reason": None, "answer": answer, "confidence": 0.9, **kw}


def test_binary_to_scale(base, prepared):
    cl = mutate(base, "c01", type="scale", scale=[{"value": 3, "label": "a"}, {"value": 1, "label": "b"}, {"value": 0, "label": "c"}])
    c = cl.criterion("c01")
    assert "Допустимые ответы: 3, 1, 0." in build_system_prompt([c])
    assert build_response_schema([c])["properties"]["c01"]["properties"]["answer"]["enum"] == ["3", "1", "0"]
    assert validate_criterion(c, item("yes"), prepared["chat"])[0] is None
    assert validate_criterion(c, item("1"), prepared["chat"])[0].answer == "1"
    r = score(cl, {**MAX, "c01": 1})
    assert next(x for x in r.criteria if x.criterion_id == "c01").points == Decimal(10) / 3
    assert str(r.total) == "93.33"   # (90 + 10/3) / 100


def test_scale_to_binary(base, prepared):
    cl = mutate(base, "c04", type="binary", scale=[])
    assert build_response_schema([cl.criterion("c04")])["properties"]["c04"]["properties"]["answer"]["enum"] == ["yes", "no", "na"]
    assert str(score(cl, {**MAX, "c04": "no"}).total) == "80.00"
    with pytest.raises(ScoringError):
        score(cl, {**MAX, "c04": 20})


def test_new_scale_steps(base):
    cl = mutate(base, "c06", scale=[{"value": 5, "label": "x"}, {"value": 3, "label": "y"}, {"value": 0, "label": "z"}])
    assert str(score(cl, {**MAX, "c06": 3}).total) == "94.00"   # 15*3/5 = 9 → 94
    with pytest.raises(ScoringError, match="ступенью"):
        score(cl, {**MAX, "c06": 7})


def test_weight_change_no_llm(base, mock_payload, tmp_db):
    from app.dataset import parse_record
    conv = parse_record(mock_payload["data"]["records"][0], "MOCK")
    p = FakeProvider(make_responder({"c01": {"answer": "no", "evidence": [], "checked_range": {"from": "m0", "to": "m2"}}}))
    v1 = Checklist.model_validate(base)
    asyncio.run(evaluate_conversation(conv, v1, p, db_path=tmp_db))
    v2 = mutate(base, "c01", weight=30)
    assert [x["llm"] for x in diff(v1, v2)] == [False]
    out = asyncio.run(evaluate_conversation(conv, v2, p, db_path=tmp_db))
    assert out.llm_criteria == [] and len(p.requests) == 1
    assert out.record["total"] == "75.00"   # 90/120


def test_allow_and_forbid_na(base, prepared):
    allow = mutate(base, "c01", na_allowed=True, na_condition="Первым ответил другой оператор после перевода.")
    assert validate_criterion(allow.criterion("c01"), item("na", na_reason="перевод"), prepared["chat"])[0].answer == "na"
    assert "N/A разрешён. Условие неприменимости: Первым ответил" in build_system_prompt([allow.criterion("c01")])
    forbid = mutate(base, "c02", na_allowed=False)
    assert "na" not in build_response_schema([forbid.criterion("c02")])["properties"]["c02"]["properties"]["answer"]["enum"]
    assert validate_criterion(forbid.criterion("c02"), item("na", na_reason="x"), prepared["chat"])[0] is None
    with pytest.raises(ScoringError, match="N/A не разрешён"):
        score(forbid, {**MAX, "c02": "na"})
    # смена na_allowed меняет определение → критерий будет переоценён LLM
    assert diff(Checklist.model_validate(base), forbid)[0]["llm"] is True


def test_calls_only_criterion(base, mock_payload, tmp_db):
    from app.dataset import parse_record
    cl = mutate(base, "c05", channels=["voice call"])
    chat = parse_record(mock_payload["data"]["records"][0], "MOCK")
    call = parse_record(mock_payload["data"]["records"][3], "MOCK")
    p = FakeProvider(make_responder())
    r_chat = asyncio.run(evaluate_conversation(chat, cl, p, db_path=tmp_db)).record
    assert "c05" not in p.requests[-1]["schema"]["properties"]
    row = next(x for x in r_chat["score"]["criteria"] if x["criterion_id"] == "c05")
    assert row["status"] == "na_channel" and r_chat["total"] == "100.00"
    asyncio.run(evaluate_conversation(call, cl, p, db_path=tmp_db))
    assert "c05" in p.requests[-1]["schema"]["properties"]


def test_requires_audio_criterion(base, mock_payload, tmp_db):
    from app.dataset import parse_record
    d = copy.deepcopy(base)
    d["criteria"].append({"id": "c10", "section": "conduct_closing", "title": "Дикция и интонация", "type": "scale",
                          "weight": 10, "na_allowed": True, "na_condition": "Нет записи звонка",
                          "scale": [{"value": 2, "label": "чётко"}, {"value": 1, "label": "местами"}, {"value": 0, "label": "неразборчиво"}],
                          "requires_audio": True})
    cl = Checklist.model_validate(d)
    p = FakeProvider(make_responder())
    chat = parse_record(mock_payload["data"]["records"][0], "MOCK")
    r = asyncio.run(evaluate_conversation(chat, cl, p, db_path=tmp_db)).record
    v = next(a for a in r["answers"] if a["criterion_id"] == "c10")
    assert v["answer"] == "na" and v["source"] == "rule" and "c10" not in p.requests[-1]["schema"]["properties"]
    call = parse_record(mock_payload["data"]["records"][3], "MOCK")
    r = asyncio.run(evaluate_conversation(call, cl, p, db_path=tmp_db)).record
    v = next(a for a in r["answers"] if a["criterion_id"] == "c10")
    assert v["status"] == "needs_review" and "прослушивание" in v["review_reason"]


def test_new_flag_criterion_with_own_types(base, prepared, mock_payload, tmp_db):
    from app.dataset import parse_record
    d = copy.deepcopy(base)
    d["criteria"].append({"id": "c11", "section": "gross", "title": "Обещание гарантированной доходности", "type": "flag",
                          "flag_types": [{"id": "guaranteed_return", "label": "обещал гарантированный доход"},
                                         {"id": "pressure", "label": "давил на клиента"}],
                          "guidance": "Флаг, если оператор гарантирует доходность инвестиций."})
    cl = Checklist.model_validate(d)
    c11 = cl.criterion("c11")
    assert "guaranteed_return — обещал гарантированный доход" in build_system_prompt([c11])
    assert build_response_schema([c11])["properties"]["c11"]["properties"]["gross_types"]["items"]["enum"] == ["guaranteed_return", "pressure"]
    bad = validate_criterion(c11, item("yes", mid="m5", quote="Вывод заблокирован", gross_types=["insult"]), prepared["chat"])
    assert bad[0] is None and "неизвестные gross_types" in bad[1][0]
    ok = validate_criterion(c11, item("yes", mid="m5", quote="Вывод заблокирован", gross_types=["pressure"]), prepared["chat"])[0]
    assert ok.gross_types == ["pressure"]
    conv = parse_record(mock_payload["data"]["records"][0], "MOCK")
    p = FakeProvider(make_responder({"c11": {"answer": "yes", "gross_types": ["guaranteed_return"],
                                             "evidence": [{"message_id": "m5", "quote": "Загрузите документ"}]}}))
    r = asyncio.run(evaluate_conversation(conv, cl, p, db_path=tmp_db)).record
    assert r["gross_error"] is True and r["total"] == "100.00" and r["score"]["gross_types"] == ["guaranteed_return"]


def test_seed_flag_types_come_from_config(base):
    """Подтипы грубых ошибок c08 — данные чек-листа, а не код."""
    cl = Checklist.model_validate(base)
    assert cl.criterion("c08").flag_type_ids == ["insult", "bypass_service", "confidential_disclosure"]
    d = copy.deepcopy(base)
    d["criteria"] = [dict(x, flag_types=[]) if x["id"] == "c08" else x for x in d["criteria"]]
    no_types = Checklist.model_validate(d).criterion("c08")
    assert "gross_types" not in build_response_schema([no_types])["properties"]["c08"]["properties"]
    assert str(score(Checklist.model_validate(d), {**MAX, "c08": "yes"}).total) == "100.00"
