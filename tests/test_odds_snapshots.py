"""盘口快照（ah.odds_snapshots）：全部用假客户端/mock，不耗任何额度。"""
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from ah import odds_snapshots as snap
from ah import oddspapi as op

UTC = timezone.utc
AH = {"1060": {"h": -0.5, "o": {"1060": "1", "1061": "2"}},
      "1062": {"h": -0.25, "o": {"1062": "1", "1063": "2"}},
      "1064": {"h": 0.0, "o": {"1064": "1", "1065": "2"}},
      "1066": {"h": -1.5, "o": {"1066": "1", "1067": "2"}}}
LEAGUES = [{"id": "ucl", "name": "UEFA Champions League", "cn": "欧冠"},
           {"id": "acl_two", "name": "AFC Champions League Two", "cn": "亚冠联2"}]
NOW = datetime(2026, 11, 4, 19, 0, tzinfo=UTC)


def e(ts, price, active=True):
    return {"createdAt": ts, "price": price, "active": active}


def mkt(home_hist, away_hist):
    return {"outcomes": {"a": {"players": {"0": home_hist}}, "b": {"players": {"0": away_hist}}}}


def hist_book():
    # 开盘 11-03 20:30：主盘 -0.25（1.95/1.95）；报告时 11-04 12:00 主盘 -0.5（1.92/1.96）；开赛前最后 19:55 -0.5（1.88/2.02）
    m = {}
    for mid, (hh, ah_) in {
        "1062": ([e("2026-11-03T20:30:00Z", 1.95), e("2026-11-04T11:00:00Z", 1.70)],
                 [e("2026-11-03T20:30:00Z", 1.95), e("2026-11-04T11:00:00Z", 2.25)]),
        "1060": ([e("2026-11-03T20:31:00Z", 2.30), e("2026-11-04T11:00:00Z", 1.92), e("2026-11-04T19:55:00Z", 1.88),
                  e("2026-11-04T20:05:00Z", 1.50)],
                 [e("2026-11-03T20:31:00Z", 1.65), e("2026-11-04T11:00:00Z", 1.96), e("2026-11-04T19:55:00Z", 2.02),
                  e("2026-11-04T20:05:00Z", 2.60)]),
        "1066": ([e("2026-11-03T20:30:00Z", 4.0), e("2026-11-04T09:00:00Z", 4.0, False)],
                 [e("2026-11-03T20:30:00Z", 1.2), e("2026-11-04T09:00:00Z", 1.2, False)]),
    }.items():
        h = AH[mid]
        oid1, oid2 = list(h["o"])
        m[mid] = {"outcomes": {oid1: {"players": {"0": hh}}, oid2: {"players": {"0": ah_}}}}
    return {"markets": m}


def test_hist_ladder_open_and_close():
    b = hist_book()
    t0 = snap.hist_open_ts(b, AH)
    assert t0 == datetime(2026, 11, 3, 20, 30, 0, tzinfo=UTC) or t0 == datetime(2026, 11, 3, 20, 31, tzinfo=UTC)
    lad = snap.hist_ladder(b, AH, datetime(2026, 11, 3, 20, 31, tzinfo=UTC))
    assert snap.pick_main(lad)[:3] == (-0.25, 1.95, 1.95)
    close = snap.hist_ladder(b, AH, datetime(2026, 11, 4, 19, 59, 59, tzinfo=UTC))
    assert snap.pick_main(close)[:3] == (-0.5, 1.88, 2.02)
    assert all(x[0] != -1.5 for x in close)            # 已关盘（inactive）的线不出现
    # 开赛后的价格不会进入收盘
    assert snap.pick_main(snap.hist_ladder(b, AH, datetime(2026, 11, 4, 19, 59, tzinfo=UTC)))[1] == 1.88


def test_ladder_rows_window():
    lad = [(-1.0, 2.5, 1.5, None), (-0.75, 2.2, 1.7, None), (-0.5, 1.9, 1.95, None), (-0.25, 1.7, 2.2, None),
           (0.25, 1.4, 2.9, None)]
    rows = snap.ladder_rows(lad, 0.5)
    assert [x[0][0] for x in rows] == [-1.0, -0.75, -0.5, -0.25]
    assert [m for _, m in rows].count(True) == 1 and rows[2][1]


def row(mid, ph, src, book, line, main="1", oh=1.90, oa=1.90):
    return {"match_id": mid, "stage": ph, "source": src, "bookmaker": book, "ah_home_line": line, "is_main": main,
            "odds_home": oh, "odds_away": oa}


def test_assign_status_levels():
    rows = [row("a", "report", "oddspapi", "pinnacle", -0.5), row("a", "report", "odds_api", "pinnacle", -0.5),
            row("a", "report", "odds_api", "1xbet", -0.75), row("a", "report", "odds_api", "betfair", -0.5),
            row("b", "report", "oddspapi", "pinnacle", -1.0), row("b", "report", "api_football", "pinnacle", -1.25),
            row("c", "open", "oddspapi", "sbobet", 0.25),
            {"match_id": "d", "stage": "report", "source": "", "bookmaker": "", "ah_home_line": "", "is_main": ""}]
    st = snap.assign_status(rows)
    assert st[("a", "report")]["verification"] == "verified"
    assert rows[0]["verification"] == "verified" and rows[2]["verification"] == "single"
    assert st[("b", "report")]["verification"] == "conflict" and rows[4]["verification"] == "conflict"
    assert st[("c", "open")]["verification"] == "single"
    assert st[("d", "report")]["verification"] == "missing" and rows[-1]["verification"] == "missing"


