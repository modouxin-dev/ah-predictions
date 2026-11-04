import json

from ah import source_status as ss


def test_record_and_sanitize(tmp_path, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "supersecret123")
    p = tmp_path / "s.json"
    ss.record("odds_api", False, "boom https://x/?apiKey=supersecret123&a=1 Bearer abcdef", {"remaining": 5, "limit": 500}, p=p)
    ss.record("odds_api", True, quota={"remaining": 4}, p=p)
    raw = p.read_text()
    assert "supersecret123" not in raw and "abcdef" not in raw
    s = json.loads(raw)["sources"]["odds_api"]
    assert s["last_ok_at"] and s["last_error"] and s["quota"]["remaining"] == 4 and s["quota"]["limit"] == 500


def test_record_never_raises(tmp_path):
    ss.record("x", True, p=tmp_path / "nodir" / "a" / "s.json")
    ss.record("x", True, p=tmp_path)  # 目录：写失败也不抛


def test_seed_from_usage(tmp_path):
    c = tmp_path / "cache"
    c.mkdir()
    (c / "api_usage.json").write_text(json.dumps({"date": "2026-11-04", "used": 4, "remaining": 96,
                                                  "log": [{"t": "11:28:15", "status": 200}]}))
    (c / "oddspapi_usage.json").write_text(json.dumps({"month": "2026-11", "used": 5, "limit": 250}))
    d = ss.seed_from_usage(c, p=tmp_path / "s.json")
    assert d["sources"]["api_football"]["last_ok_at"] == "2026-11-04T11:28:15Z"
    assert d["sources"]["oddspapi"]["quota"]["remaining"] == 245


def test_client_writes_status(tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    monkeypatch.setenv("AH_SOURCE_STATUS_FILE", str(p))
    from ah import odds_api

    class Resp:
        status_code = 200
        headers = {"x-requests-remaining": "490", "x-requests-used": "10"}

        def json(self):
            return []

    class S:
        def get(self, *a, **k):
            return Resp()
    odds_api.OddsAPI(key="KEYVALUE123", session=S(), usage_file=tmp_path / "u.json").sports()
    raw = p.read_text()
    assert "KEYVALUE123" not in raw and json.loads(raw)["sources"]["odds_api"]["quota"]["remaining"] == 490
