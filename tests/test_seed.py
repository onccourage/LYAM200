import asyncio
import json

from app import db
from app.dataset import load_upload
from app.evaluator import evaluate_conversation
from app.llm import FakeProvider
from app.models import Checklist
from app.seed import autoload, export_seed, import_seed
from tests.helpers import make_responder


def test_export_import_roundtrip(tmp_path, mock_payload, default_checklist_dict, monkeypatch):
    from app import config
    src = tmp_path / "src.db"
    db.init_db(src)
    load_upload(mock_payload, "MOCK", db_path=src)
    v1 = db.save_checklist_version(Checklist.model_validate(default_checklist_dict), path=src)
    d = dict(default_checklist_dict)
    d["criteria"] = [dict(c, guidance="v2") if c["id"] == "c05" else c for c in d["criteria"]]
    v2 = db.save_checklist_version(Checklist.model_validate(d), path=src)
    for c in db.list_conversations("MOCK", path=src):
        asyncio.run(evaluate_conversation(c, v2, FakeProvider(make_responder({"c06": {"answer": "7"}})), db_path=src))
    seed = export_seed("MOCK", tmp_path / "seed" / "MOCK.json", db_path=src)
    doc = json.loads(seed.read_text(encoding="utf-8"))
    assert [c["version"] for c in doc["checklists"]] == [2] and len(doc["evaluations"]) == 4
    assert doc["evaluations"][0]["raw_responses"] and doc["criterion_cache"]   # сырые ответы — для прозрачности

    # чистая БД, транскрипты из кэша набора
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "MOCK.json").write_text(json.dumps(mock_payload), encoding="utf-8")
    dst = tmp_path / "dst.db"
    db.init_db(dst)
    db.save_checklist_version(Checklist.model_validate(default_checklist_dict), path=dst)   # v1 как при старте
    r = import_seed(seed, db_path=dst, cache_dir=cache_dir)
    assert r["evaluations"] == 4 and r["checklist_versions"] == 1
    e = db.get_current_evaluation("MOCK", "mock-ru-001", path=dst)
    assert e["checklist_version"] == 2 and e["total"] == "95.50" and e["answers"][0]["evidence"]
    assert e["origin"] == "seed" and e["raw_responses"]
    assert {r["origin"] for r in db.list_current_evaluations("MOCK", path=dst)} == {"seed"}
    assert db.get_checklist("default_chat", path=dst).version == 2
    assert import_seed(seed, db_path=dst, cache_dir=cache_dir)["evaluations"] == 0   # повторно не дублирует

    # кэш перенесён: переоценка той же версией не вызывает модель
    p = FakeProvider(make_responder())
    conv = db.get_conversation("MOCK", "mock-ru-001", path=dst)
    out = asyncio.run(evaluate_conversation(conv, db.get_checklist("default_chat", 2, path=dst), p, db_path=dst))
    assert p.requests == [] and out.record["total"] == "95.50"
    assert db.get_current_evaluation("MOCK", "mock-ru-001", path=dst)["origin"] == "live"   # оценено здесь
    assert config.settings.seed_on_start is False   # по умолчанию сид не подгружается сам

    # автозагрузка: только в пустую БД
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    monkeypatch.setattr(config.settings, "db_path", tmp_path / "auto.db")
    db.init_db()
    db.save_checklist_version(Checklist.model_validate(default_checklist_dict))
    res = autoload()
    assert res and res[0]["evaluations"] == 4
    assert autoload() == []
