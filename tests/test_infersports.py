"""InferSports 客户端与 odds_snapshots 接入：全部 mock，不发真实请求。"""
import csv
import json
from datetime import datetime, timezone

import pytest

from ah import infersports as inf
from ah import odds_snapshots as snap

UTC = timezone.utc
ODDS = {"event_id": "evt_A", "as_of": "2026-11-04T19:50:00Z", "stale": False, "odds": [
    {"bookmaker": "sbobet", "market_type": "asian_handicap", "period": "full_time", "line": -0.5,
     "prices": {"home": 1.89, "away": 2.01}, "status": "open", "as_of": "2026-11-04T19:49:00Z"},
    {"bookmaker": "sbobet", "market_type": "asian_handicap", "period": "full_time", "line": -0.25,
     "prices": {"home": 1.70, "away": 2.24}, "status": "open", "as_of": "2026-11-04T19:49:00Z"},
    {"bookmaker": "crown", "market_type": "asian_handicap", "period": "full_time", "line": -0.75,
     "prices": {"home": 0.0, "away": 0.0}, "status": "suspended", "as_of": "2026-11-04T10:00:00Z"},
    {"bookmaker": "crown", "market_type": "asian_handicap", "period": "full_time", "line": -0.5,
     "prices": {"home": 1.93, "away": 1.95}, "status": "open", "as_of": "2026-11-04T19:48:00Z"},
    {"bookmaker": "crown", "market_type": "asian_handicap", "period": "half_time", "line": -0.25,
     "prices": {"home": 1.9, "away": 1.9}, "status": "open", "as_of": "2026-11-04T19:48:00Z"},
    {"bookmaker": "crown", "market_type": "totals", "period": "full_time", "line": 2.5,
     "prices": {"over": 1.9, "under": 1.9}, "status": "open", "as_of": "2026-11-04T19:48:00Z"}]}
OPENING = {"event_id": "evt_A", "opening": [
    {"bookmaker": "sbobet", "market_type": "asian_handicap", "period": "full_time", "line": -0.25,
     "prices": {"home": 1.95, "away": 1.93}, "opened_at": "2026-11-03T20:00:00Z"},
    {"bookmaker": "hkjc", "market_type": "asian_handicap", "period": "full_time", "line": -0.5,
     "prices": {"home": 1.99, "away": 1.77}, "opened_at": "2026-11-04T21:00:00Z"},   # 晚于开球 → 丢弃
    {"bookmaker": "sbobet", "market_type": "asian_handicap", "period": "half_time", "line": 0.0,
     "prices": {"home": 1.9, "away": 1.9}, "opened_at": "2026-11-03T20:00:00Z"}]}
EVENTS = {"data": [{"id": "evt_A", "league": {"name": "UCL"}, "home_team": {"name": "Porto"},
                    "away_team": {"name": "Napoli"}, "scheduled_at": "2026-11-04T20:00:00Z", "status": "scheduled"}],
          "page": {"next_cursor": None, "limit": 200}}


def test_ah_ladders_drops_suspended_halftime_and_other_markets():
    lad = inf.ah_ladders(ODDS)
    assert sorted(lad) == ["crown", "sbobet"]
    assert lad["crown"] == [(-0.5, 1.93, 1.95, "2026-11-04T19:48:00Z")]
    assert {x[0] for x in lad["sbobet"]} == {-0.5, -0.25}


def test_ah_opening_full_time_only():
    op = inf.ah_opening(OPENING)
    assert op["sbobet"] == (-0.25, 1.95, 1.93, "2026-11-03T20:00:00Z") and "hkjc" in op


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._b, self.headers = status, body, headers or {}

    def json(self):
        return self._b


class Sess:
    def __init__(self, resps):
        self.resps, self.calls = list(resps), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        return self.resps.pop(0)


class Clock:
    def __init__(self, t=1_793_860_000.0):
        self.t, self.slept = t, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def client(tmp_path, resps, key="", **kw):
    c = Clock()
    return inf.Client(key=key, session=Sess(resps), cache_dir=tmp_path / "c", usage_file=tmp_path / "u.json",
                      sleep=c.sleep, clock=c, **kw), c


def test_keyless_no_auth_header_min_interval_and_cache(tmp_path):
    cl, clock = client(tmp_path, [Resp(200, ODDS), Resp(200, OPENING)])
    assert cl.tier == "keyless" and cl.remaining() == 200
    cl.odds("evt_A")
    cl.odds("evt_A")                                   # 90 秒内走缓存，不再请求
    assert len(cl.s.calls) == 1 and "Authorization" not in cl.s.calls[0][2]
    cl.opening("evt_A")
    assert clock.slept and clock.slept[-1] == pytest.approx(10.0)    # 同站点 ≥10 秒
    assert cl.usage()["used"] == 2 and cl.usage()["tier"] == "keyless"
    clock.t += 91                                      # 过了 90 秒新鲜度 → 重新请求
    cl.s.resps.append(Resp(200, ODDS))
    cl.odds("evt_A")
    assert len(cl.s.calls) == 3


def test_key_from_env_sent_as_bearer_but_never_in_repr(tmp_path):
    cl, _ = client(tmp_path, [Resp(200, EVENTS)], key="isk_secret")
    cl.events("2026-11-04")
    assert cl.s.calls[0][2]["Authorization"] == "Bearer isk_secret"
    assert "isk_secret" not in repr(cl) and cl.remaining() == 4999