def test_same_market_is_not_verified():
    def r(src, line, oh, oa, main="1"):
        return {"match_id": "m", "stage": "report", "source": src, "bookmaker": "pinnacle", "ah_home_line": line,
                "odds_home": oh, "odds_away": oa, "is_main": main}
    # 主盘线不同（1.0 vs 1.25），但 1.0 这条线两边都有、赔率差 ≤0.06 → conflict + same_market（不是 verified）
    rows = [r("odds_api", 1.0, 2.10, 1.84), r("oddspapi", 1.25, 1.833, 2.06),
            r("oddspapi", 1.0, 2.09, 1.85, "0"), r("oddspapi", 1.5, 1.66, 2.28, "0")]
    st = snap.assign_status(rows)
    assert st[("m", "report")]["verification"] == "conflict"
    assert "same_market" in rows[0]["status_detail"]
    assert "verified_ladder" not in snap.STATUSES
    # 同一条主线、赔率一致 → verified（+1.0 去水后更接近 0.5，是真正主线）
    rows = [r("odds_api", 1.0, 1.95, 1.95), r("oddspapi", 1.0, 1.96, 1.94),
            r("oddspapi", 1.25, 1.70, 2.20, "0")]
    st = snap.assign_status(rows)
    assert st[("m", "report")]["verification"] == "verified" and rows[0]["verification"] == "verified"
    assert rows[0]["is_main"] == "1" and rows[1]["is_main"] == "1"
    assert "same_market" not in rows[0]["status_detail"]
    # 同一条主线但赔率差 > 0.06 → conflict，标 same_line_price_diff
    rows[1]["odds_home"] = 2.30
    st = snap.assign_status(rows)
    assert st[("m", "report")]["verification"] == "conflict" and rows[0]["verification"] == "conflict"
    assert "same_line_price_diff" in rows[0]["status_detail"]


def test_pick_main_novig_leipzig_example():
    # apif-1635710：Pinnacle +1.25 @1.833/2.06 去水主队概率 0.529；+1.0 @2.10/1.84 → 0.467 → 主线 +1.25
    lad = [(1.0, 2.10, 1.84, None), (1.25, 1.833, 2.06, None), (0.75, 2.36, 1.653, None), (1.5, 1.662, 2.28, None)]
    assert snap.pick_main(lad)[:3] == (1.25, 1.833, 2.06)
    assert abs(snap.home_novig(1.833, 2.06) - 0.5) < abs(snap.home_novig(2.10, 1.84) - 0.5)


def test_single_line_book_is_main_only_if_matches_pinnacle():
    rows = [
        row("m", "report", "oddspapi", "pinnacle", 1.25, oh=1.833, oa=2.06),
        row("m", "report", "oddspapi", "pinnacle", 1.0, "0", oh=2.10, oa=1.84),
        row("m", "report", "oddspapi", "pinnacle", 1.5, "0", oh=1.66, oa=2.28),
        row("m", "report", "odds_api", "pinnacle", 1.0, oh=2.10, oa=1.84),
        row("m", "report", "odds_api", "1xbet", 1.5, oh=1.69, oa=2.36),
        row("m", "report", "odds_api", "matchbook", 1.25, oh=1.87, oa=2.12),
    ]
    snap.assign_status(rows)
    pin = {(r["source"], float(r["ah_home_line"])): r["is_main"] for r in rows if r["bookmaker"] == "pinnacle"}
    assert pin[("oddspapi", 1.25)] == "1" and pin[("oddspapi", 1.0)] == "0"
    assert pin[("odds_api", 1.0)] == "0"   # 单线 ≠ 主线
    assert next(r["is_main"] for r in rows if r["bookmaker"] == "1xbet") == "0"
    assert next(r["is_main"] for r in rows if r["bookmaker"] == "matchbook") == "1"  # 线等于主线


def test_match_level_consensus_differs_but_common_book_verified():
    # 共同公司 pinnacle 两源同线同价 → verified；各源其它公司让共识线不同，不影响
    rows = [row("y", "report", "oddspapi", "pinnacle", 1.25), row("y", "report", "odds_api", "pinnacle", 1.25),
            row("y", "report", "odds_api", "bet365", 1.0), row("y", "report", "oddspapi", "sbobet", 1.25)]
    st = snap.assign_status(rows)
    assert st[("y", "report")]["verification"] == "verified" and rows[0]["verification"] == "verified"
    # 共识线不同、没有共同公司：各公司都是 single → 场级 single（没有可核对的共同公司）
    rows = [row("z", "report", "oddspapi", "sbobet", 1.25), row("z", "report", "odds_api", "1xbet", 1.0)]
    assert snap.assign_status(rows)[("z", "report")]["verification"] == "single"
    # 两个完整盘口来源、主线不同、无共同公司 → conflict
    rows = [row("z2", "report", "oddspapi", "sbobet", 1.25), row("z2", "report", "api_football", "dafabet", 1.0)]
    assert snap.assign_status(rows)[("z2", "report")]["verification"] == "conflict"


