import asyncio
import json

import httpx

from app import db
from app.batch import run_job
from app.dataset import load_upload
from app.evaluator import evaluate_conversation, merge_votes
from app.llm import FakeProvider, OpenAICompatibleProvider
from app.models import Checklist
from app.validator import Verdict
from tests.helpers import make_responder

OP = [{"message_id": "m5", "quote": "Вывод заблокирован"}]


def sampled(per_sample: dict):
    """per_sample: {criterion: [ответ прохода 0, 1, 2]}"""
    base = make_responder()

    def responder(system, user, schema, sample):
        data = base(system, user, schema)
        for cid, answers in per_sample.items():
            if cid in data:
                a = answers[sample]
                if a == "BROKEN":
                    data[cid]["answer"] = "???"
                else:
                    data[cid].update(answer=a, evidence=OP, confidence=0.9 - 0.1 * sample)
        return data
    return responder


def setup(tmp_db, mock_payload, default_checklist_dict):
    load_upload(mock_payload, "MOCK", db_path=tmp_db)
    cl = db.save_checklist_version(Checklist.model_validate(default_checklist_dict), path=tmp_db)
    return cl, db.get_conversation("MOCK", "mock-ru-001", path=tmp_db)


def test_majority_and_agreement(tmp_db, mock_payload, default_checklist_dict):
    cl, conv = setup(tmp_db, mock_payload, default_checklist_dict)
    p = FakeProvider(sampled({"c06": ["10", "7", "10"], "c04": ["15", "BROKEN", "15"]}))
    out = asyncio.run(evaluate_conversation(conv, cl, p, db_path=tmp_db, samples=3))
    r = out.record
    assert len(p.requests) == 3 and sorted(x["sample"] for x in p.requests) == [0, 1, 2]
    v = {a["criterion_id"]: a for a in r["answers"]}
    assert v["c06"]["answer"] == "10" and v["c06"]["votes"] == {"10": 2, "7": 1} and v["c06"]["agreement"] == 0.667
    assert v["c06"]["confidence"] <= 0.667
    assert v["c04"]["answer"] == "15" and v["c04"]["votes"] == {"15": 2} and v["c04"]["agreement"] == 0.667   # 1 проход сломан
    assert v["c01"]["agreement"] == 1.0 and v["c01"]["samples"] == 3
    assert r["llm_calls"] == 3 and r["samples"] == 3 and r["status"] == "done"
    assert db.get_current_evaluation("MOCK", "mock-ru-001", path=tmp_db)["samples"] == 3


def test_tie_broken_by_confidence():
    a = Verdict(criterion_id="c01", answer="yes", confidence=0.6)
    b = Verdict(criterion_id="c01", answer="no", confidence=0.9, checked_range={"from": "m0", "to": "m1"})
    m = merge_votes([a, b], 2)
    assert m.answer == "no" and m.votes == {"yes": 1, "no": 1} and m.agreement == 0.5
    assert merge_votes([a], 1) is a


def test_consensus_cache_separate_and_batch_does_not_skip_single(tmp_db, mock_payload, default_checklist_dict):
    cl, conv = setup(tmp_db, mock_payload, default_checklist_dict)
    p = FakeProvider(make_responder())
    asyncio.run(evaluate_conversation(conv, cl, p, db_path=tmp_db))                 # 1 проход
    job = db.create_batch_job("MOCK", cl.id, cl.version, ["mock-ru-001"], False, True, path=tmp_db, samples=3)
    j = asyncio.run(run_job(job, p, db_path=tmp_db))
    assert j["skipped"] == 0 and j["succeeded"] == 1 and len(p.requests) == 1 + 3  # не взято из кэша одиночной оценки
    job = db.create_batch_job("MOCK", cl.id, cl.version, ["mock-ru-001"], False, True, path=tmp_db, samples=1)
    assert asyncio.run(run_job(job, p, db_path=tmp_db))["skipped"] == 1            # консенсус покрывает одиночную


def test_openai_temperature_per_sample():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content)["temperature"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": {}})

    p = OpenAICompatibleProvider(model="m", base_url="https://x.test/v1", api_key="k")
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(p.complete_json("s", "u", {}, sample=0))
    asyncio.run(p.complete_json("s", "u", {}, sample=2))
    assert seen == [0, 0.7]
