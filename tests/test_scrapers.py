"""官网爬虫离线测试：全部使用 tests/data/scrapers/ 下保存的小样本，不联网。"""
import json
from pathlib import Path

import pytest

from ah import source_status
from ah.scrapers import (RobotsDisallowed, SiteUnavailable, SourceDisabled, StructureChanged, jleague, probes)
from ah.scrapers.crosscheck import crosscheck_lineup, names_compatible
from ah.scrapers.http import PoliteFetcher
from ah.scrapers.robots import Robots

D = Path(__file__).parent / "data" / "scrapers"
POST = (D / "jleague_acle_2026_110401_postgame.html").read_text(encoding="utf-8")
PRE = (D / "jleague_j1_2026_110706_pregame.html").read_text(encoding="utf-8")
URL_POST = "https://www.jleague.jp/en/match/acle/2026/110401/"
URL_PRE = "https://www.jleague.jp/en/match/j1/2026/110706/"


class Resp:
    def __init__(self, status=200, text="", headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}


class FakeSession:
    def __init__(self, routes):
        self.routes = routes          # url -> list[Resp]（依次返回）
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        q = self.routes[url]
        return q.pop(0) if len(q) > 1 else q[0]


class Clock:
    def __init__(self):
        self.t = 1_000_000.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def make_fetcher(tmp_path, routes, **kw):
    PoliteFetcher._last_call.clear()
    c = Clock()
    f = PoliteFetcher("jleague", "https://www.jleague.jp", cache_dir=tmp_path / "cache", session=FakeSession(routes),
                      sleep=c.sleep, now=c.now, **kw)
    return f, c


ROBOTS = {"https://www.jleague.jp/robots.txt": [Resp(200, (D / "jleague_robots.txt").read_text())]}


def status(site):
    return source_status.load().get("sources", {}).get(f"scraper_{site}", {})


# ---------- robots ----------
def test_robots_wildcards_jleague():
    r = Robots((D / "jleague_robots.txt").read_text())
    ua = "ah-predictions-scraper/0.1 (+https://github.com/x)"
    assert r.can_fetch(ua, URL_POST)
    assert r.can_fetch(ua, "https://www.jleague.jp/en/match/j1/2026/110706/")
    assert not r.can_fetch(ua, "https://www.jleague.jp/en/match/j1/2015/110706/")   # /en/match/*/201*
    assert not r.can_fetch(ua, "https://www.jleague.jp/application/x")
    assert not r.can_fetch("spider/1.0", URL_POST)                                   # User-Agent: spider 全禁


def test_robots_aleagues_query_rules():
    r = Robots((D / "aleagues_robots.txt").read_text())
    ua = "ah-predictions-scraper/0.1"
    assert r.can_fetch(ua, "https://aleagues.com.au/matches/a-league-men/")
    assert not r.can_fetch(ua, "https://aleagues.com.au/fixtures/loaders/abc")
    assert not r.can_fetch(ua, "https://aleagues.com.au/match/x?tab=lineups")
    assert not r.can_fetch(ua, "https://aleagues.com.au/x?apiIdentifier=1")


def test_robots_allow_longest_match_and_dollar():
    r = Robots("User-agent: *\nDisallow: /a/\nAllow: /a/ok$\n")
    assert r.can_fetch("x", "https://h/a/ok") and not r.can_fetch("x", "https://h/a/ok2")


# ---------- fetcher ----------
def test_fetch_respects_robots_without_request(tmp_path):
    f, _ = make_fetcher(tmp_path, ROBOTS)
    with pytest.raises(RobotsDisallowed):
        f.fetch("https://www.jleague.jp/en/match/j1/2015/110706/")
    assert [u for u, _ in f.session.calls] == ["https://www.jleague.jp/robots.txt"]


def test_fetch_rate_limit_ua_cache_and_conditional(tmp_path):
    routes = {**ROBOTS, URL_POST: [Resp(200, POST, {"ETag": '"v1"', "Last-Modified": "Wed, 04 Nov 2026 12:00:00 GMT"}),
                                   Resp(304, "")]}
    f, clock = make_fetcher(tmp_path, routes, min_interval_s=10)
    t1, m1 = f.fetch(URL_POST)
    assert t1 == POST and not m1["from_cache"] and Path(m1["body_file"]).exists()
    t2, m2 = f.fetch(URL_POST)
    assert t2 == POST and m2["not_modified"]
    url, h = f.session.calls[-1]
    assert h["If-None-Match"] == '"v1"' and h["If-Modified-Since"].startswith("Wed")
    assert "ah-predictions-scraper" in h["User-Agent"] and "Mozilla" not in h["User-Agent"]
    assert clock.sleeps and all(abs(s - 10) < 1e-6 for s in clock.sleeps)   # 同站两次请求之间等满 10 秒
    _, m3 = f.fetch(URL_POST, ttl_s=3600)                                   # TTL 内不发请求
    assert m3["from_cache"] and len(f.session.calls) == 3