def test_summary_counts_no_verified_ladder(tmp_path):
    ms = [{"match_id": f"apif-{i}", "fixture_id": i, "kickoff_utc": "2026-11-05T10:00:00Z", "league": "L",
           "league_id": "l", "home": "A", "away": "B", "list": "watchlist"} for i in (1, 2)]
    base = {"captured_at": "2026-11-04T10:00:00Z", "kickoff_utc": "2026-11-05T10:00:00Z", "odds_format": "decimal"}
    rows = [{**base, **row("apif-1", "report", "oddspapi", "pinnacle", -0.5)},
            {**base, **row("apif-1", "report", "odds_api", "pinnacle", -0.5)},
            {**base, **row("apif-2", "report", "odds_api", "pinnacle", 1.0, oh=2.10, oa=1.84)},
            {**base, **row("apif-2", "report", "oddspapi", "pinnacle", 1.25, oh=1.833, oa=2.06)},
            {**base, **row("apif-2", "report", "oddspapi", "pinnacle", 1.0, "0", oh=2.09, oa=1.85)}]
    snap.write_rows(tmp_path / "s" / "2026-11-05.csv", rows)
    d = snap.write_summary("2026-11-05", ms, [], {}, ["report"], {}, snap_dir=tmp_path / "s", now=NOW)
    assert d["counts"]["report"] == {"verified": 1, "conflict": 1}
    assert "verified_ladder" not in snap.STATUSES


def test_slug_and_screened():
    assert snap.slug("SBO") == "sbobet" and snap.slug("BetOnline.ag") == "betonline" and snap.slug("Pinnacle") == "pinnacle"
    rep = {"picks": [{"match_id": "apif-1", "kickoff_utc": "2026-11-04T20:00:00Z", "league": "UEFA Champions League（欧冠）",
                      "home": "A", "away": "B"}],
           "watched": [{"home": "X", "away": "Y"}],
           "watchlist": [{"match_id": "apif-1", "kickoff_utc": "2026-11-04T20:00:00Z", "league": "", "home": "A", "away": "B"},
                         {"match_id": "apif-2", "kickoff_utc": "2026-11-05T10:00:00Z",
                          "league": "AFC Champions League Two（亚冠联2）", "home": "C", "away": "D"}]}
    ms, skipped = snap.screened(rep, LEAGUES)
    assert [m["match_id"] for m in ms] == ["apif-1", "apif-2"] and ms[0]["fixture_id"] == 1
    assert ms[0]["league_id"] == "ucl" and ms[1]["league_id"] == "acl_two" and len(skipped) == 1


# ---------- 端到端（假客户端） ----------

class FakePapi:
    enabled = True

    def __init__(self):
        self.used, self.hist_calls, self.fx_calls = 10, [], 0
        self.backoffs = []

    def remaining(self):
        return 250 - self.used

    def usage(self):
        return {"used": self.used, "limit": 250, "month": "2026-11"}

    def fixtures(self, tid, start, end):
        self.used += 1
        self.fx_calls += 1
        return [{"fixtureId": "idP1", "startTime": "2026-11-04T20:00:00.000Z", "participant1Name": "FC Porto",
                 "participant2Name": "Napoli"}]

    def historical_odds(self, fid, books, ttl=None):
        self.hist_calls.append((fid, tuple(books)))
        return {"fixtureId": fid, "bookmakers": {b: hist_book() for b in books if b in ("pinnacle", "sbobet")}}


class FakeOddsAPI:
    enabled = True

    def __init__(self):
        self.u = {"used": 5, "remaining": 495}
        self.calls = 0

    def usage(self):
        return dict(self.u)

    def odds(self, sport, markets="spreads", regions=None):
        assert regions == "eu" and markets == "spreads"
        self.calls += 1
        self.u = {"used": self.u["used"] + 1, "remaining": self.u["remaining"] - 1}
        return json.loads((Path(__file__).parent / "data" / "odds_api_spreads.json").read_text())


class FakeApif:
    enabled = True

    def __init__(self, remaining=80):
        self.used, self._rem, self.calls = 0, remaining, []

    def remaining(self):
        return self._rem - self.used

    def usage(self):
        return {"used": self.used}

    def get(self, endpoint, params, force=False):
        self.calls.append((endpoint, params, force))
        self.used += 1
        return {"response": [{"update": "2026-11-04T18:50:00+00:00", "bookmakers": [
            {"name": "Pinnacle", "bets": [{"name": "Asian Handicap", "values": [
                {"value": "Home -0.5", "odd": "1.90"}, {"value": "Away -0.5", "odd": "2.00"},
                {"value": "Home -0.25", "odd": "1.70"}, {"value": "Away -0.25", "odd": "2.25"}]}]}]}]}


