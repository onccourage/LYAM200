"""Дробление критериев на группы по llm_criteria_chunk_size (Anthropic отклоняет большую схему
ответа — "compiled grammar is too large" — на ~6+ разнородных критериях в одном запросе; проверено
на реальном API, порог воспроизведён в app/evaluator.py::_chunk, настройка — LLM_CRITERIA_PER_CALL).

Тесты пина ют маленький размер группы на дефолтном чек-листе (8 критериев), чтобы проверить само
дробление; остальные тесты пакета держат большой размер (conftest.py), чтобы не зависеть от него.
"""
import asyncio

import pytest

from app import db
from app.config import settings
from app.dataset import load_upload
from app.evaluator import evaluate_conversation
from app.llm import FakeProvider, LLMError
from app.models import Checklist
from tests.helpers import make_responder


@pytest.fixture
def world(tmp_db, mock_payload, default_checklist_dict, monkeypatch):
    monkeypatch.setattr(settings, "llm_criteria_chunk_size", 3)
    load_upload(mock_payload, "MOCK", db_path=tmp_db)
    cl = db.save_checklist_version(Checklist.model_validate(default_checklist_dict), path=tmp_db)
    return cl, db.get_conversation("MOCK", "mock-ru-001", path=tmp_db)


def run(coro):
    return asyncio.run(coro)


def test_splits_into_groups_of_configured_size(world, tmp_db):
    cl, conv = world
    p = FakeProvider(make_responder())
    out = run(evaluate_conversation(conv, cl, p, db_path=tmp_db))
    r = out.record
    # 8 критериев / группы по 3 → 3 вызова (3+3+2), все 8 отвечены, ни один не ушёл в одном запросе
    assert r["llm_calls"] == 3 and len(p.requests) == 3
    sizes = sorted(len(req["schema"]["properties"]) for req in p.requests)
    assert sizes == [2, 3, 3]
    assert all(a["status"] == "ok" for a in r["answers"]) and r["status"] == "done" and r["total"] == "100.00"
    seen = {cid for req in p.requests for cid in req["schema"]["properties"]}
    assert seen == {f"c0{i}" for i in range(1, 9)}


class ChunkFailer(FakeProvider):
    """Вызов с указанным критерием в схеме — отказ N раз подряд, остальные группы отвечают как обычно."""

    def __init__(self, responder, fail_crit_id, fail_times, model="fake-model"):
        super().__init__(responder, model=model)
        self.fail_crit_id = fail_crit_id
        self.fail_times = fail_times

    async def complete_json(self, system, user, schema, *, sample=0):
        if self.fail_crit_id in schema["properties"] and self.fail_times > 0:
            self.fail_times -= 1
            self.requests.append({"system": system, "user": user, "schema": schema, "sample": sample})
            raise LLMError("API 529: overloaded")
        return await super().complete_json(system, user, schema, sample=sample)


def test_one_chunk_failing_does_not_block_others(world, tmp_db):
    """Группы по 3: [c01,c02,c03] [c04,c05,c06] [c07,c08] — отказ в группе с c06 валит ВСЮ эту группу
    (один HTTP-вызов на группу), но не затрагивает соседние группы — в этом и ценность дробления."""
    cl, conv = world
    p = ChunkFailer(make_responder(), fail_crit_id="c06", fail_times=99)   # группа [c04,c05,c06] всегда недоступна
    out = run(evaluate_conversation(conv, cl, p, db_path=tmp_db))
    r = out.record
    by_id = {a["criterion_id"]: a for a in r["answers"]}
    assert {c: by_id[c]["status"] for c in ("c04", "c05", "c06")} == {"c04": "needs_review", "c05": "needs_review", "c06": "needs_review"}
    assert all(by_id[c]["status"] == "ok" for c in ("c01", "c02", "c03", "c07", "c08"))
    assert r["status"] == "partial" and sorted(r["score"]["pending"]) == ["c04", "c05", "c06"]
    assert r["total"] is not None   # остальные критерии посчитаны несмотря на упавшую соседнюю группу


def test_retry_only_targets_failed_chunk_not_whole_set(world, tmp_db):
    cl, conv = world
    p = ChunkFailer(make_responder(), fail_crit_id="c06", fail_times=1)   # первый вызов группы падает, повтор — ок
    out = run(evaluate_conversation(conv, cl, p, db_path=tmp_db))
    r = out.record
    assert r["status"] == "done" and all(a["status"] == "ok" for a in r["answers"])
    retried = [req for req in p.requests if req.get("schema") and set(req["schema"]["properties"]) == {"c04", "c05", "c06"}]
    assert len(retried) == 2   # неудачная попытка + повтор — только эта группа, не c01..c03/c07/c08 заново
    assert not any(set(req["schema"]["properties"]) == {f"c0{i}" for i in range(1, 9)} for req in p.requests)


def test_consensus_across_chunks(world, tmp_db):
    cl, conv = world
    base = make_responder()

    def responder(system, user, schema, sample):
        data = base(system, user, schema)
        if "c06" in data:
            data["c06"].update(answer=["10", "7", "10"][sample], evidence=data["c06"]["evidence"] or [])
        return data

    p = FakeProvider(responder)
    out = run(evaluate_conversation(conv, cl, p, db_path=tmp_db, samples=3))
    r = out.record
    # 3 группы × 3 прохода = 9 вызовов
    assert r["llm_calls"] == 9 and len(p.requests) == 9
    v = next(a for a in r["answers"] if a["criterion_id"] == "c06")
    assert v["answer"] == "10" and v["votes"] == {"10": 2, "7": 1} and v["samples"] == 3
    other = next(a for a in r["answers"] if a["criterion_id"] == "c01")
    assert other["samples"] == 3 and other["agreement"] == 1.0
