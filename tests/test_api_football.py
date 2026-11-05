"""API-Football 客户端/映射/回填的离线测试（录制数据位于 tests/data/apif，pytest 中不发真实请求）。"""
import copy
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ah import api_football as af
from ah.analyze import Candidate, apply_enrichment
from ah.enrich import enrich, injury_feature
from ah.fetch_fixtures import fetch_any, fetch_api
from ah.match import find_fixture, map_titan, team_similarity
from ah.settle import autofill_results, settle_csv

D = Path(__file__).parent / "data" / "apif"


def load(name):
    return json.loads((D / name).read_text(encoding="utf-8"))


class Resp:
    def __init__(self, data, status=200, remaining="90"):
        self._d, self.status_code = data, status
        self.headers = {"x-ratelimit-requests-remaining": remaining}

    def json(self):
        return self._d


class FakeSession:
    """按 endpoint 返回录制数据；记录调用，用于断言缓存与预算。"""
    def __init__(self, routes, remaining="90"):
        self.routes, self.calls, self.remaining = routes, [], remaining

    def get(self, url, params=None, headers=None, timeout=None):
        assert headers and headers.get("x-apisports-key") == "test-key"
        ep = url.split(".io/", 1)[1]
        self.calls.append((ep, dict(params or {})))
        r = self.routes[ep]
        data = r(params) if callable(r) else r
        return Resp(copy.deepcopy(data), remaining=self.remaining)


class Clock:
    def __init__(self, t=datetime(2026, 11, 4, 11, 0, tzinfo=timezone.utc).timestamp()):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _isolate_apif_kill(monkeypatch, tmp_path):
    """仓库 sources.yaml enabled:false / 本地 flag 不应污染离线单测。"""
    monkeypatch.delenv("AH_API_FOOTBALL_DISABLED", raising=False)
    monkeypatch.setattr(af, "DISABLED_FLAG", tmp_path / "api_football_disabled.json")


def client(tmp_path, routes, remaining="90", clock=None):
    # check_yaml=False：单测不读仓库 sources.yaml（其中 enabled:false 会全局熔断）
    return af.Client(key="test-key", cache_dir=tmp_path, session=FakeSession(routes, remaining),
                     min_interval=0, now=clock or Clock(), check_yaml=False)


FX = load("fixtures_2026-11-04.json")


def test_repr_hides_key(tmp_path):
    c = client(tmp_path, {})
    assert "test-key" not in repr(c)


def test_fixtures_cached_and_budget_counted(tmp_path):
    c = client(tmp_path, {"fixtures": FX})
    a = c.fixtures_by_date("2026-11-04", [2, 17])
    b = c.fixtures_by_date("2026-11-04", [17])
    assert len(a) == 11 and len(b) == 2
    assert len(c.session.calls) == 1  # 第二次命中缓存
    u = json.loads((tmp_path / "api_usage.json").read_text())
    assert u["used"] == 1 and u["remaining"] == 90 and "test-key" not in json.dumps(u)


def test_cache_ttl_expiry(tmp_path):
    clk = Clock()
    c = client(tmp_path, {"fixtures": FX}, clock=clk)
    c.fixtures_by_date("2026-11-04")
    clk.t += 6 * 3600 + 1
    c.fixtures_by_date("2026-11-04")
    assert len(c.session.calls) == 2


def test_budget_reserve_refuses(tmp_path):
    c = client(tmp_path, {"fixtures": FX, "injuries": load("injuries_1635656.json")}, remaining="11")
    c.fixtures_by_date("2026-11-04")          # 本地记录 used=1，header 剩余 11
    c.session.remaining = "10"
    c.injuries(1635656)                        # 剩余 11 > 10，允许；之后 header=10
    with pytest.raises(af.BudgetExceeded):
        c.h2h(212, 492)
    # 结算属于 essential，可动用预留
    c.session.routes["fixtures"] = lambda p: {"errors": [], "response": [FX["response"][0]]}
    assert c.fixture_result(1629973) is not None