def matches():
    return [{"match_id": "apif-100", "fixture_id": 100, "kickoff_utc": "2026-11-04T20:00:00Z", "league": "UCL",
             "league_id": "ucl", "home": "波尔图 FC Porto", "away": "那不勒斯 Napoli", "list": "watchlist"},
            {"match_id": "apif-200", "fixture_id": 200, "kickoff_utc": "2026-11-04T20:00:00Z", "league": "ACL2",
             "league_id": "acl_two", "home": "C", "away": "D", "list": "watchlist"}]


def mk(tmp_path, now=NOW, **kw):
    return snap.Snapshotter("2026-11-05", papi=kw.get("papi", FakePapi()), oddsapi=kw.get("oddsapi", FakeOddsAPI()),
                            apif=kw.get("apif", FakeApif()), now=lambda: now, snap_dir=tmp_path / "s",
                            ah_markets=AH, aliases={}, leagues=LEAGUES, odds_api_cache=tmp_path / "oa",
                            log=lambda m: None, apif_mode=kw.get("apif_mode", "auto"))


def read(tmp_path):
    with (tmp_path / "s" / "2026-11-05.csv").open() as fh:
        return list(csv.DictReader(fh))


def test_run_report_and_open(tmp_path):
    papi, oa, apif = FakePapi(), FakeOddsAPI(), FakeApif()
    s = mk(tmp_path, papi=papi, oddsapi=oa, apif=apif)
    res = s.run(matches(), ["report", "open"])
    rows = read(tmp_path)
    assert list(rows[0].keys()) == snap.FIELDS
    p = [r for r in rows if r["match_id"] == "apif-100"]
    rep_main = [r for r in p if r["stage"] == "report" and r["is_main"] == "1"]
    by = {(r["source"], r["bookmaker"]): r for r in rep_main}
    assert by[("oddspapi", "pinnacle")]["ah_home_line"] == "-0.5" and by[("oddspapi", "pinnacle")]["home_hk"] == "0.92" and by[("oddspapi", "pinnacle")]["odds_format"] == "decimal"
    # odds_api 只有单线 -0.25，与 oddspapi 主线 -0.5 不同 → 不标 is_main，只核价
    oa_pin = [r for r in p if r["stage"] == "report" and r["source"] == "odds_api" and r["bookmaker"] == "pinnacle"]
    assert oa_pin and oa_pin[0]["ah_home_line"] == "-0.25" and oa_pin[0]["is_main"] == "0"
    assert res["verification"][("apif-100", "report")]["verification"] in ("conflict", "verified", "single")
    op_ = [r for r in p if r["stage"] == "open" and r["is_main"] == "1"]
    assert {r["ah_home_line"] for r in op_} == {"-0.25"} and all(r["source"] == "oddspapi" for r in op_)
    assert res["verification"][("apif-100", "open")]["verification"] == "single"
    # ACL2 不在 The Odds API → auto 模式下 API-Football 补第二来源，未映射到 OddsPapi → 只有 api_football
    acl = [r for r in rows if r["match_id"] == "apif-200"]
    assert {r["source"] for r in acl if r["stage"] == "report"} == {"api_football"}
    miss = [r for r in acl if r["stage"] == "open"]
    assert len(miss) == 1 and miss[0]["verification"] == "missing" and miss[0]["ah_home_line"] == ""
    assert "oddspapi:no_fixture_match" in miss[0]["status_detail"]
    assert apif.calls == [("odds", {"fixture": 200}, False)]   # UCL 已有 Odds API，不再耗 API-Football
    assert oa.calls == 1 and papi.fx_calls == 2                 # 每联赛 1 次 /fixtures，每 sport 1 credit
    assert res["calls"]["odds_api_credits"] == 1 and res["calls"]["api_football"] == 1
    # 映射持久化：再跑不再调用 /fixtures；Odds API 走 10 分钟缓存
    s2 = mk(tmp_path, papi=papi, oddsapi=oa, apif=apif)
    s2.run(matches(), ["report"])
    assert papi.fx_calls == 2 and oa.calls == 1
    assert json.loads((tmp_path / "s" / "2026-11-05.map.json").read_text())["apif-100"]["oddspapi"] == "idP1"


def test_close_before_and_after_kickoff(tmp_path):
    before = datetime(2026, 11, 4, 19, 56, tzinfo=UTC)
    apif = FakeApif()
    mk(tmp_path, now=before, apif=apif).run(matches()[:1], ["close"])
    rows = [r for r in read(tmp_path) if r["stage"] == "close" and r["is_main"] == "1"]
    assert {r["source"] for r in rows} == {"oddspapi", "odds_api"}
    pin = next(r for r in rows if r["source"] == "oddspapi" and r["bookmaker"] == "pinnacle")
    assert pin["odds_home"] == "1.88" and pin["price_ts"] == "2026-11-04T19:55:00Z"
    # 开赛后补跑：只更新 OddsPapi，Odds API 的开赛前行保留；开赛后的价格不进入收盘
    after = datetime(2026, 11, 4, 20, 30, tzinfo=UTC)
    oa = FakeOddsAPI()
    mk(tmp_path, now=after, oddsapi=oa).run(matches()[:1], ["close"], backfill=True)
    rows = [r for r in read(tmp_path) if r["stage"] == "close" and r["is_main"] == "1"]
    assert oa.calls == 0 and {r["source"] for r in rows} == {"oddspapi", "odds_api"}
    pin = next(r for r in rows if r["source"] == "oddspapi" and r["bookmaker"] == "pinnacle")
    assert pin["odds_home"] == "1.88" and pin["as_of"] == "2026-11-04T19:59:59Z"


