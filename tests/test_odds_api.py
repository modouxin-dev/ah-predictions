import json
from pathlib import Path

import pytest

from ah import odds_api, secrets
from ah.fetch_odds import cross_check_odds_api

FIX = json.loads((Path(__file__).parent / "data" / "odds_api_spreads.json").read_text())


class Resp:
    def __init__(self, data, status=200, headers=None):
        self._d, self.status_code = data, status
        self.headers = headers or {"x-requests-remaining": "480", "x-requests-used": "20"}

    def json(self):
        return self._d


class Sess:
    def __init__(self, data, **kw):
        self.data, self.kw, self.calls = data, kw, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return Resp(self.data, **self.kw)


def test_secrets_env_then_file(tmp_path, monkeypatch):
    f = tmp_path / "s.env"
    f.write_text('# c\nexport ODDS_API_KEY="abc"\nFOO=bar\n')
    monkeypatch.setenv("AH_SECRETS_FILE", str(f))
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    assert secrets.get_secret("ODDS_API_KEY") == "abc"
    monkeypatch.setenv("ODDS_API_KEY", "env")
    assert secrets.get_secret("ODDS_API_KEY") == "env"
    assert secrets.get_secret("NOPE") is None


def test_normalize_and_consensus():
    b = odds_api.normalize_spreads(FIX[0])
    assert len(b) == 3
    p = b[0]
    assert p["home_line"] == -0.25 and p["ah_home_line"] == 0.25 and p["home_hk"] == 0.95
    c = odds_api.consensus(b)
    assert c["home_line"] == -0.25 and c["bookmakers"] == 3 and c["agree"] == 2
    assert odds_api.is_quarter(-0.25) and not odds_api.is_quarter(-0.5)


def test_usage_and_budget(tmp_path):
    uf = tmp_path / "u.json"
    s = Sess(FIX)
    c = odds_api.OddsAPI(key="k", session=s, usage_file=uf, regions="eu")
    assert "k" not in repr(c)
    c.odds("soccer_uefa_champs_league")
    assert json.loads(uf.read_text())["remaining"] == 480
    assert s.calls[0][1]["markets"] == "spreads"
    uf.write_text(json.dumps({"remaining": 20}))
    with pytest.raises(odds_api.BudgetExceeded):
        c.odds("soccer_epl")
    c.events("soccer_epl")  # 免费端点不受预算限制


def test_http_error_hides_key(tmp_path):
    c = odds_api.OddsAPI(key="SECRETKEY", session=Sess({}, status=401), usage_file=tmp_path / "u.json")
    with pytest.raises(RuntimeError) as e:
        c.events("soccer_epl")
    assert "SECRETKEY" not in str(e.value)


def test_cross_check_graceful(tmp_path):
    c = odds_api.OddsAPI(key="k", session=Sess(FIX), usage_file=tmp_path / "u.json")
    out = cross_check_odds_api("soccer_uefa_champs_league", client=c)
    assert out["oddsapi-ev1"]["consensus"]["bookmakers"] == 3
    bad = odds_api.OddsAPI(key="k", session=Sess({}, status=500), usage_file=tmp_path / "u2.json")
    assert cross_check_odds_api("x", client=bad) == {}
    assert cross_check_odds_api("x", client=odds_api.OddsAPI(key="", usage_file=tmp_path / "u3.json")) == {}


def test_consensus_mode_tie_breaks():
    lines = [
        {"home_line": -0.5, "home_hk": 0.9, "away_hk": 0.95},
        {"home_line": -0.25, "home_hk": 0.85, "away_hk": 1.0},
        {"home_line": -0.5, "home_hk": 0.92, "away_hk": 0.93},
        {"home_line": -0.25, "home_hk": 0.88, "away_hk": 0.98},
    ]
    assert odds_api.consensus(lines, pinnacle_main=-0.25)["home_line"] == -0.25
    assert odds_api.consensus(lines, pinnacle_main=-0.5)["home_line"] == -0.5
    assert odds_api.consensus(lines)["home_line"] == -0.25  # 无 Pinnacle：|line| 较小
