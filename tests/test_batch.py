import asyncio

import pytest

from app import db
from app.batch import BatchManager, run_job
from app.dataset import load_upload
from app.llm import FakeProvider, LLMError
from tests.helpers import make_responder


@pytest.fixture
def setup(tmp_db, mock_payload, default_checklist_dict):
    from app.models import Checklist
    load_upload(mock_payload, "MOCK", db_path=tmp_db)
    cl = db.save_checklist_version(Checklist.model_validate(default_checklist_dict), path=tmp_db)
    ids = [c.conversation_id for c in db.list_conversations("MOCK", path=tmp_db)]
    return tmp_db, cl, ids


class SlowFake(FakeProvider):
    """Считает одновременные вызовы."""
    def __init__(self, *a, fail_times=0, **kw):
        super().__init__(*a, **kw)
        self.active = self.max_active = 0
        self.fail_times = fail_times

    async def complete_json(self, system, user, schema):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)
            if self.fail_times > 0:
                self.fail_times -= 1
                raise LLMError("API 529: overloaded")
            return await super().complete_json(system, user, schema)
        finally:
            self.active -= 1


def _job(path, cl, ids, full=False):
    return db.create_batch_job("MOCK", cl.id, cl.version, ids, full, True, path=path)


def test_batch_runs_all_with_concurrency_limit(setup):
    path, cl, ids = setup
    p = SlowFake(make_responder())
    j = asyncio.run(run_job(_job(path, cl, ids), p, db_path=path, concurrency=2))
    assert j["status"] == "done" and j["processed"] == 4 and j["succeeded"] == 4 and j["failed"] == 0
    assert p.max_active == 2
    assert j["llm_calls"] == 4 and float(j["cost"]) > 0
    assert len(db.list_current_evaluations("MOCK", path=path)) == 4


def test_resume_skips_already_evaluated(setup):
    path, cl, ids = setup
    asyncio.run(run_job(_job(path, cl, ids[:2]), FakeProvider(make_responder()), db_path=path))
    p = FakeProvider(make_responder())
    j = asyncio.run(run_job(_job(path, cl, ids), p, db_path=path))
    assert j["skipped"] == 2 and j["succeeded"] == 2 and len(p.requests) == 2


def test_full_rerun_ignores_cache_and_resume_of_full_keeps_progress(setup):
    path, cl, ids = setup
    asyncio.run(run_job(_job(path, cl, ids), FakeProvider(make_responder()), db_path=path))
    p = FakeProvider(make_responder())
    job_id = _job(path, cl, ids, full=True)
    j = asyncio.run(run_job(job_id, p, db_path=path))
    assert j["succeeded"] == 4 and len(p.requests) == 4
    p2 = FakeProvider(make_responder())
    j = asyncio.run(run_job(job_id, p2, db_path=path))   # «продолжить» завершённое полное задание
    assert j["skipped"] == 4 and len(p2.requests) == 0


def test_model_unavailable_is_retried_then_succeeds(setup):
    path, cl, ids = setup
    # первые 2 вызова падают (основной + повтор внутри evaluator) → диалог status=error → повтор пакета
    p = SlowFake(make_responder(), fail_times=2)
    j = asyncio.run(run_job(_job(path, cl, ids[:1]), p, db_path=path, retries=2, retry_base_sec=0))
    assert j["succeeded"] == 1 and j["failed"] == 0


def test_permanent_failure_counted_and_not_saved(setup):
    path, cl, ids = setup
    p = SlowFake(make_responder(), fail_times=100)
    j = asyncio.run(run_job(_job(path, cl, ids[:2]), p, db_path=path, retries=1, retry_base_sec=0))
    assert j["failed"] == 2 and len(j["errors"]) == 2 and "529" in j["errors"][0]["error"]
    assert db.list_current_evaluations("MOCK", path=path) == []
    # после восстановления API «продолжить» доделывает
    j = asyncio.run(run_job(j["id"], FakeProvider(make_responder()), db_path=path))
    assert j["succeeded"] == 2


def test_new_checklist_version_applies_incrementally(setup, default_checklist_dict):
    from app.models import Checklist
    path, cl, ids = setup
    asyncio.run(run_job(_job(path, cl, ids), FakeProvider(make_responder()), db_path=path))
    d = dict(default_checklist_dict)
    d["criteria"] = [dict(c, title="Новый критерий", guidance="другое") if c["id"] == "c05" else c for c in d["criteria"]]
    v2 = db.save_checklist_version(Checklist.model_validate(d), path=path)
    p = FakeProvider(make_responder())
    j = asyncio.run(run_job(_job(path, v2, ids), p, db_path=path))
    assert j["succeeded"] == 4 and len(p.requests) == 4
    assert all(list(r["schema"]["properties"]) == ["c05"] for r in p.requests)
    assert {e["checklist_version"] for e in db.list_current_evaluations("MOCK", path=path)} == {2}


def test_manager_cancel_and_interrupted_on_restart(setup):
    path, cl, ids = setup

    async def scenario():
        slow = SlowFake(make_responder())
        m = BatchManager(provider_factory=lambda: slow, db_path=path)
        job_id = m.submit("MOCK", cl, ids)
        assert m.active_for("MOCK") == job_id
        m.cancel(job_id)
        await m.tasks[job_id]
        return db.get_batch_job(job_id, path=path)

    j = asyncio.run(scenario())
    assert j["status"] == "cancelled" and j["processed"] <= 4

    jid = _job(path, cl, ids)
    db.update_batch_job(jid, path=path, status="running")
    assert db.mark_running_jobs_interrupted(path=path) == 1
    assert db.get_batch_job(jid, path=path)["status"] == "interrupted"


def test_dataset_stats(setup):
    path, cl, ids = setup
    asyncio.run(run_job(_job(path, cl, ids), FakeProvider(make_responder()), db_path=path))
    s = db.dataset_stats("MOCK", path=path)
    assert s["evaluated"] == 4 and s["conversations"] == 4 and s["avg_total"] == "100.00"
    assert float(s["spent"]) > 0 and s["avg_cost_per_conversation"] is not None and s["avg_tokens_in"] > 0


def test_fatal_quota_stops_batch(setup):
    path, cl, ids = setup

    class Quota(FakeProvider):
        async def complete_json(self, *a):
            self.requests.append(a)
            raise LLMError("квота провайдера исчерпана (429)", fatal=True)

    p = Quota(make_responder())
    j = asyncio.run(run_job(_job(path, cl, ids), p, db_path=path, concurrency=1, retries=3, retry_base_sec=0))
    assert j["status"] == "failed" and len(p.requests) == 1          # ни повторов, ни остальных диалогов
    assert "квота" in j["errors"][-1]["error"] and "Продолжить" in j["errors"][-1]["error"]
    j = asyncio.run(run_job(j["id"], FakeProvider(make_responder()), db_path=path))
    assert j["status"] == "done" and j["succeeded"] == 4


def test_quota_detection():
    from app.llm import quota_exhausted
    assert quota_exhausted(429, '{"retryDelay": "79301s"}')
    assert quota_exhausted(429, '"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"')
    assert not quota_exhausted(429, '{"retryDelay": "20s"}')
    assert not quota_exhausted(500, '{"retryDelay": "79301s"}')