def test_select_close_window():
    ms = matches() + [{**matches()[0], "match_id": "apif-300", "kickoff_utc": "2026-11-04T22:00:00Z"}]
    now = datetime(2026, 11, 4, 19, 52, tzinfo=UTC)
    assert [m["match_id"] for m in snap.select(ms, ["close"], now, 10, False)] == ["apif-100", "apif-200"]
    done = [{"match_id": "apif-100", "stage": "close", "source": "odds_api", "captured_at": "2026-11-04T19:51:00Z"}]
    assert [m["match_id"] for m in snap.select(ms, ["close"], now, 10, False, existing=done)] == ["apif-200"]
    later = datetime(2026, 11, 4, 21, 0, tzinfo=UTC)
    assert [m["match_id"] for m in snap.select(ms, ["close"], later, 10, True)] == ["apif-100", "apif-200"]
    assert len(snap.select(ms, ["report"], now, 10, False)) == 3


def test_apif_floor_protects_quota(tmp_path):
    apif = FakeApif(remaining=30)
    res = mk(tmp_path, apif=apif).run(matches()[1:], ["report"])
    assert apif.calls == [] and "api_football:floor" in res["reasons"]["apif-200|report"][-1]


def test_summary_counts(tmp_path):
    s = mk(tmp_path)
    res = s.run(matches(), ["report", "open"])
    d = snap.write_summary("2026-11-05", matches(), [], res, ["report", "open"], {"oddspapi": {"remaining": 238}},
                           snap_dir=tmp_path / "s", now=NOW)
    assert d["counts"]["close"] == {"pending": 2}
    assert sum(d["counts"]["open"].values()) == 2 and d["counts"]["open"].get("missing") == 1
    assert d["matches"][0]["kickoff_bjt"] == "2026-11-05 04:00"


# ---------- OddsPapi 429 退避 ----------

class R:
    def __init__(self, data, status=200, headers=None):
        self.d, self.status_code, self.headers = data, status, headers or {}

    def json(self):
        return self.d


class S:
    def __init__(self, seq):
        self.seq, self.calls = list(seq), 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return self.seq.pop(0)


def test_oddspapi_429_exponential_backoff(tmp_path):
    slept = []
    sess = S([R({}, 429), R({}, 429, {"Retry-After": "7"}), R({"fixtureId": "x", "bookmakers": {}})])
    c = op.Client(key="KEY", session=sess, cache_dir=tmp_path / "c", usage_file=tmp_path / "u.json",
                  sleep=slept.append, now=lambda: NOW, min_interval=0)
    assert c.historical_odds("x", ("pinnacle",))["fixtureId"] == "x"
    backoff = [x for x in slept if x >= 2]
    assert backoff == [2.0, 7.0] and sess.calls == 3 and c.usage()["used"] == 0   # 免费端点、429 都不计数


def test_oddspapi_429_gives_up_and_billable_5xx_not_retried(tmp_path):
    import pytest
    sess = S([R({}, 429)] * 4)
    c = op.Client(key="KEY", session=sess, cache_dir=tmp_path / "c", usage_file=tmp_path / "u.json",
                  sleep=lambda s: None, now=lambda: NOW)
    with pytest.raises(op.OddsPapiError):
        c.historical_odds("x")
    assert sess.calls == 4 and len(c.backoffs) == 3
    sess = S([R({}, 503)])
    c = op.Client(key="KEY", session=sess, cache_dir=tmp_path / "c2", usage_file=tmp_path / "u2.json",
                  sleep=lambda s: None, now=lambda: NOW)
    with pytest.raises(op.OddsPapiError):
        c.odds("x")
    assert sess.calls == 1 and c.usage()["used"] == 1


# ---------- 请求间隔可配置 ----------

def test_min_interval_from_sources_yaml(tmp_path):
    y = tmp_path / "sources.yaml"
    y.write_text("odds_snapshots:\n  sources:\n    oddspapi:\n      min_interval_seconds: {historical-odds: 6, default: 1.5}\n")
    iv = op.load_min_intervals(y)
    assert iv["historical-odds"] == 6.0 and iv["default"] == 1.5 and iv["fixtures"] == op.MIN_INTERVAL_S["fixtures"]
    y.write_text("odds_snapshots:\n  oddspapi:\n    min_interval_seconds: 3\n")
    assert set(op.load_min_intervals(y).values()) == {3.0}
    y.write_text(": : not yaml [")
    assert op.load_min_intervals(y) == op.MIN_INTERVAL_S
    assert op.load_min_intervals(tmp_path / "missing.yaml") == op.MIN_INTERVAL_S
    # 仓库里的配置：historical-odds 不低于官方 5s
    assert op.load_min_intervals()["historical-odds"] >= 5.0


