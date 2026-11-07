import json
import time

from fastapi.testclient import TestClient

from app.llm import FakeProvider
from tests.helpers import make_responder


def test_batch_api_flow(tmp_path, monkeypatch, mock_payload):
    from app import config
    monkeypatch.setattr(config.settings, "db_path", tmp_path / "api.db")
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    (tmp_path / "cache").mkdir()
    (tmp_path / "checklists").mkdir()
    src = config.ROOT_DIR / "data/checklists/default_chat_v1.json"
    (tmp_path / "checklists/default_chat_v1.json").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "cache/MOCK.json").write_text(json.dumps(mock_payload), encoding="utf-8")
    monkeypatch.setattr(config.settings, "dataset_api_url", "")

    from app import main
    with TestClient(main.app) as client:
        client.post("/api/datasets/MOCK/load")
        monkeypatch.setattr(config.settings, "anthropic_api_key", "")
        assert client.post("/api/batches", json={"group_id": "MOCK"}).status_code == 503

        monkeypatch.setattr(config.settings, "anthropic_api_key", "test")
        monkeypatch.setattr(main.batches, "provider_factory", lambda: FakeProvider(make_responder()))
        assert client.post("/api/batches", json={"group_id": "MOCK", "conversation_ids": ["nope"]}).status_code == 400
        r = client.post("/api/batches", json={"group_id": "MOCK", "conversation_ids": ["mock-ru-001", "mock-en-001"]})
        assert r.status_code == 200, r.text
        job_id = r.json()["id"]
        for _ in range(100):
            j = client.get(f"/api/batches/{job_id}").json()
            if j["status"] == "done":
                break
            time.sleep(0.05)
        assert j["status"] == "done" and j["succeeded"] == 2

        r = client.post("/api/batches", json={"group_id": "MOCK"})
        job2 = r.json()["id"]
        r2 = client.post("/api/batches", json={"group_id": "MOCK"})
        assert r2.status_code in (409, 200)   # 409, если первое ещё идёт
        for _ in range(100):
            j = client.get(f"/api/batches/{job2}").json()
            if j["status"] == "done":
                break
            time.sleep(0.05)
        assert j["skipped"] == 2 and j["succeeded"] == 2

        stats = client.get("/api/datasets/MOCK/stats").json()
        assert stats["evaluated"] == 4 and stats["avg_cost_per_conversation"]
        assert client.get("/api/batches", params={"group_id": "MOCK"}).json()[0]["id"] >= job2
        convs = client.get("/api/datasets/MOCK/conversations").json()
        assert all(c["evaluation"]["total"] == "100.00" for c in convs)
