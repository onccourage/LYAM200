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