def test_client_waits_per_endpoint_interval(tmp_path):
    t = [100.0]
    slept = []

    def sleep(x):
        slept.append(round(x, 3))
        t[0] += x

    ok = {"fixtureId": "x", "bookmakers": {}}
    sess = S([R(ok), R(ok), R([]), R(ok)])
    c = op.Client(key="KEY", session=sess, cache_dir=tmp_path / "c", usage_file=tmp_path / "u.json", sleep=sleep,
                  now=lambda: NOW, clock=lambda: t[0], min_interval={"historical-odds": 5.5, "fixtures": 2.2})
    c.historical_odds("a", ttl=0)
    t[0] += 1.0
    c.historical_odds("b", ttl=0)                    # 距上次 1.0s → 再等 4.5s
    c.get("fixtures", {"tournamentId": 1})            # 另一个端点，各自计时，不用等
    c.historical_odds("c", ttl=0)                    # 距上次 historical 0s → 等 5.5s
    assert slept == [4.5, 5.5] and sess.calls == 4


def test_429_honours_retry_ms_body(tmp_path):
    slept = []
    sess = S([R({"error": {"code": "RATE_LIMITED", "retryMs": 9000}}, 429), R({"fixtureId": "x", "bookmakers": {}})])
    c = op.Client(key="KEY", session=sess, cache_dir=tmp_path / "c", usage_file=tmp_path / "u.json",
                  sleep=slept.append, now=lambda: NOW, min_interval=0)
    c.historical_odds("x")
    assert slept == [9.0]


# ---------- 新路径 data/snapshots/odds/ + 旧路径兼容 ----------

def test_paths_default_new_dir_and_legacy_fallback(tmp_path, monkeypatch):
    assert snap.SNAP_DIR.parts[-2:] == ("snapshots", "odds") and snap.LEGACY_SNAP_DIR.name == "odds_snapshots"
    new, old = tmp_path / "snapshots" / "odds", tmp_path / "odds_snapshots"
    monkeypatch.setattr(snap, "SNAP_DIR", new)
    monkeypatch.setattr(snap, "LEGACY_SNAP_DIR", old)
    assert snap.read_path("2026-11-05", "csv") == new / "2026-11-05.csv"     # 都没有 → 新路径
    old.mkdir(parents=True)
    (old / "2026-11-05.csv").write_text("x")
    assert snap.read_path("2026-11-05", "csv") == old / "2026-11-05.csv"     # 只有旧的 → 读旧的
    new.mkdir(parents=True)
    (new / "2026-11-05.csv").write_text("y")
    assert snap.read_path("2026-11-05", "csv") == new / "2026-11-05.csv"     # 新的有了 → 新的优先
    assert snap.paths("2026-11-05")["map"] == new / "2026-11-05.map.json"


def test_close_rows_carried_from_legacy_and_not_refetched(tmp_path):
    legacy = tmp_path / "legacy"
    # 旧路径里已有 report + close（循环之前写的）
    s0 = mk(tmp_path, now=datetime(2026, 11, 4, 19, 55, tzinfo=UTC))
    s0.p = snap.paths("2026-11-05", legacy)
    s0.run(matches()[:1], ["report", "close"])
    old_rows = snap.load_rows(legacy / "2026-11-05.csv")
    (legacy / "2026-11-05.map.json").write_text((tmp_path / "s" / "2026-11-05.map.json").read_text()
                                                if (tmp_path / "s" / "2026-11-05.map.json").exists() else "{}")
    for f in (tmp_path / "s").glob("*"):
        f.unlink()
    closes = [r for r in old_rows if r["stage"] == "close"]
    assert closes
    # select 读旧路径的 close 行 → 已抓过，不再抓
    existing = snap.load_rows(snap.read_path("2026-11-05", "csv", tmp_path / "s", legacy))
    assert snap.select(matches()[:1], ["close"], datetime(2026, 11, 4, 19, 56, tzinfo=UTC), 10, False,
                       existing=existing) == []
    # 新路径写入时把旧文件的记录（含 close）带过来
    papi = FakePapi()
    s1 = snap.Snapshotter("2026-11-05", papi=papi, oddsapi=FakeOddsAPI(), apif=FakeApif(),
                          now=lambda: datetime(2026, 11, 4, 20, 30, tzinfo=UTC), snap_dir=tmp_path / "s",
                          legacy_dir=legacy, ah_markets=AH, aliases={}, leagues=LEAGUES,
                          odds_api_cache=tmp_path / "oa", log=lambda m: None)
    assert s1.map == json.loads((legacy / "2026-11-05.map.json").read_text())
    s1.run(matches()[1:2], ["report"])
    new_rows = snap.load_rows(tmp_path / "s" / "2026-11-05.csv")
    key = lambda r: (r["match_id"], r["stage"], r["source"], r["bookmaker"], r["ah_home_line"], r["captured_at"])  # noqa: E731
    assert {key(r) for r in old_rows} <= {key(r) for r in new_rows}
    assert any(r["match_id"] == "apif-200" for r in new_rows)
    assert not (legacy / "2026-11-05.csv").read_text() == ""   # 旧文件不动