def test_fetch_backoff_on_429_then_ok(tmp_path):
    routes = {**ROBOTS, URL_POST: [Resp(429, "", {"Retry-After": "7"}), Resp(503, ""), Resp(200, POST)]}
    f, clock = make_fetcher(tmp_path, routes, min_interval_s=0, backoff_base_s=2)
    text, _ = f.fetch(URL_POST)
    assert text == POST
    assert 7 in clock.sleeps and 4 in clock.sleeps        # Retry-After=7，第二次退避 2*2^1


def test_fetch_block_and_challenge_not_bypassed(tmp_path):
    f, _ = make_fetcher(tmp_path, {**ROBOTS, URL_POST: [Resp(403, "denied")]})
    with pytest.raises(SiteUnavailable):
        f.fetch(URL_POST)
    f, _ = make_fetcher(tmp_path, {**ROBOTS, URL_PRE: [Resp(200, "<html>g-recaptcha</html>")]})
    with pytest.raises(SiteUnavailable):
        f.fetch(URL_PRE)


def test_disabled_source_sends_nothing(tmp_path):
    f, _ = make_fetcher(tmp_path, {}, enabled=False, disabled_reason="条款禁止")
    with pytest.raises(SourceDisabled):
        f.fetch(URL_POST)
    assert f.session.calls == []


# ---------- J.League 解析 ----------
def test_parse_postgame_lineups():
    rec = jleague.parse_match(POST, URL_POST, fetched_at="2026-11-04T12:55:00Z")
    assert rec["site_match_id"] == "acle/2026/110401" and rec["status"] == "post-game"
    assert rec["kickoff_utc"] == "2026-11-04T10:00:00Z" and rec["kickoff_bjt"] == "2026-11-04T18:00:00+08:00"
    assert rec["home"]["name"] == "Pohang Steelers" and rec["home"]["site_name"] == "FC Pohang Steelers"
    assert rec["home"]["alias_matched"] and rec["away"]["name"] == "Gamba Osaka"
    home = rec["lineups"]["home"]["starting_xi"]
    assert len(home) == 11 and home[0] == {"name": "HWANG Injae", "number": 21, "pos": "GK", "site_player_id": None} \
        or (home[0]["number"] == 21 and home[0]["pos"] == "GK")
    assert rec["lineups"]["home"]["substitutes"] is None and rec["field_status"]["substitutes"]["status"] == "暂无"
    assert rec["lineups"]["away"]["absences"] is None and rec["field_status"]["absences"]["status"] == "暂无"
    assert rec["field_status"]["starting_xi"]["status"] == "已获取" and rec["match_id"] is None


def test_parse_pregame_no_lineup():
    rec = jleague.parse_match(PRE, URL_PRE)
    assert rec["status"] == "pre-game" and rec["lineups"]["home"]["starting_xi"] is None
    assert rec["field_status"]["starting_xi"]["status"] == "暂无" and "开赛前" in rec["field_status"]["starting_xi"]["reason"]
    assert rec["home"]["alias_matched"] is False and rec["home"]["name"] == rec["home"]["site_name"]


def test_structure_change_alerts_and_raises():
    broken = POST.replace('\\"position\\"', '\\"pos\\"')
    with pytest.raises(StructureChanged):
        jleague.parse_match(broken, URL_POST)
    s = status("jleague")
    assert "结构" in s["last_error"] and s["consecutive_failures"] >= 1
    with pytest.raises(StructureChanged):
        jleague.parse_match("<html><body>nothing</body></html>", URL_POST)
    ten = POST.replace('{\\"name\\":\\"HWANG Injae\\"', '{\\"x\\":0},{\\"name\\":\\"HWANG Injae\\"', 1)
    with pytest.raises(StructureChanged):
        jleague.parse_match(ten, URL_POST)


