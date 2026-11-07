"""Тесты раздела 4 CLAUDE.md. Все числа — Decimal; итог — строго ROUND_HALF_UP."""
from decimal import Decimal

import pytest

from app.models import Checklist
from app.scoring import ScoringError, score

MAX = {"c01": "yes", "c02": "yes", "c03": "yes", "c04": 20, "c05": "yes", "c06": 10, "c07": "yes", "c08": "no"}


@pytest.fixture
def cl(default_checklist_dict) -> Checklist:
    return Checklist.model_validate(default_checklist_dict)


def ans(**kw):
    return {**MAX, **kw}


def crit(r, cid):
    return next(x for x in r.criteria if x.criterion_id == cid)


# ---------------------------------------------------------------- обязательные кейсы

def test_all_max_is_100(cl):
    r = score(cl, MAX)
    assert r.total == Decimal("100.00") and str(r.total) == "100.00"
    assert r.applicable_weight == 100 and not r.gross_error and r.total_status == "calculated"


def test_c06_7_gives_95_50(cl):
    r = score(cl, ans(c06=7))
    assert str(r.total) == "95.50"
    assert crit(r, "c06").points == Decimal("10.5")


def test_na_redistribution_65(cl):
    r = score(cl, ans(c02="na", c03="na"))
    assert str(r.total) == "100.00"
    assert r.applicable_weight == 65
    assert crit(r, "c02").status == "na" and crit(r, "c02").effective_weight is None
    assert sum(x.effective_weight for x in r.criteria if x.effective_weight is not None) == 100


def test_c01_no_c03_na_c04_15_gives_81_25(cl):
    r = score(cl, ans(c01="no", c03="na", c04=15))
    assert r.applicable_weight == 80
    assert r.points == 65
    assert str(r.total) == "81.25"
    # видно перераспределение: c04 вес 20 → 25 из 100, 15 баллов → 18.75
    c04 = crit(r, "c04")
    assert c04.effective_weight == 25 and c04.effective_points == Decimal("18.75")
    assert c04.effective_weight_display == "25.00"


def test_gross_error_does_not_zero_total(cl):
    r = score(cl, ans(c08={"answer": "yes", "gross_types": ["bypass_service"]}))
    assert str(r.total) == "100.00"
    assert r.gross_error is True
    assert r.gross_types == ["bypass_service"] and r.gross_criteria == ["c08"]
    gross = next(s for s in r.sections if s.section_id == "gross")
    assert gross.gross_error and not gross.has_weights and gross.pct is None and gross.points is None


def test_all_weighted_na_gives_null_not_zero():
    custom = Checklist.model_validate({
        "id": "custom", "name": "x",
        "sections": [{"id": "s", "title": "S"}],
        "criteria": [
            {"id": "a", "section": "s", "title": "A", "type": "binary", "weight": 50, "na_allowed": True, "na_condition": "x"},
            {"id": "b", "section": "s", "title": "B", "type": "scale", "weight": 50, "na_allowed": True, "na_condition": "x",
             "scale": [{"value": 0}, {"value": 3}]},
            {"id": "g", "section": "s", "title": "G", "type": "flag"},
        ],
    })
    r = score(custom, {"a": "na", "b": "na", "g": "no"})
    assert r.total is None and r.total_status == "not_calculated"
    assert r.sections[0].pct is None


def test_na_when_not_allowed_is_error(cl):
    with pytest.raises(ScoringError, match="c01: N/A не разрешён"):
        score(cl, ans(c01="na"))


def test_scale_value_not_in_steps_is_error(cl):
    with pytest.raises(ScoringError, match="не является ступенью"):
        score(cl, ans(c04=12))
    with pytest.raises(ScoringError, match="не является ступенью"):
        score(cl, ans(c06=5))


def test_arbitrary_scale_and_weight_without_code_changes():
    custom = Checklist.model_validate({
        "id": "custom", "name": "x",
        "sections": [{"id": "s1", "title": "S1", "order": 1}, {"id": "s2", "title": "S2", "order": 2}],
        "criteria": [
            {"id": "q", "section": "s1", "title": "Q", "type": "scale", "weight": 37,
             "scale": [{"value": v} for v in range(6)]},          # 0..5
            {"id": "w", "section": "s2", "title": "W", "type": "binary", "weight": 13},
        ],
    })
    r = score(custom, {"q": 3, "w": "yes"})
    # q: 37*3/5 = 22.2; total = (22.2+13)/50*100 = 70.4
    assert crit(r, "q").points == Decimal("22.2")
    assert str(r.total) == "70.40"
    assert [str(s.pct) for s in r.sections] == ["60.00", "100.00"]


