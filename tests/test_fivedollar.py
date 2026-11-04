import json
from datetime import datetime, timezone

import pytest

from ah import consensus, fivedollar as f5
from ah.render_report import validate

FX = [{"id": 2120212024, "kickoff_utc": "2026-11-07T12:30:00+00:00", "status": "scheduled",
       "teams": {"home": {"id": 1, "name": "Leeds"}, "away": {"id": 2, "name": "Tottenham"}}}]
ODDS = {"fixture_id": 2120212024, "bookmakers": [{"name": "Bet 365", "slug": "bet365", "odds": {"asian_handicap": {
    "opening": {"line": -0.25, "home": 2.05, "away": 1.8}, "closing": {"line": 0.0, "home": 1.9, "away": 1.95},
    "inplay": None}}}]}
H = {"x-ratelimit-limit": "60", "x-ratelimit-remaining": "50", "x-ratelimit-reset": "9999999999"}


class R:
    def __init__(self, data, status=200, headers=None):
        self.d, self.status_code, self.headers = data, status, headers if headers is not None else H

    def json(self):
        return {"success": 1, "data": self.d}


class S:
    def __init__(self, seq=None):
        self.seq, self.calls = list(seq or []), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        if self.seq:
            return self.seq.pop(0)
        return R(ODDS if url.endswith("/odds") else FX)


def mk(tmp_path, seq=None, sleeps=None, reserve=5):
    return f5.Client(key="KEY", session=S(seq), cache_dir=tmp_path / "c", usage_file=tmp_path / "u.json",
                     reserve=reserve, sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def test_auth_usage_cache(tmp_path):
    c = mk(tmp_path)
    assert c.odds(1)["fixture_id"] == 2120212024
    assert c.s.calls[0][2] == {"Authorization": "Bearer KEY"} and "KEY" not in repr(c)
    assert c.usage()["remaining"] == 50
    c.odds(1)
    assert len(c.s.calls) == 1  # 缓存命中


def test_429_retry_after_and_budget(tmp_path):
    sl = []
    c = mk(tmp_path, seq=[R({}, 429, {"Retry-After": "12"}), R(ODDS)], sleeps=sl)
    c.odds(2)
    assert sl == [12.0]
    (tmp_path / "u.json").write_text(json.dumps({"remaining": 3, "reset": 9999999999}))
    with pytest.raises(f5.BudgetExceeded):
        c.odds(3)


def test_error_hides_key(tmp_path):
    with pytest.raises(f5.FiveDollarError) as e:
        mk(tmp_path, seq=[R({}, 401)]).fixture(1)
    assert "KEY" not in str(e.value)


def test_normalize_and_movement():
    n = f5.normalize_ah(ODDS)
    assert n["home_line"] == 0.0 and n["open"]["home_line"] == -0.25 and n["home_hk"] == 0.9
    m = f5.movement(n, "home")
    assert m["open_pick_line"] == -0.25 and m["pick_line"] == 0.0 and m["toward_pick"] == -0.25
    a = f5.movement(n, "away")
    assert a["open_pick_line"] == 0.25 and a["pick_line"] == 0.0 and a["toward_pick"] == 0.25 and a["pick_water"] == 0.95
    assert f5.normalize_ah({"bookmakers": []}) is None


def test_attach_bet365_movement(tmp_path):
    leagues = [{"id": "epl", "name": "Premier League", "cn": "英超"}]
    r = {"date": "2026-11-07", "generated_at": "2026-11-07T00:00:00Z", "watched": [], "picks": [
        {"match_id": "t", "league": "Premier League 第11轮", "home": "利兹联 Leeds", "away": "Tottenham",
         "kickoff_utc": "2026-11-07T12:30:00Z", "pick_side": "away", "line": 0.0, "odds": 0.95, "confidence": "medium",
         "reasons": ["x"], "risks": [], "sources": ["https://a.example/"]}]}
    consensus.attach(r, fetch=lambda s: {}, aliases={}, leagues=leagues, fd5=mk(tmp_path))
    bm = r["picks"][0]["bet365_movement"]
    assert bm["fixture_id"] == 2120212024 and bm["toward_pick"] == 0.25
    assert consensus.FIVEDOLLAR_URL in r["picks"][0]["sources"]
    validate(r)
    from jinja2 import Environment, FileSystemLoader
    from ah import TEMPLATES
    md = Environment(loader=FileSystemLoader(str(TEMPLATES)), trim_blocks=True, lstrip_blocks=True) \
        .get_template("report.md.j2").render(r=r)
    assert "Bet365 盘口变化：初盘 +0.25 @ 0.8 → 即时 +0 @ 0.95（朝推荐方升盘" in md