def test_usage_resets_next_day(tmp_path):
    clk = Clock()
    c = client(tmp_path, {"fixtures": FX}, clock=clk)
    c.fixtures_by_date("2026-11-04")
    clk.t += 86400
    assert c.usage()["used"] == 0 and c.remaining() == 100


def test_plan_errors_raise(tmp_path):
    c = client(tmp_path, {"standings": load("err_season.json"), "fixtures/headtohead": load("err_last.json")})
    with pytest.raises(af.ApiFootballError, match="try from 2022 to 2024"):
        c.standings(17, 2026)
    assert not list((tmp_path).glob("standings/*.json"))  # 错误不缓存


def test_h2h_no_last_param_and_local_slice(tmp_path):
    hist = []
    for i in range(12):
        f = copy.deepcopy(FX["response"][5])  # Porto v Napoli
        f["fixture"]["id"] = 1000 + i
        f["fixture"]["date"] = f"20{10 + i}-03-01T20:00:00+00:00"
        f["fixture"]["status"]["short"] = "FT"
        f["goals"] = {"home": i % 3, "away": 1}
        hist.append(f)
    hist.append(FX["response"][5])  # 本场未开赛，应被排除
    c = client(tmp_path, {"fixtures/headtohead": {"errors": [], "response": hist}})
    r = c.h2h(492, 212, last=10)
    ep, params = c.session.calls[0]
    assert "last" not in params and params["h2h"] == "212-492"
    assert len(r) == 10 and r[0]["fixture"]["date"].startswith("2021")
    s = af.summarize_h2h(r, 212)
    assert s["played"] == 10 and s["w"] + s["d"] + s["l"] == 10


def test_injuries_dedup():
    inj = af.summarize_injuries(load("injuries_1635656.json")["response"])
    porto = [p["player"] for p in inj["FC Porto"]]
    assert len(porto) == len(set(porto)) == 6
    assert injury_feature(inj, "FC Porto", "Napoli", "home") > 0


def test_main_ah_line_prefers_pinnacle():
    m = af.main_ah_line(load("odds_1635656.json")["response"])
    assert m["bookmaker"] == "Pinnacle" and m["home_line"] == -0.5
    assert m["home_odds"] == 1.92 and m["away_hk"] == 0.99
    assert len(af.asian_handicap_from_odds(load("odds_1635656.json")["response"])) > 50


def test_lineups_only_cached_when_present(tmp_path):
    state = {"resp": []}
    c = client(tmp_path, {"fixtures/lineups": lambda p: {"errors": [], "response": state["resp"]}})
    assert c.lineups(1629973) == []
    state["resp"] = load("lineups_1629973.json")["response"]
    assert len(c.lineups(1629973)) == 2
    assert len(c.lineups(1629973)) == 2
    assert len(c.session.calls) == 2


def test_summarize_result_uses_90min_score():
    f = copy.deepcopy(FX["response"][0])
    f["fixture"]["status"]["short"] = "AET"
    f["goals"] = {"home": 2, "away": 1}
    f["score"]["fulltime"] = {"home": 1, "away": 1}
    f["score"]["extratime"] = {"home": 1, "away": 0}
    r = af.summarize_result(f)
    assert (r["home_goals"], r["away_goals"]) == (1, 1) and r["finished"] and r["had_extra_time"]


def test_form_from_fixtures():
    fs = []
    for i, (hg, ag) in enumerate([(2, 0), (1, 1), (0, 3), (4, 1), (1, 0), (0, 0)]):
        f = copy.deepcopy(FX["response"][5])
        f["fixture"]["date"] = f"2026-10-{10 + i:02d}T20:00:00+00:00"
        f["fixture"]["status"]["short"] = "FT"
        f["goals"] = {"home": hg, "away": ag}
        fs.append(f)
    assert af.form_from_fixtures(fs, 212, n=5) == "DLWWD"
    assert af.form_from_fixtures(fs, 492, n=3) == "LLD"


