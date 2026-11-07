"""POST /api/datasets/{group_id}/impact — предпросмотр влияния чек-листа на набор до запуска оценки."""


def test_impact_whole_group(api_client):
    cl = api_client.get("/api/checklists/default_chat").json()["checklist"]
    r = api_client.post("/api/datasets/MOCK/impact", json={"checklist": cl})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_conversations"] == 4
    assert body["llm_criteria"] == 8 * 4
    assert body["estimated_new_calls"] == 4   # 8 критериев, не дробится (conftest: чанк=20)
    assert body["changes"] == []              # черновик идентичен сохранённой версии


def test_impact_reports_changed_criteria_and_shrinks_with_cache(api_client, monkeypatch):
    from app import config
    # /impact резолвит модель профиля из настроек — приравниваем её к модели тестовой FakeProvider,
    # которой реально считаются оценки ниже (иначе кэш-ключ не совпадёт по полю "модель").
    monkeypatch.setattr(config.settings, "llm_model", api_client.fake.model)
    cl = api_client.get("/api/checklists/default_chat").json()["checklist"]
    draft = dict(cl)
    draft["criteria"] = [dict(c, guidance="новое правило") if c["id"] == "c05" else c for c in cl["criteria"]]

    r = api_client.post("/api/datasets/MOCK/impact", json={"checklist": draft})
    assert r.status_code == 200, r.text
    body = r.json()
    assert any(c["criterion_id"] == "c05" and c["llm"] for c in body["changes"])
    assert body["llm_criteria"] == 8 * 4   # пока ничего не оценивалось — весь чек-лист мимо кэша

    # оцениваем набор исходным чек-листом, затем смотрим влияние той же правки — должен остаться c05
    assert api_client.post("/api/evaluations/MOCK/mock-ru-001").status_code == 200
    assert api_client.post("/api/evaluations/MOCK/mock-kz-001").status_code == 200
    r2 = api_client.post("/api/datasets/MOCK/impact",
                         json={"checklist": draft, "conversation_ids": ["mock-ru-001", "mock-kz-001"]})
    body2 = r2.json()
    assert body2["llm_criteria"] == 2   # только c05, по одному на диалог
    assert body2["cached_criteria"] == 14


def test_impact_unknown_conversation_ids_ignored(api_client):
    cl = api_client.get("/api/checklists/default_chat").json()["checklist"]
    r = api_client.post("/api/datasets/MOCK/impact", json={"checklist": cl, "conversation_ids": ["nope"]})
    assert r.status_code == 404


def test_impact_bad_provider(api_client):
    cl = api_client.get("/api/checklists/default_chat").json()["checklist"]
    r = api_client.post("/api/datasets/MOCK/impact", json={"checklist": cl, "provider": "does-not-exist"})
    assert r.status_code == 400


def test_impact_invalid_checklist_rejected(api_client):
    cl = api_client.get("/api/checklists/default_chat").json()["checklist"]
    bad = dict(cl)
    bad["criteria"] = [dict(c, weight=3) if c["id"] == "c08" else c for c in cl["criteria"]]  # flag с весом
    r = api_client.post("/api/datasets/MOCK/impact", json={"checklist": bad})
    assert r.status_code == 422
