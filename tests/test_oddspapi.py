import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ah import consensus, oddspapi as op
from ah.render_report import validate

D = json.loads((Path(__file__).parent / "data" / "oddspapi_sample.json").read_text())
NOW = lambda: datetime(2026, 11, 4, tzinfo=timezone.utc)  # noqa: E731


class R:
    def __init__(self, data, status=200):
        self.d, self.status_code = data, status

    def json(self):
        return self.d


class S:
    def __init__(self, seq=None):
        self.seq, self.calls = list(seq or []), []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if self.seq:
            return self.seq.pop(0)
        if url.endswith("/account"):
            return R({"api_key": "SECRET", "subscriptions": [{"is_active": True, "request_count": 7, "request_limit": 250}]})
        return R(D["odds"] if url.endswith("/odds") else D["fixtures"])


def mk(tmp_path, seq=None, reserve=30):
    return op.Client(key="KEY", session=S(seq), cache_dir=tmp_path / "c", usage_file=tmp_path / "u.json",
                     reserve=reserve, sleep=lambda s: None, now=NOW)


def test_normalize_main_line():
    b = op.normalize_ah(D["odds"])
    pin = next(x for x in b if x["bookmaker"] == "pinnacle")
    assert pin["home_line"] == 0.25 and pin["home_odds"] == 2.07 and pin["away_hk"] == 0.8
    assert {x["bookmaker"] for x in b} == {"pinnacle", "sbobet", "singbet"}
    assert op.normalize_ah({"bookmakerOdds": {"x": {"markets": {"101": {}}}}}) == []


def test_usage_budget_cache_and_key_hygiene(tmp_path):
    c = mk(tmp_path)
    assert "KEY" not in repr(c)
    c.odds("id1"); c.odds("id1")
    assert len(c.s.calls) == 1 and c.usage()["used"] == 1
    a = c.account()
    assert "api_key" not in a
    assert c.sync_usage()["used"] == 7          # /account 不计数
    (tmp_path / "u.json").write_text(json.dumps({"month": "2026-11", "used": 221, "limit": 250}))
    with pytest.raises(op.BudgetExceeded):
        c.odds("id2")
    c.historical_odds("id2")                    # 免费端点不受预算限制


def test_error_hides_key(tmp_path):
    with pytest.raises(op.OddsPapiError) as e:
        mk(tmp_path, seq=[R({}, 500)]).fixtures(463, NOW(), NOW())
    assert "KEY" not in str(e.value)


def test_attach_acl_pick(tmp_path):
    leagues = [{"id": "acl_elite", "name": "AFC Champions League Elite", "cn": "亚冠精英"}]
    r = {"date": "2026-11-04", "generated_at": "2026-11-04T00:00:00Z", "watched": [], "picks": [
        {"match_id": "t", "league": "AFC Champions League Elite（亚冠精英·东区）", "home": "武里南联 Buriram United",
         "away": "鹿岛鹿角 Kashima Antlers", "kickoff_utc": "2026-11-04T12:15:00Z", "pick_side": "away", "line": -0.75,
         "odds": 0.9, "confidence": "medium", "reasons": ["x"], "risks": [], "sources": ["https://a.example/"]}]}
    calls = []
    c = mk(tmp_path)
    log = consensus.attach(r, fetch=lambda s: calls.append(s) or {}, aliases={}, leagues=leagues, papi=c)
    assert calls == []                               # The Odds API 不覆盖亚冠，不调用
    mc = r["picks"][0]["market_consensus"]
    assert mc["source"] == "OddsPapi" and mc["bookmakers"] == 3 and mc["pick_line"] == -0.25
    assert mc["pinnacle"]["pick_line"] == -0.25 and log["oddspapi:acl_elite"] == 1
    assert len(c.s.calls) == 2                       # 1 次 fixtures + 1 次 odds
    validate(r)


def test_attach_budget_exhausted_is_graceful(tmp_path):
    (tmp_path / "u.json").write_text(json.dumps({"month": "2026-11", "used": 240, "limit": 250}))
    r = {"picks": [{"match_id": "t", "league": "AFC Champions League Elite", "home": "Buriram United",
                    "away": "Kashima Antlers", "kickoff_utc": "2026-11-04T12:15:00Z", "pick_side": "away",
                    "sources": []}]}
    log = consensus.attach(r, fetch=lambda s: {}, aliases={},
                           leagues=[{"id": "acl_elite", "name": "AFC Champions League Elite"}], papi=mk(tmp_path))
    assert log["oddspapi:acl_elite"] == "error" and "market_consensus" not in r["picks"][0]
