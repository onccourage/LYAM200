"""Фейковый «LLM» для тестов: отвечает по сгенерированной схеме, цитирует реальные реплики."""
import re

LINE = re.compile(r"^\[(m\d+)\]\[[^\]]*\]\[(\w+)\] (.*)$", re.M)


def transcript_lines(user: str) -> list[tuple[str, str, str]]:
    body = user.split("<transcript>\n", 1)[1].split("\n</transcript>", 1)[0]
    return LINE.findall(body)


def make_responder(overrides: dict | None = None):
    """overrides: {criterion_id: dict | callable(default_item)->dict}"""
    overrides = overrides or {}

    def responder(system, user, schema):
        lines = transcript_lines(user)
        op = next(((mid, txt) for mid, sp, txt in lines if sp == "operator"), None)
        out = {}
        for cid, cs in schema["properties"].items():
            enum = cs["properties"]["answer"]["enum"]
            first = enum[0]
            item = {
                "evidence": [{"message_id": op[0], "quote": op[1][:15]}] if op else [],
                "checked_range": None,
                "rationale": f"{cid} — {first}. Тестовое обоснование.",
                "na_reason": None,
                "answer": first,
                "confidence": 0.9,
            }
            if "gross_types" in cs["properties"]:
                item.update(answer="no", gross_types=[], evidence=[])
            ov = overrides.get(cid)
            if callable(ov):
                item = ov(item)
            elif ov:
                item = {**item, **ov}
            out[cid] = item
        return out

    return responder
