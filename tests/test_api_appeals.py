def test_appeal_http_flow(api_client):
    c = api_client
    ev = c.post("/api/evaluations/MOCK/mock-ru-001").json()
    assert ev["total"] == "100.00"
    calls = len(c.fake.requests)

    assert c.post("/api/appeals", json={"evaluation_id": ev["id"], "criterion_id": "c01", "comment": ""}).status_code == 400
    r = c.post("/api/appeals", json={"evaluation_id": ev["id"], "criterion_id": "c07", "comment": "Не попрощался"})
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    assert c.post("/api/appeals", json={"evaluation_id": ev["id"], "criterion_id": "c07", "comment": "ещё"}).status_code == 409
    assert c.get("/api/appeals/summary").json() == {"open": 1}
    assert c.get("/api/datasets/MOCK/conversations").json()[3]["evaluation"]["open_appeals"] == 1

    bad = c.post(f"/api/appeals/{aid}/resolve", json={"decision": "changed", "reason": "x", "new_answer": "7"})
    assert bad.status_code == 400
    d = c.post(f"/api/appeals/{aid}/resolve", json={"decision": "changed", "reason": "Прощания нет", "new_answer": "no",
                                                     "reviewer_label": "Б"}).json()
    assert d["appeal"]["status"] == "resolved" and d["appeal"]["final_total"] == "95.00"
    assert d["evaluation"]["total"] == "95.00" and d["evaluation"]["original_total"] == "100.00"

    e = c.get("/api/evaluations/MOCK/mock-ru-001").json()
    assert e["total"] == "95.00" and e["appeals"][0]["decision"] == "changed"
    assert c.get("/api/appeals", params={"status": "resolved"}).json()[0]["id"] == aid
    assert len(c.fake.requests) == calls   # пересчёт без LLM
    assert c.get("/api/appeals/999").status_code == 404
