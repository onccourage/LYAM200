import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def default_checklist_dict() -> dict:
    return json.loads((ROOT / "data/checklists/default_chat_v1.json").read_text(encoding="utf-8"))


@pytest.fixture
def mock_payload() -> dict:
    return json.loads((FIXTURES / "mock_dataset.json").read_text(encoding="utf-8"))


@pytest.fixture
def tmp_db(tmp_path):
    from app import db
    p = tmp_path / "test.db"
    db.init_db(p)
    return p


@pytest.fixture
def api_client(tmp_path, monkeypatch, mock_payload):
    """TestClient на временной БД с загруженным набором MOCK и подставной моделью."""
    from fastapi.testclient import TestClient

    from app import config, main
    from app.llm import FakeProvider
    from tests.helpers import make_responder

    monkeypatch.setattr(config.settings, "db_path", tmp_path / "api.db")
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    monkeypatch.setattr(config.settings, "dataset_api_url", "")
    monkeypatch.setattr(config.settings, "anthropic_api_key", "test")
    (tmp_path / "cache").mkdir()
    (tmp_path / "checklists").mkdir()
    src = ROOT / "data/checklists/default_chat_v1.json"
    (tmp_path / "checklists/default_chat_v1.json").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "cache/MOCK.json").write_text(json.dumps(mock_payload), encoding="utf-8")
    fake = FakeProvider(make_responder())
    monkeypatch.setattr(main, "get_provider", lambda: fake)
    monkeypatch.setattr(main.batches, "provider_factory", lambda: fake)
    with TestClient(main.app) as client:
        assert client.post("/api/datasets/MOCK/load").status_code == 200
        client.fake = fake
        yield client
