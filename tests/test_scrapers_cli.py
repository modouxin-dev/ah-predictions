"""首发/比分入口（python -m ah.scrapers.lineups / scores）、unverified 状态、AFC 合规开关（全部离线）。"""
import copy
import json
from pathlib import Path

import jsonschema
import pytest

from ah.scrapers import afc, apif, common, jleague, lineups, scores
from ah.scrapers.crosscheck import UNVERIFIED, crosscheck_lineup, single_source_lineup
from ah.scrapers.http import PoliteFetcher

D = Path(__file__).parent / "data" / "scrapers"
ACLE = (D / "afc_acle_landing_2026-11-04.html").read_text(encoding="utf-8")
POST = (D / "jleague_acle_2026_110401_postgame.html").read_text(encoding="utf-8")
URL_POST = "https://www.jleague.jp/en/match/acle/2026/110401/"
API_XI = json.loads((D / "apif_lineups_1629973.json").read_text())
AFC_ROBOTS = "https://www.the-afc.com/robots.txt"
JL_ROBOTS = "https://www.jleague.jp/robots.txt"


def fx(fid, league, home, away, hg=None, ag=None, status="NS", date="2026-11-04T10:00:00+00:00"):
    return {"fixture": {"id": fid, "date": date, "status": {"short": status}},
            "league": {"id": league, "name": f"L{league}", "round": "Group Stage - 4"},
            "teams": {"home": {"name": home}, "away": {"name": away}},
            "goals": {"home": hg, "away": ag},
            "score": {"fulltime": {"home": hg, "away": ag}, "extratime": {"home": None, "away": None}}}


class Resp:
    def __init__(self, status, text, headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}


class Sess:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected request {url}")
        return self.routes[url]


def fetcher(tmp_path, site, base, routes, **kw):
    PoliteFetcher._last_call.clear()
    return PoliteFetcher(site, base, cache_dir=tmp_path / site, session=Sess(routes), min_interval_s=0,
                         sleep=lambda s: None, **kw)


class FakeApi:
    """模拟 apif.Api：fixtures / lineups 来自内存；calls 计数模拟额度消耗。"""
    def __init__(self, fixtures=(), xi=None, cached_xi=False):
        self.fx = {f["fixture"]["id"]: f for f in fixtures}
        self.xi, self.cached_xi, self.calls = xi or {}, cached_xi, 0

    def fixture(self, fid, need_final=False):
        f = self.fx.get(fid)
        return (f, "cache") if f else (None, "none")

    def fixtures_by_date(self, date):
        return [f for f in self.fx.values() if f["fixture"]["date"].startswith(date)], "cache"

    def lineups(self, fid):
        if self.cached_xi:
            return self.xi.get(fid), "cache"
        self.calls += 1
        return self.xi.get(fid) or [], "api"


# ---------- unverified 状态 ----------
def test_compare_team_unverified_when_site_has_no_xi():
    rec = jleague.map_fixture(jleague.parse_match(POST, URL_POST),
                              json.loads((D / "apif_fixtures_2026-11-04.json").read_text()))
    pre = copy.deepcopy(rec)
    for s in ("home", "away"):
        pre["lineups"][s]["starting_xi"] = None
    res = crosscheck_lineup(pre, API_XI)
    assert res["verdict"] == UNVERIFIED and res["teams"]["home"]["verdict"] == UNVERIFIED
    assert "单一来源" in res["reason"]
    common.validate_crosscheck(res)
    # 官网有一队、另一队没公布 -> 整场仍是 unverified（不会被当成已验证）
    pre["lineups"]["home"]["starting_xi"] = rec["lineups"]["home"]["starting_xi"]
    r2 = crosscheck_lineup(pre, API_XI)
    assert r2["teams"]["home"]["verdict"] == "已验证" and r2["verdict"] == UNVERIFIED
    # 冲突优先于 unverified；两边都没有 -> 暂无
    bad = copy.deepcopy(API_XI)
    bad[0]["startXI"][0]["player"]["number"] = 99
    assert crosscheck_lineup(pre, bad)["verdict"] == "冲突"
    assert crosscheck_lineup(pre, [])["verdict"] == "暂无"


def test_single_source_lineup_and_schema():
    f = fx(42, 188, "Pohang Steelers", "Gamba Osaka")
    r = single_source_lineup(f, API_XI, "A-Leagues 无官网来源")
    assert r["verdict"] == UNVERIFIED and r["sources"]["official"] is None
    assert r["teams"]["home"]["api_count"] == 11 and r["kickoff_bjt"] == "2026-11-04T18:00:00+08:00"
    assert r["substitutes"] == r["absences"] == "暂无"
    common.validate_crosscheck(r)
    half = single_source_lineup(f, API_XI[:1], "x")
    assert half["verdict"] == "暂无" and half["teams"]["away"]["verdict"] == "暂无"
    assert single_source_lineup(f, [], "x")["verdict"] == "暂无"
    with pytest.raises(jsonschema.ValidationError):
        common.validate_crosscheck(dict(r, verdict="verified"))
    with pytest.raises(jsonschema.ValidationError):
        common.validate_crosscheck(dict(r, kickoff_bjt="2026-11-04T10:00:00Z"))