def test_main_line_reads_new_path_and_legacy_fallback(tmp_path, monkeypatch):
    new, old = tmp_path / "snapshots" / "odds", tmp_path / "odds_snapshots"
    monkeypatch.setattr(snap, "SNAP_DIR", new)
    monkeypatch.setattr(snap, "LEGACY_SNAP_DIR", old)
    base = {"match_id": "apif-1", "stage": "report", "bookmaker": "pinnacle", "source": "oddspapi",
            "is_main": "1", "ah_home_line": "-0.5", "odds_home": "1.90", "odds_away": "1.95",
            "odds_format": "decimal", "captured_at": "2026-11-04T10:00:00Z", "verification": "single",
            "kickoff_utc": "2026-11-05T10:00:00Z"}
    old.mkdir(parents=True)
    snap.write_rows(old / "2026-11-05.csv", [dict(base)])
    ml = snap.main_line("2026-11-05", "apif-1")
    assert ml["ah_home_line"] == -0.5 and ml["source"] == "oddspapi"
    new.mkdir(parents=True)
    snap.write_rows(new / "2026-11-05.csv", [{**base, "ah_home_line": "-0.25", "odds_home": "1.88", "odds_away": "1.98"}])
    assert snap.main_line("2026-11-05", "apif-1")["ah_home_line"] == -0.25
    assert snap.main_line("2026-11-05", "missing") is None


def test_consensus_line_mode_tie_breaks():
    # 众数并列：离 Pinnacle 更近
    assert snap._consensus_line([-0.5, -0.25, -0.5, -0.25], pinnacle_main=-0.25) == -0.25
    assert snap._consensus_line([-0.5, -0.25, -0.5, -0.25], pinnacle_main=-0.5) == -0.5
    # 无 Pinnacle：取绝对值较小
    assert snap._consensus_line([1.0, -0.5, 1.0, -0.5]) == -0.5


def test_overround_and_high_flag():
    rows = [row("a", "report", "oddspapi", "pinnacle", -0.25, oh=1.884, oa=1.813)]
    snap.assign_status(rows)
    assert abs(float(rows[0]["overround"]) - round(1/1.884 + 1/1.813 - 1, 4)) < 1e-9
    assert "high_overround" in rows[0]["status_detail"]


def test_default_sources_include_infersports_and_papi_ps3838():
    assert "infersports" in snap.SOURCES
    assert any("ps3838" in g and "198bet" in g for g in snap.PAPI_BOOK_GROUPS)


def _xv_row(mid, stage, book, source, line, oh=1.9, oa=1.9, verified="single", ts="2026-11-05T09:00:00Z", cap="2026-11-05T09:05:00Z"):
    return {"match_id": mid, "stage": stage, "bookmaker": book, "source": source,
            "is_main": "1", "ah_home_line": str(line), "odds_home": str(oh), "odds_away": str(oa),
            "verification": verified, "price_ts": ts, "captured_at": cap}


def test_cross_verification_pass_two_sources_three_books():
    rows = [
        _xv_row("m", "report", "pinnacle", "oddspapi", -0.5, verified="single"),
        _xv_row("m", "report", "sbobet", "oddspapi", -0.5, oh=1.95, verified="verified"),
        _xv_row("m", "report", "sbobet", "infersports", -0.5, oh=1.95, verified="verified"),
        _xv_row("m", "report", "singbet", "oddspapi", -0.5),
    ]
    v = snap.evaluate_cross_verification(rows, "m", "report", snap.load_cross_verification())
    assert v["passed"], v
    assert len(v["sources"]) == 2 and len(v["bookmakers_on_main"]) == 3 and v["verified_books"] == ["sbobet"]


def test_cross_verification_fail_single_source():
    rows = [_xv_row("m", "report", "pinnacle", "oddspapi", -0.5, verified="verified")]
    v = snap.evaluate_cross_verification(rows, "m", "report", snap.load_cross_verification())
    assert not v["passed"] and any(r.startswith("sources:") for r in v["reasons"])


def test_cross_verification_stale_downgrade_and_withdraw():
    rows = [
        _xv_row("m", "close", "pinnacle", "oddspapi", -0.5, verified="single", ts="2026-11-05T08:00:00Z"),
        _xv_row("m", "close", "sbobet", "oddspapi", -0.5, verified="verified"),
        _xv_row("m", "close", "sbobet", "infersports", -0.5, verified="verified"),
        _xv_row("m", "close", "singbet", "oddspapi", -0.5),
    ]
    v = snap.evaluate_cross_verification(rows, "m", "close", snap.load_cross_verification())
    assert v["freshness"] == "stale_withdraw" and not v["passed"]


def test_deep_line_blocks_verdict():
    rows = [
        _xv_row("m", "report", "pinnacle", "oddspapi", -2.0, verified="single"),
        _xv_row("m", "report", "sbobet", "oddspapi", -2.0, verified="verified"),
        _xv_row("m", "report", "sbobet", "infersports", -2.0, verified="verified"),
        _xv_row("m", "report", "singbet", "oddspapi", -2.0),
    ]
    from datetime import datetime, timezone
    xv = snap.cross_verification_for_rows(rows, "m", now=datetime(2026, 11, 5, 9, 6, tzinfo=timezone.utc))
    assert xv["phases"]["report"]["deep_line"] is True
    assert xv["advisory_only"] is True
    assert xv["verdict"] == "ok"  # 只打标不自动拦截


