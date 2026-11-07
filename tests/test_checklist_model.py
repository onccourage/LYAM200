from decimal import Decimal

import pytest
from pydantic import ValidationError

from app import db
from app.models import Checklist


def test_seed_loads_and_matches_table(default_checklist_dict):
    cl = Checklist.model_validate(default_checklist_dict)
    weights = {c.id: c.weight for c in cl.criteria}
    assert weights == {"c01": 10, "c02": 15, "c03": 20, "c04": 20, "c05": 15, "c06": 15, "c07": 5, "c08": 0}
    assert cl.total_weight() == 100
    assert cl.warnings() == []
    na = {c.id for c in cl.criteria if c.na_allowed}
    assert na == {"c02", "c03", "c04", "c07"}
    assert cl.criterion("c04").scale_values == [20, 15, 10, 5, 0]
    assert cl.criterion("c06").scale_values == [10, 7, 4, 0]
    assert cl.criterion("c08").type == "flag"


def test_numbers_are_decimal_and_serialize_as_numbers(default_checklist_dict):
    d = dict(default_checklist_dict)
    d["criteria"] = [dict(c) for c in d["criteria"]]
    d["criteria"][0]["weight"] = 10.5
    cl = Checklist.model_validate(d)
    assert cl.criteria[0].weight == Decimal("10.5")
    dumped = cl.model_dump(mode="json")
    assert dumped["criteria"][0]["weight"] == 10.5
    assert dumped["criteria"][1]["weight"] == 15 and isinstance(dumped["criteria"][1]["weight"], int)
    assert Checklist.model_validate(dumped) == cl  # round-trip


def _with(d, cid, **changes):
    d = dict(d)
    d["criteria"] = [dict(c, **changes) if c["id"] == cid else c for c in d["criteria"]]
    return d


@pytest.mark.parametrize("cid,changes,msg", [
    ("c08", {"weight": 5}, "flag"),
    ("c04", {"scale": [{"value": 5, "label": "x"}]}, "2 ступени"),
    ("c04", {"scale": [{"value": 5}, {"value": 5}]}, "уникальны"),
    ("c04", {"scale": [{"value": 0}, {"value": 0.0}]}, "уникальны"),
    ("c04", {"scale": [{"value": 0}, {"value": -1}]}, "отрицательн"),
    ("c01", {"scale": [{"value": 1}, {"value": 0}]}, "только для type=scale"),
    ("c01", {"weight": -1}, "отрицательным"),
    ("c01", {"section": "nope"}, "несуществующие секции"),
    ("c01", {"type": "multi"}, "type"),
    ("c01", {"channels": []}, "канал"),
    ("c01", {"unknown_field": 1}, "xtra"),
])
def test_invalid_criteria_rejected(default_checklist_dict, cid, changes, msg):
    with pytest.raises(ValidationError, match=msg):
        Checklist.model_validate(_with(default_checklist_dict, cid, **changes))


def test_duplicate_criterion_ids_rejected(default_checklist_dict):
    d = dict(default_checklist_dict)
    d["criteria"] = d["criteria"] + [d["criteria"][0]]
    with pytest.raises(ValidationError, match="уникальны"):
        Checklist.model_validate(d)


def test_arbitrary_scale_is_sorted_and_accepted(default_checklist_dict):
    d = _with(default_checklist_dict, "c05", type="scale", weight=15, na_allowed=True, na_condition="x",
              scale=[{"value": v, "label": str(v)} for v in (0, 2, 4, 6)])
    cl = Checklist.model_validate(d)
    assert cl.criterion("c05").scale_values == [6, 4, 2, 0]
    assert cl.criterion("c05").scale_max == 6


def test_warnings(default_checklist_dict):
    cl = Checklist.model_validate(_with(default_checklist_dict, "c01", weight=20))
    assert any("110" in w for w in cl.warnings())
    cl = Checklist.model_validate(_with(default_checklist_dict, "c02", na_condition=""))
    assert any("c02" in w and "условие" in w for w in cl.warnings())


def test_definition_hash_ignores_weight_but_not_text(default_checklist_dict):
    base = Checklist.model_validate(default_checklist_dict).criterion("c05")
    w = Checklist.model_validate(_with(default_checklist_dict, "c05", weight=10)).criterion("c05")
    t = Checklist.model_validate(_with(default_checklist_dict, "c05", guidance="другое")).criterion("c05")
    assert base.definition_hash() == w.definition_hash()
    assert base.definition_hash() != t.definition_hash()


def test_versions_are_immutable(tmp_db, default_checklist_dict):
    v1 = db.save_checklist_version(Checklist.model_validate(default_checklist_dict), path=tmp_db)
    assert v1.version == 1
    changed = Checklist.model_validate(_with(default_checklist_dict, "c05", title="Новый критерий"))
    v2 = db.save_checklist_version(changed.model_copy(update={"version": 99}), path=tmp_db)
    assert v2.version == 2  # номер назначает БД, а не клиент
    assert db.get_checklist("default_chat", 1, path=tmp_db).criterion("c05").title == "Эмпатия и клиентоориентированность"
    assert db.get_checklist("default_chat", path=tmp_db).criterion("c05").title == "Новый критерий"
    assert [v["version"] for v in db.list_checklist_versions("default_chat", path=tmp_db)] == [1, 2]
