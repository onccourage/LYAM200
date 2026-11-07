import asyncio

import pytest

from app import db
from app.appeals import AppealError, appeal_details, create_appeal, resolve_appeal
from app.dataset import load_upload
from app.evaluator import evaluate_conversation
from app.llm import FakeProvider
from app.models import Checklist
from tests.helpers import make_responder

OP = {"evidence": [{"message_id": "m5", "quote": "Вывод заблокирован"}]}


@pytest.fixture
def ev(tmp_db, mock_payload, default_checklist_dict):
    load_upload(mock_payload, "MOCK", db_path=tmp_db)
    cl = db.save_checklist_version(Checklist.model_validate(default_checklist_dict), path=tmp_db)
    conv = db.get_conversation("MOCK", "mock-ru-001", path=tmp_db)
    p = FakeProvider(make_responder({"c06": {"answer": "7", **OP}, "c04": {"answer": "bad"}}))
    rec = asyncio.run(evaluate_conversation(conv, cl, p, db_path=tmp_db)).record
    assert rec["total"] == "94.38" and rec["score"]["pending"] == ["c04"]   # 75.5/80
    return tmp_db, rec, p


def test_change_rescores_without_llm(ev):
    path, rec, p = ev
    calls = len(p.requests)
    a = create_appeal(rec["id"], "c06", "Оператор предупредил о паузе в m4", "контролёр Аня", path=path)
    assert a["status"] == "open" and a["original_answer"] == "7" and a["original_points"] == "10.5" and a["original_total"] == "94.38"
    r = resolve_appeal(a["id"], "changed", "Предупреждение было", "Проверяющий Б", new_answer="10", path=path)
    assert r["status"] == "resolved" and r["new_answer"] == "10" and r["new_points"] == "15" and r["final_total"] == "100.00"
    e = db.get_evaluation(rec["id"], path=path)
    assert e["total"] == "100.00" and e["original_total"] == "94.38"
    v = next(x for x in e["answers"] if x["criterion_id"] == "c06")
    assert v["source"] == "appeal" and v["original_answer"] == "7" and v["answer"] == "10"
    assert len(p.requests) == calls   # без LLM


def test_confirm_keeps_score(ev):
    path, rec, _ = ev
    a = create_appeal(rec["id"], "c01", "Не согласен", path=path)
    r = resolve_appeal(a["id"], "confirmed", "Оценка верна", "Б", path=path)
    assert r["decision"] == "confirmed" and r["new_answer"] == "yes" and r["final_total"] == "94.38"
    assert db.get_evaluation(rec["id"], path=path)["total"] == "94.38"


def test_needs_review_resolved_by_appeal(ev):
    path, rec, _ = ev
    a = create_appeal(rec["id"], "c04", "Проверьте вручную", path=path)
    assert a["original_answer"] is None
    with pytest.raises(AppealError, match="подтверждать нечего"):
        resolve_appeal(a["id"], "confirmed", "x", path=path)
    r = resolve_appeal(a["id"], "changed", "Консультация полная", new_answer=20, path=path)
    e = db.get_evaluation(rec["id"], path=path)
    assert e["status"] == "done" and e["score"]["pending"] == [] and e["total"] == "95.50" and r["final_total"] == "95.50"


@pytest.mark.parametrize("cid,answer,msg", [
    ("c01", "na", "N/A не разрешён"),
    ("c06", "5", "не является ступенью"),
    ("c01", "10", "yes/no"),
    ("c08", "yes", "тип нарушения"),
    ("c01", "yes", "совпадает"),
])
def test_invalid_new_values(ev, cid, answer, msg):
    path, rec, _ = ev
    a = create_appeal(rec["id"], cid, "x", path=path)
    with pytest.raises(AppealError, match=msg):
        resolve_appeal(a["id"], "changed", "причина", new_answer=answer, path=path)
    assert db.get_appeal(a["id"], path=path)["status"] == "open"


def test_rules(ev):
    path, rec, _ = ev
    with pytest.raises(AppealError, match="обязателен"):
        create_appeal(rec["id"], "c01", "  ", path=path)
    with pytest.raises(AppealError, match="нет в чек-листе"):
        create_appeal(rec["id"], "c99", "x", path=path)
    a = create_appeal(rec["id"], "c01", "x", path=path)
    with pytest.raises(AppealError, match="уже есть открытая") as e:
        create_appeal(rec["id"], "c01", "y", path=path)
    assert e.value.status == 409
    with pytest.raises(AppealError, match="Причина"):
        resolve_appeal(a["id"], "confirmed", " ", path=path)
    resolve_appeal(a["id"], "confirmed", "ok", path=path)
    with pytest.raises(AppealError, match="уже рассмотрена"):
        resolve_appeal(a["id"], "confirmed", "ok", path=path)
    create_appeal(rec["id"], "c01", "повторно", path=path)   # после решения можно подать снова


def test_flag_change_sets_gross_without_zeroing(ev):
    path, rec, _ = ev
    a = create_appeal(rec["id"], "c08", "Оператор просил перевести деньги ему", path=path)
    resolve_appeal(a["id"], "changed", "Есть предложение в обход", new_answer="yes", new_gross_types=["bypass_service"], path=path)
    e = db.get_evaluation(rec["id"], path=path)
    assert e["gross_error"] is True and e["total"] == "94.38" and e["score"]["gross_types"] == ["bypass_service"]


def test_na_allowed_change_and_details(ev):
    path, rec, _ = ev
    a = create_appeal(rec["id"], "c03", "Идентификация не требовалась", path=path)
    resolve_appeal(a["id"], "changed", "Общий вопрос", new_answer="na", path=path)
    e = db.get_evaluation(rec["id"], path=path)
    assert e["score"]["applicable_weight"] == "60" and e["total"] == "92.50"   # 55.5/60
    d = appeal_details(a["id"], path=path)
    assert d["criterion"]["id"] == "c03" and d["conversation"]["messages"][0]["id"] == "m0"
    assert d["history"][0]["id"] == a["id"] and d["evaluation"]["original_total"] == "94.38"


def test_stale_evaluation_rejected(ev):
    path, rec, p = ev
    conv = db.get_conversation("MOCK", "mock-ru-001", path=path)
    cl = db.get_checklist("default_chat", path=path)
    asyncio.run(evaluate_conversation(conv, cl, p, db_path=path))
    with pytest.raises(AppealError, match="устарела"):
        create_appeal(rec["id"], "c01", "x", path=path)
