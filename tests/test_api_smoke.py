import json

from fastapi.testclient import TestClient


def test_api_smoke(tmp_path, monkeypatch, mock_payload):
    from app import config
    monkeypatch.setattr(config.settings, "db_path", tmp_path / "api.db")
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    (tmp_path / "cache").mkdir()
    (tmp_path / "checklists").mkdir()
    src = config.ROOT_DIR / "data/checklists/default_chat_v1.json"
    (tmp_path / "checklists/default_chat_v1.json").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "cache/MOCK.json").write_text(json.dumps(mock_payload), encoding="utf-8")
    monkeypatch.setattr(config.settings, "dataset_api_url", "")

    from app.main import app
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"ok": True}
        assert "anthropic_api_key" not in client.get("/api/config").json()
        assert client.get("/").status_code == 200

        r = client.post("/api/datasets/MOCK/load")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source"] == "cache" and body["total"] == 4

        convs = client.get("/api/datasets/MOCK/conversations").json()
        assert len(convs) == 4 and all(c["evaluation"] is None for c in convs)
        assert next(c for c in convs if c["conversation_id"] == "mock-ru-001")["preview"] == "Не могу вывести деньги со счёта"
        assert client.get("/api/evaluations/MOCK/mock-ru-001").status_code == 404
        assert client.post("/api/evaluations/MOCK/mock-ru-001").status_code in (503, 200)
        assert client.get("/js/app.js").status_code == 200 and client.get("/css/styles.css").status_code == 200
        conv = client.get("/api/conversations/MOCK/mock-call-001").json()
        assert conv["messages"][0]["timestamp"] == "2026-11-06T10:15:02+05:00"

        assert client.post("/api/datasets/NOPE/load").status_code == 502

        cl = client.get("/api/checklists/default_chat").json()
        assert cl["checklist"]["version"] == 1 and cl["warnings"] == []
        new = cl["checklist"]
        new["criteria"][4]["weight"] = 20
        r = client.post("/api/checklists", json=new)
        assert r.status_code == 200 and r.json()["checklist"]["version"] == 2
        assert r.json()["warnings"]  # сумма весов 105
        new["criteria"][7]["weight"] = 3  # flag с весом — ошибка
        r = client.post("/api/checklists", json=new)
        assert r.status_code == 422 and "flag" in json.dumps(r.json(), ensure_ascii=False)
        assert len(client.get("/api/checklists/default_chat/versions").json()) == 2

        up = client.post("/api/conversations/upload",
                         files={"file": ("r.json", json.dumps(mock_payload["data"]["records"][2]), "application/json")})
        assert up.status_code == 200 and up.json()["group_id"] == "MANUAL"
