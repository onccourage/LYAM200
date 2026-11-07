"""GET /api/datasets/{group_id}/gold-agreement — живой дашборд согласия с эталонной разметкой
(тот же расчёт, что scripts/eval_gold.py, но в UI, для критерия жюри «точность vs Ground Truth»)."""
import json


def test_no_gold_files(api_client):
    r = api_client.get("/api/datasets/MOCK/gold-agreement")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False and body["gold_files"] == 0


def test_empty_templates_counted_but_no_pairs(api_client, monkeypatch, tmp_path):
    from app import config
    gold_dir = tmp_path / "gold"
    gold_dir.mkdir()
    (gold_dir / "mock-ru-001.json").write_text(json.dumps({
        "conversation_id": "mock-ru-001", "group_id": "MOCK", "checklist_id": "default_chat",
        "answers": {f"c0{i}": None for i in range(1, 9)},
    }), encoding="utf-8")
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)

    r = api_client.get("/api/datasets/MOCK/gold-agreement")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True and body["gold_files"] == 1
    assert body["annotation_progress"] == {"filled": 0, "total": 8}
    assert body["pairs"] == 0


def test_filled_annotation_reports_agreement(api_client, monkeypatch, tmp_path):
    from app import config
    monkeypatch.setattr(config.settings, "llm_model", api_client.fake.model)
    assert api_client.post("/api/evaluations/MOCK/mock-ru-001").status_code == 200
    ev = api_client.get("/api/evaluations/MOCK/mock-ru-001").json()
    model_answers = {a["criterion_id"]: a["answer"] for a in ev["answers"]}

    gold_dir = tmp_path / "gold"
    gold_dir.mkdir()
    gold_answers = dict(model_answers)
    gold_answers["c01"] = "no" if model_answers["c01"] == "yes" else "yes"   # одно намеренное расхождение
    (gold_dir / "mock-ru-001.json").write_text(json.dumps({
        "conversation_id": "mock-ru-001", "group_id": "MOCK", "checklist_id": "default_chat",
        "answers": gold_answers,
    }), encoding="utf-8")
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)

    r = api_client.get("/api/datasets/MOCK/gold-agreement")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["annotation_progress"] == {"filled": 8, "total": 8}
    assert body["pairs"] == 8
    assert body["criteria"]["c01"]["agreement"] == 0.0
    assert all(body["criteria"][cid]["agreement"] == 1.0 for cid in body["criteria"] if cid != "c01")
