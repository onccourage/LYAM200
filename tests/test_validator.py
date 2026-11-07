import pytest

from app.dataset import parse_record
from app.models import Checklist
from app.preprocess import preprocess
from app.validator import quote_in_text, validate_criterion, validate_response


@pytest.fixture
def cl(default_checklist_dict):
    return Checklist.model_validate(default_checklist_dict)


@pytest.fixture
def prep(mock_payload):
    return preprocess(parse_record(mock_payload["data"]["records"][0], "MOCK"))


def item(**kw):
    base = {"evidence": [{"message_id": "m2", "quote": "Меня зовут Айгерим"}], "checked_range": None,
            "rationale": "ok", "na_reason": None, "answer": "yes", "confidence": 0.8}
    return {**base, **kw}


def test_quote_matching():
    t = "Здравствуйте!  Меня   зовут «Айгерим». Подскажите номер договора?"
    assert quote_in_text("меня зовут \"Айгерим\"", t)
    assert quote_in_text("Здравствуйте! ... номер договора", t)
    assert not quote_in_text("номер договора ... Здравствуйте", t)
    assert not quote_in_text("Меня зовут Айгуль", t)
    assert not quote_in_text("  ", t)


def test_valid_yes_enriches_evidence(cl, prep):
    v, errs = validate_criterion(cl.criterion("c01"), item(), prep)
    assert errs == [] and v.answer == "yes"
    assert v.evidence[0].speaker == "operator" and v.evidence[0].timestamp == "00:40" and v.evidence[0].offset_sec == 40


@pytest.mark.parametrize("cid,kw,msg", [
    ("c01", {"answer": "na", "na_reason": "x"}, "N/A для этого критерия не разрешён"),
    ("c04", {"answer": "12"}, "недопустимый ответ"),
    ("c01", {"evidence": [{"message_id": "m99", "quote": "x"}]}, "нет в диалоге"),
    ("c01", {"evidence": [{"message_id": "m2", "quote": "Меня зовут Айгуль"}]}, "не найдена"),
    ("c01", {"evidence": [{"message_id": "m1", "quote": "Не могу вывести"}]}, "цитаты из реплики оператора"),
    ("c01", {"evidence": [{"message_id": "m0", "quote": "Здравствуйте"}]}, "бот"),
    ("c02", {"answer": "na", "na_reason": None, "evidence": []}, "na_reason"),
    ("c01", {"answer": "no", "evidence": []}, "checked_range"),
    ("c01", {"answer": "no", "evidence": [], "checked_range": {"from": "m5", "to": "m2"}}, "позже"),
    ("c01", {"rationale": ""}, "обоснование"),
    ("c08", {"answer": "yes", "gross_types": []}, "gross_types"),
    ("c08", {"answer": "yes", "gross_types": ["insult"], "evidence": [{"message_id": "m1", "quote": "Не могу"}]}, "оператора"),
])
def test_invalid(cl, prep, cid, kw, msg):
    v, errs = validate_criterion(cl.criterion(cid), item(**kw), prep)
    assert v is None and any(msg in e for e in errs), errs


def test_system_event_is_valid_evidence(cl, prep):
    v, errs = validate_criterion(cl.criterion("c03"), item(evidence=[{"message_id": "m4", "quote": "идентифицирован"}]), prep)
    assert errs == [] and v.evidence[0].speaker == "system"


def test_no_with_checked_range_and_na_with_reason(cl, prep):
    v, errs = validate_criterion(cl.criterion("c07"), item(answer="no", evidence=[], checked_range={"from": "m5", "to": "m7"}), prep)
    assert errs == [] and v.checked_range.from_id == "m5"
    v, errs = validate_criterion(cl.criterion("c02"), item(answer="na", evidence=[], na_reason="всё было ясно"), prep)
    assert errs == [] and v.na_reason == "всё было ясно"


def test_scale_zero_needs_no_operator_quote(cl, prep):
    v, errs = validate_criterion(cl.criterion("c04"), item(answer="0", evidence=[{"message_id": "m1", "quote": "вывести деньги"}]), prep)
    assert errs == [] and v.answer == "0"
    v, errs = validate_criterion(cl.criterion("c04"), item(answer="15.0"), prep)
    assert errs == [] and v.answer == "15"


def test_flag_no_and_yes(cl, prep):
    v, errs = validate_criterion(cl.criterion("c08"), item(answer="no", evidence=[], gross_types=["insult"]), prep)
    assert errs == [] and v.gross_types == []
    v, errs = validate_criterion(cl.criterion("c08"), item(answer="yes", gross_types=["confidential_disclosure"]), prep)
    assert errs == [] and v.gross_types == ["confidential_disclosure"]


def test_validate_response_missing_and_confidence_clamped(cl, prep):
    ok, errs = validate_response([cl.criterion("c01"), cl.criterion("c05")], {"c01": item(confidence=7)}, prep)
    assert ok["c01"].confidence == 1.0
    assert errs == {"c05": ["критерий отсутствует в ответе"]}
