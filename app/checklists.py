"""Операции над версиями чек-листа: сравнение версий (что потребует вызова LLM, а что — только пересчёта)."""
from __future__ import annotations

from .models import Checklist, Criterion

def diff(old: Checklist, new: Checklist) -> list[dict]:
    """Изменения критериев между версиями.

    llm=True — смысловое определение изменилось (или критерий новый/включён): при применении к набору
    этот критерий уйдёт в LLM. llm=False — только пересчёт баллов (вес, код, выключение, удаление).
    """
    old_map = {c.id: c for c in old.criteria}
    new_map = {c.id: c for c in new.criteria}
    out: list[dict] = []
    for c in new.criteria:
        o = old_map.get(c.id)
        if o is None:
            out.append({"criterion_id": c.id, "title": c.title, "change": "added", "fields": [], "llm": c.enabled})
            continue
        fields = [f for f in Criterion.model_fields if getattr(o, f) != getattr(c, f)]
        if not fields:
            continue
        if o.enabled and not c.enabled:
            change, llm = "disabled", False
        elif not o.enabled and c.enabled:
            change, llm = "enabled", True
        else:
            change = "modified"
            llm = c.enabled and o.definition_hash() != c.definition_hash()
        out.append({"criterion_id": c.id, "title": c.title, "change": change, "fields": fields, "llm": llm})
    for o in old.criteria:
        if o.id not in new_map:
            out.append({"criterion_id": o.id, "title": o.title, "change": "removed", "fields": [], "llm": False})
    return out