def test_acl_match_ids_config():
    ids = common.acl_match_ids()
    assert ids[1629974] == "acle/2026/110402" and ids[1629973] == "acle/2026/110401"
    assert common.acl_match_ids({"acl_match_ids": {"7": "/acl2/2026/1/"}}) == {7: "acl2/2026/1"}


# ---------- AFC 合规：开关 / 不落盘 / 页数 ----------
def test_afc_sources_yaml_compliance():
    cfg = afc.load_site_config("afc")
    assert cfg["tos_note"] == "internal-crosscheck-only" and cfg["cache_raw"] is False
    assert cfg["min_interval_s"] >= 15 and cfg["scores"]["max_pages"] <= 2 and afc.MAX_PAGES == 2
    assert PoliteFetcher.from_config("afc").cache_raw is False


def test_afc_kill_switch_env_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv(afc.KILL_ENV, "1")
    f = fetcher(tmp_path, "afc", afc.BASE, {})
    got = afc.fetch_scores(fetcher=f)
    assert got["enabled"] is False and got["claims"] == [] and f.session.calls == []
    assert all("已停用" in p["error"] for p in got["pages"].values())


def test_afc_kill_switch_config(tmp_path, monkeypatch):
    monkeypatch.delenv(afc.KILL_ENV, raising=False)
    monkeypatch.setattr(afc, "load_site_config", lambda site: {"enabled": True, "scores": {"enabled": False}})
    f = fetcher(tmp_path, "afc", afc.BASE, {})
    got = afc.fetch_scores(fetcher=f)
    assert got["enabled"] is False and "scores.enabled=false" in got["disabled_reason"] and f.session.calls == []
    assert afc.scores_enabled({"enabled": False})[0] is False
    assert afc.scores_enabled({"enabled": True, "scores": {"enabled": True}}) == (True, "")


def test_afc_html_not_written_and_claims_cache(tmp_path, monkeypatch):
    monkeypatch.delenv(afc.KILL_ENV, raising=False)
    routes = {AFC_ROBOTS: Resp(200, "Sitemap: https://www.the-afc.com/sitemap.xml\n"),
              afc.COMP_PAGES["acle"]: Resp(200, ACLE), afc.COMP_PAGES["acl2"]: Resp(200, ACLE.replace("aclelite", "x"))}
    f = fetcher(tmp_path, "afc", afc.BASE, routes, cache_raw=False)
    got = afc.fetch_scores(["acle", "acl2", "acle"], fetcher=f)
    assert f.session.calls.count(afc.COMP_PAGES["acle"]) == 1 and len(got["pages"]) == 2
    bodies = [p for p in (tmp_path / "afc").glob("*.body")]
    assert [json.loads(p.with_suffix("").with_suffix(".meta.json").read_text())["url"] for p in bodies] == [AFC_ROBOTS]
    claims_file = tmp_path / "afc" / "claims-acle.json"
    saved = json.loads(claims_file.read_text())
    assert saved["claims"] and all(set(c) >= {"home", "away", "home_goals", "away_goals"} and "text" not in c
                                   for c in saved["claims"])
    assert "Synthetic" not in claims_file.read_text() and "<h4" not in claims_file.read_text()
    n = len(f.session.calls)
    again = afc.fetch_scores(["acle"], fetcher=f, ttl_s=3600)
    assert len(f.session.calls) == n and again["pages"]["acle"]["from_cache"] is True
    assert len(again["claims"]) == len(saved["claims"])


def test_fetcher_cache_raw_false_removes_old_body(tmp_path):
    url = afc.COMP_PAGES["acle"]
    routes = {AFC_ROBOTS: Resp(200, ""), url: Resp(200, ACLE)}
    f = fetcher(tmp_path, "afc", afc.BASE, routes)               # 旧行为：正文落盘
    f.fetch(url)
    body, meta = f._paths(url)
    assert body.exists()
    f2 = fetcher(tmp_path, "afc", afc.BASE, routes, cache_raw=False)
    text, m = f2.fetch(url, ttl_s=3600)                         # 无正文缓存 -> 重新请求，并删除旧正文
    assert text == ACLE and not body.exists() and m["body_file"] is None
    assert f2.cached(url) is None