# ---------- 映射 ----------
def test_team_similarity():
    assert team_similarity("Kashima", "鹿岛鹿角 Kashima Antlers") >= 0.9
    assert team_similarity("FC Porto", "波尔图") == 1.0
    assert team_similarity("Buriram United", "武里南联 Buriram United") == 1.0
    assert team_similarity("Fenerbahçe", "Fenerbahce") == 1.0
    assert team_similarity("Real Madrid", "Real Betis") < 0.75


def test_find_fixture_time_tolerance():
    fxs = FX["response"]
    f = find_fixture(fxs, "武里南联 Buriram United", "鹿岛鹿角 Kashima Antlers", "2026-11-04T12:15:00Z")
    assert f["fixture"]["id"] == 1629974
    assert find_fixture(fxs, "波尔图 Porto", "那不勒斯 Napoli", "2026-11-04T20:14:00Z")["fixture"]["id"] == 1635656
    assert find_fixture(fxs, "波尔图 Porto", "那不勒斯 Napoli", "2026-11-04T20:16:00Z") is None
    # 同时段不同比赛不能串
    assert find_fixture(fxs, "莱比锡", "曼城", "2026-11-04T20:00:00Z")["fixture"]["id"] == 1635710


def test_map_titan_bjt_across_midnight():
    titan = [{"match_id": "titan007-3073152", "home": "波尔图", "away": "那不勒斯", "kickoff_bjt": "04:00"},
             {"match_id": "titan007-3068888", "home": "武里南联", "away": "鹿岛鹿角", "kickoff_bjt": "20:15"},
             {"match_id": "titan007-x", "home": "某队", "away": "另一队", "kickoff_bjt": "20:15"}]
    m = map_titan(FX["response"], titan, "2026-11-04")
    assert m == {"titan007-3073152": 1635656, "titan007-3068888": 1629974}


# ---------- fetch_fixtures ----------
def test_fetch_api_filters_configured_leagues(tmp_path):
    rows = fetch_api("2026-11-04", client(tmp_path, {"fixtures": FX}))
    assert len(rows) == 11 and {r["league_id"] for r in rows} == {2, 17}
    r = next(x for x in rows if x["api_fixture_id"] == 1629974)
    assert r["league"] == "亚冠精英" and r["kickoff_utc"] == "2026-11-04T12:15:00Z" and r["match_id"] == "apif-1629974"


def test_fetch_any_falls_back_to_titan(tmp_path):
    c = af.Client(key="", cache_dir=tmp_path, session=FakeSession({}), min_interval=0)
    src, rows = fetch_any("2026-11-04", client=c, titan_fetch=lambda: [{"match_id": "titan007-1"}])
    assert src == "titan007" and rows == [{"match_id": "titan007-1"}]


# ---------- enrich / analyze ----------
def test_enrich_and_apply(tmp_path):
    routes = {"fixtures": FX, "injuries": load("injuries_1635656.json"),
              "fixtures/headtohead": {"errors": [], "response": [FX["response"][5]]},
              "odds": load("odds_1635656.json")}
    c = client(tmp_path, routes)
    fx = next(r for r in fetch_api("2026-11-04", c) if r["api_fixture_id"] == 1635656)
    e = enrich(fx, "home", c)
    assert e["errors"] == {} and e["ah_market"]["home_line"] == -0.5
    assert e["h2h"]["played"] == 0 and e["suggested_features"]["h2h"] == 0
    cand = Candidate(match_id="m", league="L", home="FC Porto", away="Napoli", kickoff_utc=fx["kickoff_utc"],
                     pick_side="home", line=-0.75, odds=1.06, features={"injuries": 2})
    apply_enrichment(cand, e)
    assert cand.features["injuries"] == 2  # 手工值不被覆盖
    assert "API-Football fixture 1635656" in cand.sources


def test_enrich_survives_plan_error(tmp_path):
    routes = {"fixtures": FX, "fixtures/headtohead": load("err_last.json"), "odds": load("odds_1635656.json")}
    c = client(tmp_path, routes)
    fx = next(r for r in fetch_api("2026-11-04", c) if r["api_fixture_id"] == 1629974)
    e = enrich(fx, "away", c, injuries_covered=False)
    assert "h2h" in e["errors"] and e["injuries"] is None and e["ah_market"]