def test_xv_excludes_suspended_api_and_pending_empty():
    rows = [_xv_row("m", "report", "pinnacle", "api_football", -0.5, verified="verified")]
    xv = snap.cross_verification_for_rows(rows, "m")
    assert xv["phases"]["report"]["status"] == "pending"
    assert xv["phases"]["close"]["status"] == "pending"
    assert xv["phases"]["open"]["status"] == "pending"
    assert xv["verdict"] == "pending"


def test_xv_old_report_does_not_veto_fresh_close():
    # 旧 report 单源+过时；新 close 双源三家 verified → 看 close，不看旧 report
    old = [
        _xv_row("m", "report", "pinnacle", "oddspapi", -0.5, verified="single",
                ts="2026-11-05T01:00:00Z", cap="2026-11-05T01:05:00Z"),
    ]
    close = [
        _xv_row("m", "close", "pinnacle", "oddspapi", -0.5, verified="single",
                ts="2026-11-05T09:59:00Z", cap="2026-11-05T10:00:00Z"),
        _xv_row("m", "close", "sbobet", "oddspapi", -0.5, verified="verified",
                ts="2026-11-05T09:59:00Z", cap="2026-11-05T10:00:00Z"),
        _xv_row("m", "close", "sbobet", "infersports", -0.5, verified="verified",
                ts="2026-11-05T09:59:00Z", cap="2026-11-05T10:00:00Z"),
        _xv_row("m", "close", "singbet", "oddspapi", -0.5,
                ts="2026-11-05T09:59:00Z", cap="2026-11-05T10:00:00Z"),
    ]
    from datetime import datetime, timezone
    now = datetime(2026, 11, 5, 10, 2, tzinfo=timezone.utc)
    xv = snap.cross_verification_for_rows(old + close, "m", now=now)
    assert xv["active_stage"] == "close"
    assert xv["verdict"] == "ok"
    assert xv["phases"]["report"]["passed"] is False  # 历史 report 仍如实打标


def test_xv_deep_line_is_advisory_not_auto_withhold():
    rows = [
        _xv_row("m", "report", "pinnacle", "oddspapi", -2.0, verified="single"),
        _xv_row("m", "report", "sbobet", "oddspapi", -2.0, verified="verified"),
        _xv_row("m", "report", "sbobet", "infersports", -2.0, verified="verified"),
        _xv_row("m", "report", "singbet", "oddspapi", -2.0),
    ]
    from datetime import datetime, timezone
    xv = snap.cross_verification_for_rows(rows, "m", now=datetime(2026, 11, 5, 9, 6, tzinfo=timezone.utc))
    assert xv["phases"]["report"]["deep_line"] is True
    assert "deep_line" in xv["phases"]["report"]["reasons"]
    assert xv["verdict"] == "ok"  # 只打标；深盘拦截在先知签发侧


def test_xv_stale_uses_pin_and_verified_not_oldest_book():
    rows = [
        _xv_row("m", "close", "pinnacle", "oddspapi", -0.5, verified="single",
                ts="2026-11-05T09:59:30Z", cap="2026-11-05T10:00:00Z"),
        _xv_row("m", "close", "sbobet", "oddspapi", -0.5, verified="verified",
                ts="2026-11-05T09:59:30Z", cap="2026-11-05T10:00:00Z"),
        _xv_row("m", "close", "sbobet", "infersports", -0.5, verified="verified",
                ts="2026-11-05T09:59:30Z", cap="2026-11-05T10:00:00Z"),
        # 皇冠很旧，但非 Pin 非 verified，不得一票否决
        _xv_row("m", "close", "crown", "infersports", -0.5, verified="single",
                ts="2026-11-05T06:00:00Z", cap="2026-11-05T10:00:00Z"),
        _xv_row("m", "close", "singbet", "oddspapi", -0.5, verified="single",
                ts="2026-11-05T06:00:00Z", cap="2026-11-05T10:00:00Z"),
    ]
    from datetime import datetime, timezone
    xv = snap.cross_verification_for_rows(rows, "m", now=datetime(2026, 11, 5, 10, 1, tzinfo=timezone.utc))
    assert xv["verdict"] == "ok"
    assert xv["phases"]["close"]["stale_age_s"] <= 90


def test_xv_disjoint_book_sets_flag_no_shared_book():
    rows = [
        _xv_row("m", "report", "pinnacle", "odds_api", -1.0, verified="single"),
        _xv_row("m", "report", "marathonbet", "odds_api", -1.0),
        _xv_row("m", "report", "coolbet", "odds_api", -1.0),
        _xv_row("m", "report", "sbobet", "infersports", -1.0),
        _xv_row("m", "report", "crown", "infersports", -1.0),
        _xv_row("m", "report", "macau", "infersports", -1.0),
    ]
    from datetime import datetime, timezone
    xv = snap.cross_verification_for_rows(rows, "m", now=datetime(2026, 11, 5, 9, 6, tzinfo=timezone.utc))
    assert xv["phases"]["report"]["shared_book"] is False
    assert "no_shared_book" in xv["phases"]["report"]["reasons"]
    assert xv["verdict"] == "review"