# ---------- lineups 入口 ----------
def test_lineups_run_one_unverified_single_source(tmp_path):
    api = FakeApi([fx(9001, 188, "Pohang Steelers", "Gamba Osaka")], {9001: API_XI})
    res, rc, line = lineups.run_one(9001, api, out_dir=tmp_path)
    assert rc == 0 and res["verdict"] == UNVERIFIED and "A-Leagues" in res["official_source"]
    out = tmp_path / "2026-11-04" / "lineups-9001.json"
    assert json.loads(out.read_text())["verdict"] == UNVERIFIED and str(out) in line
    assert res["api_football_origin"] == {"fixture": "cache", "lineups": "api"} and api.calls == 1


def test_lineups_run_one_spl_and_missing(tmp_path):
    api = FakeApi([fx(9002, 307, "Al Hilal", "Al Nassr")], {})
    res, rc, _ = lineups.run_one(9002, api, out_dir=tmp_path)
    assert rc == 0 and res["verdict"] == "暂无" and "沙特联" in res["official_source"]
    res, rc, line = lineups.run_one(12345, FakeApi(), out_dir=tmp_path)
    assert res is None and rc == 2 and "找不到" in line


def test_lineups_run_one_jleague_verified_and_conflict(tmp_path):
    f = fx(1629973, 17, "Pohang Steelers", "Gamba Osaka")
    routes = {JL_ROBOTS: Resp(200, (D / "jleague_robots.txt").read_text()), URL_POST: Resp(200, POST)}
    jf = fetcher(tmp_path, "jleague", "https://www.jleague.jp", routes)
    res, rc, _ = lineups.run_one(1629973, FakeApi([f], {1629973: API_XI}, cached_xi=True),
                                 out_dir=tmp_path / "out", fetcher=jf)
    assert rc == 0 and res["verdict"] == "已验证" and res["sources"]["jleague"] == URL_POST
    assert (tmp_path / "out" / "2026-11-04" / "jleague-acle-2026-110401.json").exists()
    bad = copy.deepcopy(API_XI)
    bad[1]["startXI"][0]["player"]["number"] = 99
    res, rc, _ = lineups.run_one(1629973, FakeApi([f], {1629973: bad}, cached_xi=True), "acle/2026/110401",
                                 out_dir=tmp_path / "out", fetcher=jf)
    assert rc == 1 and res["verdict"] == "冲突"


def test_lineups_run_one_official_failure_rc3(tmp_path):
    f = fx(1629973, 17, "Pohang Steelers", "Gamba Osaka")
    routes = {JL_ROBOTS: Resp(200, ""), URL_POST: Resp(403, "denied")}
    jf = fetcher(tmp_path, "jleague", "https://www.jleague.jp", routes)
    res, rc, _ = lineups.run_one(1629973, FakeApi([f], {1629973: API_XI}, cached_xi=True),
                                 out_dir=tmp_path / "out", fetcher=jf)
    assert rc == 3 and res["verdict"] == UNVERIFIED and "抓取失败" in res["official_source"]


def test_lineups_main_bad_args():
    assert lineups.main([]) == 2
    assert lineups.main(["--match-id", "1", "--match-id", "2", "--jleague-id", "acle/2026/1"]) == 2


# ---------- apif.Api：缓存优先 + 计数 ----------
class FakeClient:
    def __init__(self, tmp):
        self.cache_dir, self.used, self.requests = Path(tmp), 0, []

    def _cache_path(self, endpoint, params):
        return self.cache_dir / endpoint / (json.dumps(params, sort_keys=True).replace('"', "") + ".json")

    def usage(self):
        return {"used": self.used}

    def get(self, endpoint, params, **kw):
        self.requests.append((endpoint, params))
        self.used += 1
        return {"response": [{"team": {"name": "x"}, "startXI": []}]}


def test_apif_cache_first_and_counts(tmp_path):
    c = FakeClient(tmp_path)
    p = c._cache_path("fixtures/lineups", {"fixture": 5})
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"data": {"response": API_XI}}))
    (tmp_path / "fixtures").mkdir(exist_ok=True)
    (tmp_path / "fixtures" / "d1.json").write_text(json.dumps({"ts": 1, "data": {"response": [fx(5, 17, "A", "B")]}}))
    (tmp_path / "fixtures" / "d2.json").write_text(
        json.dumps({"ts": 2, "data": {"response": [fx(5, 17, "A", "B", 1, 0, status="FT")]}}))
    api = apif.Api(client=c)
    assert api.lineups(5) == (API_XI, "cache") and api.fixture(5)[0]["fixture"]["status"]["short"] == "FT"
    assert c.requests == [] and api.calls == 0
    api.lineups(6)
    assert c.requests == [("fixtures/lineups", {"fixture": 6})] and api.calls == 1
    off = apif.Api(allow_api=False, client=c)
    assert off.lineups(7) == (None, "none") and off.fixture(7) == (None, "none") and len(c.requests) == 1