# ---------- settle autofill ----------
CSV_HDR = "date,match_id,league,home,away,kickoff_utc,pick_side,pick,line,odds_if_known,confidence,result,ah_outcome,units,settled\n"


def _finished(fid, hg, ag):
    f = copy.deepcopy(next(x for x in FX["response"] if x["fixture"]["id"] == fid))
    f["fixture"]["status"]["short"] = "FT"
    f["goals"] = {"home": hg, "away": ag}
    f["score"]["fulltime"] = {"home": hg, "away": ag}
    return f


def test_autofill_and_settle(tmp_path):
    p = tmp_path / "predictions.csv"
    p.write_text(CSV_HDR +
                 "2026-11-04,titan007-3068888,ACL,武里南联 Buriram United,鹿岛鹿角 Kashima Antlers,2026-11-04T12:15:00Z,away,鹿岛鹿角,-0.75,0.78,medium,,,,false\n"
                 "2026-11-04,titan007-3073152,UCL,波尔图 Porto,那不勒斯 Napoli,2026-11-04T20:00:00Z,home,波尔图,-0.75,1.06,medium,,,,false\n",
                 encoding="utf-8")
    results = {"1629974": _finished(1629974, 0, 1)}

    def fixtures_route(params):
        if "id" in params:
            return {"errors": [], "response": [results.get(str(params["id"]), FX["response"][5])]}
        return FX

    c = client(tmp_path / "cache", {"fixtures": fixtures_route})
    now = datetime(2026, 11, 4, 14, 30, tzinfo=timezone.utc)  # 鹿岛已完场，波尔图未开赛
    log = autofill_results(p, client=c, now=now)
    assert [e["action"] for e in log] == ["filled"]
    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    assert rows[0]["result"] == "0-1" and rows[1]["result"] == ""
    assert settle_csv(p) == 1
    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    assert rows[0]["ah_outcome"] == "half_win" and rows[0]["settled"] == "true"
    # 波尔图开赛后但 API 仍未完场 → pending，不写比分
    log = autofill_results(p, client=c, now=datetime(2026, 11, 4, 22, 0, tzinfo=timezone.utc))
    assert [e["action"] for e in log] == ["pending"]


def test_kill_switch_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AH_API_FOOTBALL_DISABLED", "1")
    c = client(tmp_path, {"fixtures": FX})
    assert c.enabled is False
    with pytest.raises(af.ApiFootballError, match="已停用"):
        c.fixtures_by_date("2026-11-04")
    assert c.session.calls == []


def test_kill_switch_flag_file(tmp_path, monkeypatch):
    monkeypatch.delenv("AH_API_FOOTBALL_DISABLED", raising=False)
    monkeypatch.setattr(af, "DISABLED_FLAG", tmp_path / "api_football_disabled.json")
    af.mark_disabled("account suspended test")
    c = client(tmp_path, {"fixtures": FX})
    assert c.enabled is False
    with pytest.raises(af.ApiFootballError, match="已停用"):
        c.fixtures_by_date("2026-11-04")
    assert c.session.calls == []


def test_suspended_response_trips_breaker(tmp_path, monkeypatch):
    monkeypatch.delenv("AH_API_FOOTBALL_DISABLED", raising=False)
    monkeypatch.setattr(af, "DISABLED_FLAG", tmp_path / "api_football_disabled.json")
    routes = {"status": {"errors": {"access": "Your account is suspended, check on https://dashboard.api-football.com."}, "response": {}}}
    c = client(tmp_path, routes)
    with pytest.raises(af.ApiFootballError, match="suspend"):
        c.get("status", force=True)
    assert af.DISABLED_FLAG.exists()
    with pytest.raises(af.ApiFootballError, match="已停用"):
        c.get("status", force=True)
    assert len(c.session.calls) == 1  # 第二次被本地熔断，不再发请求
