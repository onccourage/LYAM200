import json

import httpx
import pytest

from app import db
from app.dataset import (DatasetError, compute_offsets, load_dataset, load_upload, parse_clock,
                         parse_record, read_cache)


@pytest.mark.parametrize("ts,sec", [("00:02", 2), ("01:30", 90), ("75:00", 4500), ("01:02:03", 3723),
                                     ("00:01.5", 1.5), ("garbage", None)])
def test_parse_clock(ts, sec):
    assert parse_clock(ts) == sec


def test_iso_offsets_from_first_message():
    offs = compute_offsets(["2026-11-06T10:15:02+05:00", "2026-11-06T10:15:09+05:00", "2026-11-06T05:16:30.5Z"])
    assert offs == [0.0, 7.0, 88.5]


def test_unparseable_offsets_are_none():
    assert compute_offsets(["", "n/a"]) == [None, None]


def test_parse_record_keeps_raw_timestamps(mock_payload):
    rec = mock_payload["data"]["records"][3]
    c = parse_record(rec, "MOCK")
    assert [m.timestamp for m in c.messages] == [t["timestamp"] for t in rec["transcript"]]
    assert [m.id for m in c.messages] == ["m0", "m1", "m2"]
    assert c.messages[2].offset_sec == 88.5
    assert c.channel == "voice call" and c.audio_url
    assert c.raw == rec


def _client(payload, status=200):
    def handler(request: httpx.Request):
        assert request.url.params["group_id"] == "MOCK"
        return httpx.Response(status, json=payload)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_load_from_api_writes_cache_and_db(tmp_path, tmp_db, mock_payload):
    r = load_dataset("MOCK", url="http://api.test/data", client=_client(mock_payload),
                     cache_dir=tmp_path, db_path=tmp_db)
    assert r.source == "api" and len(r.conversations) == 4 and r.warnings == []
    assert read_cache("MOCK", tmp_path) == mock_payload
    convs = db.list_conversations("MOCK", path=tmp_db)
    assert {c.conversation_id for c in convs} == {"mock-ru-001", "mock-kz-001", "mock-en-001", "mock-call-001"}
    one = db.get_conversation("MOCK", "mock-ru-001", path=tmp_db)
    assert one.messages[4].speaker == "system" and one.messages[5].offset_sec == 270


def test_is_success_false_is_shown_not_masked(tmp_path, tmp_db, mock_payload):
    (tmp_path / "MOCK.json").write_text(json.dumps(mock_payload), encoding="utf-8")
    bad = {"error": "group not found", "isSuccess": False, "data": None}
    with pytest.raises(DatasetError, match="group not found"):
        load_dataset("MOCK", url="http://api.test", client=_client(bad), cache_dir=tmp_path, db_path=tmp_db)


def test_api_down_falls_back_to_cache(tmp_path, tmp_db, mock_payload):
    (tmp_path / "MOCK.json").write_text(json.dumps(mock_payload), encoding="utf-8")
    r = load_dataset("MOCK", url="http://api.test", client=_client({}, status=503),
                     cache_dir=tmp_path, db_path=tmp_db)
    assert r.source == "cache" and len(r.conversations) == 4
    assert any("503" in w for w in r.warnings)


def test_no_url_no_cache_errors(tmp_path, tmp_db):
    with pytest.raises(DatasetError, match="не найден в кэше"):
        load_dataset("MOCK", url="", cache_dir=tmp_path, db_path=tmp_db)


def test_bad_group_id_rejected(tmp_path, tmp_db):
    with pytest.raises(DatasetError, match="group_id"):
        load_dataset("../etc", url="", cache_dir=tmp_path, db_path=tmp_db)


def test_bad_records_skipped_with_warning(tmp_path, tmp_db, mock_payload):
    mock_payload["data"]["records"].append({"conversation_id": "broken", "transcript": "oops"})
    r = load_dataset("MOCK", url="http://api.test", client=_client(mock_payload),
                     cache_dir=tmp_path, db_path=tmp_db)
    assert len(r.conversations) == 4
    assert any("broken" in w for w in r.warnings)


def test_manual_upload_variants(tmp_db, mock_payload):
    rec = mock_payload["data"]["records"][0]
    assert len(load_upload(rec, db_path=tmp_db).conversations) == 1
    assert len(load_upload([rec, mock_payload["data"]["records"][1]], db_path=tmp_db).conversations) == 2
    assert len(load_upload(mock_payload, db_path=tmp_db).conversations) == 4
    assert len(db.list_conversations("MANUAL", path=tmp_db)) == 4
    with pytest.raises(DatasetError):
        load_upload("nope", db_path=tmp_db)


def test_build_url_replaces_group_id():
    from app.dataset import build_url
    u = build_url("https://x.test/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sig=abc&group_id=DEV", "CTRL-7")
    assert u == "https://x.test/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sig=abc&group_id=CTRL-7"
    assert build_url("https://x.test/data", "DEV") == "https://x.test/data?group_id=DEV"
