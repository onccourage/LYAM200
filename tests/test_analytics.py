import asyncio

from app import db
from app.analytics import compute
from app.appeals import create_appeal, resolve_appeal
from app.dataset import load_upload
from app.evaluator import evaluate_conversation
from app.llm import FakeProvider
from app.models import Checklist
from tests.helpers import make_responder

NO = {"answer": "no", "evidence": [], "checked_range": {"from": "m0", "to": "m1"}}


def test_analytics(tmp_db, mock_payload, default_checklist_dict):
    load_upload(mock_payload, "MOCK", db_path=tmp_db)
    cl = db.save_checklist_version(Checklist.model_validate(default_checklist_dict), path=tmp_db)
    ov = {
        "mock-ru-001": {"c06": {"answer": "7"}, "c07": NO},                                     # 90.50
        "mock-kz-001": {"c07": NO, "c03": {"answer": "na", "na_reason": "x", "evidence": []}},   # 75/80 = 93.75
        "mock-en-001": {"c08": {"answer": "yes", "gross_types": ["insult"],
                                "evidence": [{"message_id": "m1", "quote": "Hello"}]}},           # 100, gross
        "mock-call-001": {"c04": {"answer": "bad"}},                                             # partial
    }
    ids = {}
    for c in db.list_conversations("MOCK", path=tmp_db):
        out = asyncio.run(evaluate_conversation(c, cl, FakeProvider(make_responder(ov[c.conversation_id])), db_path=tmp_db))
        ids[c.conversation_id] = out.record["id"]
    a = create_appeal(ids["mock-ru-001"], "c06", "было предупреждение", path=tmp_db)
    resolve_appeal(a["id"], "changed", "да", new_answer="10", path=tmp_db)                      # 90.50 → 95.00
    b = create_appeal(ids["mock-kz-001"], "c07", "x", path=tmp_db)
    resolve_appeal(b["id"], "confirmed", "верно", path=tmp_db)
    create_appeal(ids["mock-en-001"], "c01", "x", path=tmp_db)

    r = compute("MOCK", db_path=tmp_db)
    assert r["evaluated"] == 4 and r["dialogs_total"] == 4
    crit = {c["criterion_id"]: c for c in r["criteria"]}
    assert crit["c07"]["problem"] == 2 and crit["c07"]["applicable"] == 4 and crit["c07"]["problem_rate"] == "50.0"
    assert crit["c06"]["problem_rate"] == "0.0"            # после апелляции 7 → 10
    assert crit["c03"]["na"] == 1 and crit["c03"]["na_rate"] == "25.0"
    assert crit["c04"]["needs_review"] == 1 and crit["c04"]["applicable"] == 3
    assert crit["c08"]["problem_label"] == "нарушение" and crit["c08"]["problem"] == 1
    assert [c["criterion_id"] for c in r["criteria"]] == [f"c0{i}" for i in range(1, 9)]
    assert r["summary"]["gross_dialogs"] == 1 and r["summary"]["gross_rate"] == "25.0"
    assert r["gross"]["by_type"] == [{"type": "insult", "label": "оскорбление клиента", "count": 1}]
    assert r["summary"]["needs_review_dialogs"] == 1
    lang = {x["key"]: x for x in r["by_language"]}
    assert lang["RU"]["dialogs"] == 2 and lang["EN"]["gross"] == 1
    ch = {x["key"]: x for x in r["by_channel"]}
    assert ch["voice call"]["needs_review"] == 1
    assert sum(b["count"] for b in r["histogram"]) == 4
    ap = r["appeals"]
    assert ap["total"] == 3 and ap["open"] == 1 and ap["resolved"] == 2 and ap["changed"] == 1
    assert ap["changed_rate"] == "50.0" and ap["avg_total_shift"] == "4.50"
    assert float(r["cost"]["spent"]) > 0
    assert compute("MOCK", checklist_id="nope", db_path=tmp_db)["evaluated"] == 0