def test_reserve_blocks_requests(tmp_path):
    cl, clock = client(tmp_path, [])
    (tmp_path / "u.json").write_text(json.dumps({"day": inf._utc_day(clock()), "used": 181}))
    with pytest.raises(inf.BudgetExceeded):
        cl.odds("evt_A")
    assert cl.s.calls == []


def test_429_minute_retry_and_daily_stop(tmp_path):
    cl, clock = client(tmp_path, [Resp(429, {}, {"Retry-After": "3"}), Resp(200, ODDS)])
    assert cl.odds("evt_A")["event_id"] == "evt_A" and cl.backoffs == [3.0]
    cl2, _ = client(tmp_path / "b", [Resp(429, {}, {"Retry-After": "7200", "X-RateLimit-Scope": "daily"})])
    with pytest.raises(inf.BudgetExceeded):
        cl2.odds("evt_B")
    assert len(cl2.s.calls) == 1 and cl2.remaining() == 0


class FakeInfs:
    enabled = True

    def __init__(self):
        self.used, self.calls = 0, []

    def usage(self):
        return {"used": self.used, "limit": 200, "tier": "keyless", "day": "2026-11-04"}

    def remaining(self):
        return 200 - self.used

    def events(self, day):
        self.calls.append(("events", day)); self.used += 1
        return EVENTS["data"]

    def odds(self, eid, ttl=None):
        self.calls.append(("odds", eid, ttl)); self.used += 1
        return ODDS

    def opening(self, eid):
        self.calls.append(("opening", eid)); self.used += 1
        return OPENING


def run(tmp_path, phases, now, sources=snap.ALL_SOURCES, infs=None):
    from test_odds_snapshots import AH, LEAGUES, FakeApif, FakeOddsAPI, FakePapi, matches
    infs = infs or FakeInfs()
    s = snap.Snapshotter("2026-11-05", papi=FakePapi(), oddsapi=FakeOddsAPI(), apif=FakeApif(), now=lambda: now,
                         snap_dir=tmp_path / "s", ah_markets=AH, aliases={}, leagues=LEAGUES,
                         odds_api_cache=tmp_path / "oa", log=lambda m: None, sources=sources, infs=infs)
    res = s.run(matches()[:1], phases)
    with (tmp_path / "s" / "2026-11-05.csv").open() as fh:
        return list(csv.DictReader(fh)), res, infs


def test_snapshot_infersports_report_and_open(tmp_path):
    rows, res, infs = run(tmp_path, ["report", "open"], datetime(2026, 11, 4, 19, 0, tzinfo=UTC))
    inf_rows = [r for r in rows if r["source"] == "infersports"]
    rep = {(r["bookmaker"], r["ah_home_line"]): r for r in inf_rows if r["stage"] == "report"}
    assert rep[("sbobet", "-0.5")]["is_main"] == "1" and rep[("sbobet", "-0.25")]["is_main"] == "0"
    assert rep[("sbobet", "-0.5")]["price_ts"] == "2026-11-04T19:49:00Z" and rep[("sbobet", "-0.5")]["source_ref"] == "evt_A"
    # oddspapi sbobet 报告时主线 -0.5 @1.92/1.96 与 infersports -0.5 @1.89/2.01 价差 ≤0.06 → verified
    sb = [r for r in rows if r["stage"] == "report" and r["bookmaker"] == "sbobet" and r["is_main"] == "1"]
    assert {r["source"] for r in sb} == {"oddspapi", "infersports"} and {r["verification"] for r in sb} == {"verified"}
    op = [r for r in inf_rows if r["stage"] == "open"]
    assert [(r["bookmaker"], r["ah_home_line"], r["as_of"]) for r in op] == [("sbobet", "-0.25", "2026-11-03T20:00:00Z")]
    assert res["verification"][("apif-100", "open")]["verification"] == "verified"   # 初盘第一次有第二来源
    assert res["calls"]["infersports"] == 3 and infs.calls[0] == ("events", "2026-11-04")
    m = json.loads((tmp_path / "s" / "2026-11-05.map.json").read_text())
    assert m["apif-100"]["infersports"] == "evt_A"


def test_infersports_opt_in_and_not_used_after_kickoff_or_backfill(tmp_path):
    # 用户 2026-11-05 确认进默认；显式 --sources 不含 infersports 时才不调用
    rows, _, infs = run(tmp_path, ["report"], datetime(2026, 11, 4, 19, 0, tzinfo=UTC), sources=snap.SOURCES)
    assert [c for c in infs.calls if c[0] == "odds"] and [r for r in rows if r["source"] == "infersports"]
    legacy = tuple(s for s in snap.SOURCES if s != "infersports")
    rows, _, infs = run(tmp_path / "off", ["report"], datetime(2026, 11, 4, 19, 0, tzinfo=UTC), sources=legacy)
    assert not infs.calls and not [r for r in rows if r["source"] == "infersports"]
    rows, _, infs = run(tmp_path / "b", ["close"], datetime(2026, 11, 4, 20, 30, tzinfo=UTC))
    assert not [c for c in infs.calls if c[0] == "odds"]
    rows, _, infs = run(tmp_path / "c", ["close"], datetime(2026, 11, 4, 19, 56, tzinfo=UTC))
    assert ("odds", "evt_A", 0) in infs.calls      # 收盘强制不走缓存


def test_infersports_no_match_reason(tmp_path):
    class NoEv(FakeInfs):
        def events(self, day):
            self.used += 1
            return []
    rows, res, _ = run(tmp_path, ["report"], datetime(2026, 11, 4, 19, 0, tzinfo=UTC), infs=NoEv())
    assert "infersports:no_event_match" in res["reasons"]["apif-100|report"]
