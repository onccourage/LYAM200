import asyncio
import json
import subprocess
import sys

import pytest

from app import db
from app.dataset import load_upload
from app.evaluator import evaluate_conversation
from app.gold import cohen_kappa, compare, load_gold, suggest
from app.llm import FakeProvider
from app.models import Checklist
from tests.helpers import make_responder


def test_kappa():
    assert cohen_kappa([("yes", "yes"), ("yes", "no"), ("no", "no"), ("no", "no")]) == 0.5
    assert cohen_kappa([("yes", "yes")] * 5) == 1.0
    assert cohen_kappa([("yes", "no")] * 3) == 0.0
    assert cohen_kappa([]) is None
    # согласие на уровне случайности → 0
    assert cohen_kappa([("a", "a"), ("a", "b"), ("b", "a"), ("b", "b")]) == 0.0


@pytest.fixture
def world(tmp_db, tmp_path, mock_payload, default_checklist_dict):
    load_upload(mock_payload, "MOCK", db_path=tmp_db)
    cl = db.save_checklist_version(Checklist.model_validate(default_checklist_dict), path=tmp_db)
    convs = {c.conversation_id: c for c in db.list_conversations("MOCK", path=tmp_db)}
    # модель: в ru c06=7 и c07=no; в kz c03=na
    ov = {
        "mock-ru-001": {"c06": {"answer": "7"}, "c07": {"answer": "no", "evidence": [], "checked_range": {"from": "m5", "to": "m7"}}},
        "mock-kz-001": {"c03": {"answer": "na", "na_reason": "общий вопрос", "evidence": []}},
    }
    evals = {}
    for cid in ("mock-ru-001", "mock-kz-001", "mock-en-001"):
        out = asyncio.run(evaluate_conversation(convs[cid], cl, FakeProvider(make_responder(ov.get(cid, {}))), db_path=tmp_db))
        evals[cid] = db.get_evaluation(out.record["id"], path=tmp_db)
    gold_dir = tmp_path / "gold"
    gold_dir.mkdir()
    full = {"c01": "yes", "c02": "yes", "c03": "yes", "c04": 20, "c05": "yes", "c06": 10, "c07": "yes", "c08": "no"}
    files = {
        # ru: эталон расходится с моделью в c06 (10 vs 7) и c07 (yes vs no)
        "mock-ru-001": full,
        # kz: совпадает (c03=na), c04 не размечен
        "mock-kz-001": {**full, "c03": "na", "c04": None},
        # en: эталон ставит грубую ошибку, модель — нет; плюс одна ошибка разметки
        "mock-en-001": {**full, "c08": {"answer": "yes", "gross_types": ["insult"]}, "c01": "maybe"},
        # нет в наборе
        "ghost": full,
    }
    for cid, ans in files.items():
        (gold_dir / f"{cid}.json").write_text(json.dumps({"conversation_id": cid, "group_id": "MOCK", "answers": ans}), encoding="utf-8")
    (gold_dir / "_report.json").write_text("{}", encoding="utf-8")
    return tmp_db, cl, convs, evals, gold_dir


def test_compare(world):
    path, cl, convs, evals, gold_dir = world
    gold = load_gold(gold_dir)
    assert len(gold) == 4   # _report.json пропущен
    r = compare(gold, evals, convs, {(cl.id, cl.version): cl}, cl)
    assert r["dialogs"] == 3
    c06 = r["criteria"]["c06"]
    assert c06["n"] == 3 and c06["agreement"] == round(2 / 3, 4) and c06["mae_step"] == 1.0 and c06["confusion"]["10→7"] == 1
    c07 = r["criteria"]["c07"]
    assert c07["agreement"] == round(2 / 3, 4) and c07["confusion"] == {"yes→no": 1, "yes→yes": 2}
    assert r["criteria"]["c03"]["confusion"] == {"na→na": 1, "yes→yes": 2} and r["criteria"]["c03"]["kappa"] == 1.0
    assert r["criteria"]["c04"]["n"] == 2 and r["unlabeled_answers"] == 1
    assert r["criteria"]["c08"]["confusion"] == {"no→no": 2, "yes→no": 1}
    assert r["criteria"]["c01"]["n"] == 2   # ошибочная разметка en не учитывается
    assert any("mock-en-001" in i and "c01" in i for i in r["issues"])
    assert any("ghost" in i for i in r["issues"])
    assert r["by_language"]["RU"]["agreement"] < 1 and r["by_language"]["KZ"]["agreement"] == 1.0
    ru = next(t for t in r["totals"] if t["conversation_id"] == "mock-ru-001")
    assert ru["gold_total"] == "100.00" and ru["model_total"] == "90.50" and ru["abs_diff"] == 9.5
    assert r["gross_flag_agreement"] is not None and r["gross_flag_agreement"] < 1


def test_suggest_diverse(mock_payload):
    from app.dataset import parse_records
    convs, _ = parse_records(mock_payload["data"]["records"], "MOCK")
    picked = suggest(convs, 3)
    assert picked[0].channel == "voice call"
    assert len({c.language for c in picked}) >= 2
    assert len(suggest(convs, 10)) == 4


def test_scripts_run(world, tmp_path, monkeypatch):
    path, *_ , gold_dir = world
    env = {**__import__("os").environ, "DB_PATH": str(path)}
    out = subprocess.run([sys.executable, "scripts/eval_gold.py", "MOCK", "--gold-dir", str(gold_dir),
                          "--json", str(tmp_path / "r.json")], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    assert "c06" in out.stdout and "По языкам" in out.stdout
    assert json.loads((tmp_path / "r.json").read_text())["dialogs"] == 3
    tpl = tmp_path / "tpl"
    out = subprocess.run([sys.executable, "scripts/gold_template.py", "MOCK", "--suggest", "2", "--out", str(tpl),
                          "--from-model"], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    files = list(tpl.glob("*.json"))
    assert len(files) == 2
    doc = json.loads(files[0].read_text())
    assert doc["_transcript"] and "c01" in doc["_help"]