# ---------- scores 入口 ----------
@pytest.fixture
def scores_env(tmp_path, monkeypatch):
    import ah
    monkeypatch.setattr(ah, "DATA", tmp_path)
    monkeypatch.setattr(common, "acl_match_ids", lambda cfg=None: {})
    monkeypatch.delenv(afc.KILL_ENV, raising=False)
    claims = afc.parse_scores((D / "afc_acl2_landing_2026-11-04.html").read_text(encoding="utf-8"),
                              afc.COMP_PAGES["acl2"], "acl2")
    calls = []

    def fake_fetch(comps, fetcher=None, **kw):
        calls.append(list(comps))
        return {"claims": claims, "enabled": True, "pages": {c: {"url": afc.COMP_PAGES[c], "ok": True} for c in comps}}
    monkeypatch.setattr(afc, "fetch_scores", fake_fetch)

    def use(api):
        monkeypatch.setattr(apif, "Api", lambda allow_api=True: api)
    return tmp_path, use, calls


def test_scores_main_match_id_verified_and_conflict(scores_env, capsys):
    root, use, calls = scores_env
    use(FakeApi([fx(1637494, 18, "Melbourne Victory", "Persib Bandung", 1, 2, "FT", "2026-11-04T07:45:00+00:00"),
                 fx(1630392, 18, "FC Seoul", "Viettel", 3, 1, "FT")]))
    assert scores.main(["--match-id", "1637494", "--no-api"]) == 0
    out = json.loads((root / "snapshots" / "scores" / "2026-11-04" / "score-1637494.json").read_text())
    assert out["results"][0]["verdict"] == "已验证" and out["tos_note"].startswith("internal-crosscheck-only")
    assert "text" not in json.dumps(out) and calls == [["acl2"]]
    assert scores.main(["--match-id", "1630392"]) == 1
    assert "API-Football 本次消耗: 0 次" in capsys.readouterr().out


def test_scores_main_not_finished_no_afc_request(scores_env):
    root, use, calls = scores_env
    use(FakeApi([fx(1629974, 17, "Buriram United", "Kashima", status="2H", date="2026-11-04T12:15:00+00:00")]))
    assert scores.main(["--match-id", "1629974", "--no-api"]) == 0
    out = json.loads((root / "snapshots" / "scores" / "2026-11-04" / "score-1629974.json").read_text())
    assert out["results"][0]["verdict"] == "暂无" and "未完场" in out["results"][0]["reason"] and calls == []


def test_scores_main_date_and_errors(scores_env, monkeypatch):
    root, use, calls = scores_env
    use(FakeApi([fx(1637494, 18, "Melbourne Victory", "Persib Bandung", 1, 2, "FT", "2026-11-04T07:45:00+00:00"),
                 fx(77, 39, "Arsenal", "Chelsea", 1, 0, "FT", "2026-11-04T20:00:00+00:00")]))
    assert scores.main(["--date", "2026-11-04", "--no-api"]) == 0
    out = json.loads((root / "snapshots" / "scores" / "2026-11-04" / "acl-score-crosscheck.json").read_text())
    assert [r["match_id"] for r in out["results"]] == [1637494]                     # 非亚冠不进日表
    assert scores.main(["--match-id", "424242"]) == 2
    assert scores.main(["--date", "2026/11/04"]) == 2
    assert scores.main(["--date", "2026-11-04", "--match-id", "1"]) == 2
    assert scores.main([]) == 2
    monkeypatch.setattr(afc, "fetch_scores", lambda comps, fetcher=None, **kw: {
        "claims": [], "enabled": True, "pages": {c: {"url": "u", "ok": False, "error": "SiteUnavailable"} for c in comps}})
    assert scores.main(["--match-id", "1637494"]) == 3


def test_scores_kill_switch_marks_reason(tmp_path, monkeypatch):
    monkeypatch.setenv(afc.KILL_ENV, "1")
    monkeypatch.setattr(common, "acl_match_ids", lambda cfg=None: {})
    f = fetcher(tmp_path, "afc", afc.BASE, {})
    res, rc = scores.run([fx(1637494, 18, "Melbourne Victory", "Persib Bandung", 1, 2, "FT")], fetcher=f)
    assert rc == 0 and f.session.calls == [] and res["afc_disabled"]
    assert res["results"][0]["verdict"] == "暂无" and "AFC 来源已停用" in res["results"][0]["reason"]