def test_map_fixture_and_discover():
    fx = json.loads((D / "apif_fixtures_2026-11-04.json").read_text())
    rec = jleague.map_fixture(jleague.parse_match(POST, URL_POST), fx)
    assert rec["match_id"] == 1629973
    pre = jleague.map_fixture(jleague.parse_match(PRE, URL_PRE), fx)
    assert pre["match_id"] is None
    html = '<a href="/match/acle/2026/110401/">a</a><a href="/match/acle/2026/110401/#review">b</a>' \
           '<a href="/en/match/j1/2026/110706/">c</a>'
    assert jleague.discover(html) == ["acle/2026/110401", "j1/2026/110706"]
    assert jleague.discover(html, {"j1"}) == ["j1/2026/110706"]


def test_scrape_match_end_to_end(tmp_path):
    routes = {**ROBOTS, URL_POST: [Resp(200, POST)]}
    f, _ = make_fetcher(tmp_path, routes, min_interval_s=0)
    fx = json.loads((D / "apif_fixtures_2026-11-04.json").read_text())
    rec = jleague.scrape_match("acle/2026/110401", fetcher=f, api_fixtures=fx, out_dir=tmp_path / "out")
    p = tmp_path / "out" / "2026-11-04" / "jleague-acle-2026-110401.json"
    assert p.exists() and json.loads(p.read_text())["match_id"] == 1629973 == rec["match_id"]
    assert status("jleague")["consecutive_failures"] == 0


# ---------- 核对 ----------
def test_crosscheck_verified():
    fx = json.loads((D / "apif_fixtures_2026-11-04.json").read_text())
    api = json.loads((D / "apif_lineups_1629973.json").read_text())
    rec = jleague.map_fixture(jleague.parse_match(POST, URL_POST), fx)
    res = crosscheck_lineup(rec, api)
    assert res["verdict"] == "已验证", res
    assert res["teams"]["home"]["verdict"] == res["teams"]["away"]["verdict"] == "已验证"
    assert res["sources"]["api_football"].endswith("fixture=1629973") and res["sources"]["jleague"] == URL_POST
    assert res["substitutes"] == "暂无"


def test_crosscheck_conflict_and_missing():
    fx = json.loads((D / "apif_fixtures_2026-11-04.json").read_text())
    api = json.loads((D / "apif_lineups_1629973.json").read_text())
    rec = jleague.map_fixture(jleague.parse_match(POST, URL_POST), fx)
    api[1]["startXI"][0]["player"]["number"] = 99                      # 号码不同
    api[0]["startXI"][0]["player"]["name"] = "Totally Different"       # 同号不同人
    res = crosscheck_lineup(rec, api)
    assert res["verdict"] == "冲突" and "不进入推荐" in res["reason"]
    assert res["teams"]["away"]["only_api_football"][0]["number"] == 99
    assert res["teams"]["home"]["name_mismatch"][0]["number"] == 21
    assert crosscheck_lineup(rec, [])["verdict"] == "暂无"
    rec2 = dict(rec, match_id=None)
    assert crosscheck_lineup(rec2, api)["verdict"] == "暂无"


def test_names_compatible():
    assert names_compatible("HWANG Injae", "Hwang In-Jae")
    assert names_compatible("ELI ADAMS", "E. Adams")
    assert names_compatible("KIM Yesung", "Ye-Sung Kim")
    assert names_compatible("JUANPE", "Juanpe")
    assert names_compatible(None, "X Y") is None
    assert names_compatible("TANAKA Ao", "Suzuki Ken") is False


# ---------- 其他站点 ----------
def test_probe_detects_afc_spa_shell():
    assert "单页应用" in probes.detect("afc", (D / "afc_shell.html").read_text())
    assert probes.detect("aleagues", '<a href="<%= permalink %>">') is not None
    assert probes.detect("afc", '<div>Starting XI</div>') is None


def test_probe_spl_disabled_no_request():
    with pytest.raises(SourceDisabled):
        probes.probe("spl", fetcher=PoliteFetcher("spl", "https://www.spl.com.sa", session=FakeSession({})))
    assert "条款" in status("spl")["last_error"]


def test_signature_ignores_optional_keys_but_flags_missing_ones():
    fl = jleague.extract_flight(POST)
    h, lu = jleague.find_header(fl), jleague.find_lineup(fl)
    base = jleague.signature(h, lu)
    h2 = dict(h, liveMatchInfo={}, lang="en")
    h2["homeTeam"] = dict(h["homeTeam"], href="/x")
    assert jleague.signature(h2, lu) == base
    h3 = {k: v for k, v in h.items() if k != "section"}
    assert jleague.signature(h3, lu) != base
