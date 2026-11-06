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


def test_fallback_form_uses_only_confirmed_past_results_for_correct_teams():
    cutoff = datetime(2026, 11, 6, 8, tzinfo=timezone.utc)
    def game(day, home, away, hg, ag, status='finished', code='full'):
        return {'kickoff_utc': f'2026-11-{day:02d}T12:00:00Z', 'status': status, 'status_code': code,
                'teams': {'home': {'id': home}, 'away': {'id': away}}, 'goals': {'home': hg, 'away': ag}}
    games = [game(5, 1, 3, 2, 0), game(4, 3, 1, 1, 1), game(3, 1, 3, 0, 1),
             game(7, 1, 3, 9, 0), game(2, 4, 5, 1, 0), game(1, 1, 3, 4, 0, code='abandoned')]
    assert f5.team_form(games, 1, cutoff) == 'WDL'

    class Client:
        def fixtures(self, *_args, **_kwargs):
            return FX
        def team_fixtures(self, team_id, before):
            assert before == cutoff
            return games if team_id == 1 else [game(5, 2, 3, 0, 1), game(4, 3, 2, 1, 1), game(3, 2, 3, 2, 0)]
    f = f5.fundamentals(Client(), 'epl', 'Leeds', 'Tottenham', '2026-11-07T12:30:00Z', cutoff)
    assert f == {'source': '5DollarFootballAPI', 'form': {'home': 'WDL', 'away': 'LDW'}}
    assert f5.fundamentals(Client(), 'j1', 'Leeds', 'Tottenham', '2026-11-07T12:30:00Z', cutoff) == {}


def test_asian_coverage_requires_a_single_account_visible_league_id(tmp_path, monkeypatch):
    c = mk(tmp_path)
    entries = [{'id': 3836571083}, {'id': 2270596712}, {'id': 3492701972}, {'id': 3594495926}]
    monkeypatch.setattr(c, 'get', lambda *_args, **_kwargs: entries)
    available = c.available_leagues()
    assert available['j1'] == 3836571083 and available['k1'] == 2270596712
    assert 'spl' not in available and 'j2' not in available


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