def test_half_up_on_default_checklist(cl):
    # применимый вес 80 (c04=N/A), баллы 80 - 4.5 (c06=7) - 5 (c07=Нет) = 70.5 → 88.125
    r = score(cl, ans(c04="na", c06=7, c07="no"))
    assert r.points / r.applicable_weight * 100 == Decimal("88.125")
    assert str(r.total) == "88.13"            # half-up; банковское и float дали бы 88.12
    assert round(88.125, 2) == 88.12          # демонстрация, почему float нельзя


def test_half_up_custom_weights():
    custom = Checklist.model_validate({
        "id": "custom", "name": "x", "sections": [{"id": "s"}],
        "criteria": [
            {"id": "a", "section": "s", "title": "A", "type": "binary", "weight": 2469},
            {"id": "b", "section": "s", "title": "B", "type": "binary", "weight": 17531},
        ],
    })
    assert str(score(custom, {"a": "yes", "b": "no"}).total) == "12.35"   # 12.345 → 12.35


# ---------------------------------------------------------------- дополнительные свойства

def test_sections(cl):
    r = score(cl, ans(c01="no", c03="na", c04=15))
    s = {x.section_id: x for x in r.sections}
    assert s["start"].applicable_weight == 25 and str(s["start"].points) == "15.00" and str(s["start"].pct) == "60.00"
    assert s["security_resolution"].applicable_weight == 20 and str(s["security_resolution"].pct) == "75.00"
    assert str(s["conduct_closing"].pct) == "100.00"


def test_rounding_digits_from_checklist(default_checklist_dict):
    d = dict(default_checklist_dict, rounding=1)
    r = score(Checklist.model_validate(d), ans(c04="na", c06=7, c07="no"))
    assert str(r.total) == "88.1"


def test_weights_not_summing_to_100_still_normalized(default_checklist_dict):
    d = dict(default_checklist_dict)
    d["criteria"] = [dict(c, weight=c["weight"] * 2) for c in d["criteria"]]
    r = score(Checklist.model_validate(d), ans(c06=7))
    assert str(r.total) == "95.50"


def test_channel_exclusion_is_system_na(default_checklist_dict):
    d = dict(default_checklist_dict)
    d["criteria"] = [dict(c, channels=["voice call"]) if c["id"] == "c01" else c for c in d["criteria"]]
    cl = Checklist.model_validate(d)
    r = score(cl, {k: v for k, v in MAX.items() if k != "c01"}, channel="agent chat")
    c01 = crit(r, "c01")
    assert c01.status == "na_channel" and "agent chat" in c01.na_reason
    assert r.applicable_weight == 90 and str(r.total) == "100.00"
    # для звонка критерий применим и обязателен
    with pytest.raises(ScoringError, match="c01: нет ответа"):
        score(cl, {k: v for k, v in MAX.items() if k != "c01"}, channel="voice call")


def test_pending_needs_review_marks_partial(cl):
    r = score(cl, ans(c04=None))
    assert r.is_partial and r.pending == ["c04"]
    assert r.applicable_weight == 80 and str(r.total) == "100.00"


def test_missing_and_unknown_answers_are_errors(cl):
    a = dict(MAX)
    del a["c05"]
    a["zzz"] = "yes"
    with pytest.raises(ScoringError) as e:
        score(cl, a)
    assert any("c05: нет ответа" in x for x in e.value.errors)
    assert any("zzz" in x for x in e.value.errors)


def test_wrong_answer_type_is_error(cl):
    with pytest.raises(ScoringError, match="c01"):
        score(cl, ans(c01=10))
    with pytest.raises(ScoringError, match="c04"):
        score(cl, ans(c04="yes"))
    with pytest.raises(ScoringError, match="gross_types"):
        score(cl, ans(c08={"answer": "yes", "gross_types": ["rudeness"]}))


def test_disabled_criterion_ignored(default_checklist_dict):
    d = dict(default_checklist_dict)
    d["criteria"] = [dict(c, enabled=False) if c["id"] == "c06" else c for c in d["criteria"]]
    a = {k: v for k, v in MAX.items() if k != "c06"}
    r = score(Checklist.model_validate(d), a)
    assert r.applicable_weight == 85 and str(r.total) == "100.00"


def test_answer_forms_are_normalized(cl):
    r = score(cl, ans(c01=True, c02="Да", c04="15", c06=7.0, c07="NA"))
    assert crit(r, "c01").answer == "yes" and crit(r, "c04").answer == "15" and crit(r, "c06").answer == "7"
    assert crit(r, "c07").status == "na"


def test_pure_function_does_not_mutate_inputs(cl):
    a = ans(c06=7)
    before = (cl.model_dump(), dict(a))
    score(cl, a)
    score(cl, a)
    assert (cl.model_dump(), a) == before


def test_json_serialization_keeps_exact_strings(cl):
    r = score(cl, ans(c06=7)).model_dump(mode="json")
    assert r["total"] == "95.50"
    assert isinstance(next(c for c in r["criteria"] if c["criterion_id"] == "c06")["points"], str)
