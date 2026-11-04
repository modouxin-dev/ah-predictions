"""亚冠比分第二来源核对（离线）。"""
import copy
import json
from pathlib import Path

import pytest

from ah import source_status
from ah.scrapers import SiteUnavailable, StructureChanged, afc, jleague, scores
from ah.scrapers.http import PoliteFetcher

D = Path(__file__).parent / "data" / "scrapers"
ACLE = (D / "afc_acle_landing_2026-11-04.html").read_text(encoding="utf-8")
ACL2 = (D / "afc_acl2_landing_2026-11-04.html").read_text(encoding="utf-8")


def fx(fid, league, home, away, hg, ag, status="FT", date="2026-11-04T10:00:00+00:00", ft=None, et=None):
    return {"fixture": {"id": fid, "date": date, "status": {"short": status}},
            "league": {"id": league, "round": "Group Stage - 4"},
            "teams": {"home": {"name": home}, "away": {"name": away}},
            "goals": {"home": hg, "away": ag},
            "score": {"fulltime": {"home": (ft or (hg, ag))[0], "away": (ft or (hg, ag))[1]},
                      "extratime": {"home": et[0] if et else None, "away": et[1] if et else None}}}


def claims():
    return afc.parse_scores(ACLE, afc.COMP_PAGES["acle"], "acle") + afc.parse_scores(ACL2, afc.COMP_PAGES["acl2"], "acl2")


def test_parse_headlines_and_video_slugs():
    c = afc.parse_scores(ACLE, afc.COMP_PAGES["acle"], "acle", "2026-11-04T13:00:00Z")
    got = {(x["kind"], x["home"], x["home_goals"], x["away_goals"], x["away"]) for x in c}
    assert ("headline", "FC Pohang Steelers", 3, 3, "Gamba Osaka") in got
    assert ("video_slug", "al hilal sfc", 2, 0, "shabab al ahli") in got
    assert ("video_slug", "esteghlal", 1, 0, "al shamal sc") in got
    assert len(c) == 3                                      # 非比分标题（Inzaghi…）不产生声明；JSON-LD 与 h4 重复去重
    pohang = next(x for x in c if x["kind"] == "headline")
    assert pohang["stage"] == "MD4" and pohang["home_country"] == "KOR" and pohang["away_country"] == "JPN"
    c2 = afc.parse_scores(ACL2, afc.COMP_PAGES["acl2"], "acl2")
    heads = {(x["home"], x["home_goals"], x["away_goals"], x["away"]) for x in c2 if x["kind"] == "headline"}
    assert ("FC Seoul", 3, 0, "The Cong - Viettel FC") in heads and ("East Bengal FC", 0, 2, "Al Seeb Club") in heads
    assert not any("preview" in x["text"].lower() for x in c2)
    slug = next(x for x in c2 if x["kind"] == "video_slug")
    assert slug["source_url"].startswith(afc.COMP_PAGES["acl2"] + "/video/acltwo-|-group-d-")


def test_spa_shell_and_structure_change():
    with pytest.raises(SiteUnavailable):
        afc.parse_scores((D / "afc_shell.html").read_text(), afc.COMP_PAGES["acle"], "acle")
    with pytest.raises(StructureChanged):
        afc.parse_scores("<html><body>" + "x " * 2000 + "</body></html>", afc.COMP_PAGES["acle"], "acle")
    assert "seo-footer" in source_status.load()["sources"]["scraper_afc"]["last_error"]


def test_crosscheck_verified_conflict_missing():
    fixtures = [
        fx(1637494, 18, "Melbourne Victory", "Persib Bandung", 1, 2, date="2026-11-04T07:45:00+00:00"),
        fx(1630392, 18, "FC Seoul", "Viettel", 3, 1),                       # 官网 3-0 -> 冲突
        fx(1629973, 17, "Pohang Steelers", "Gamba Osaka", 3, 3),
        fx(1629974, 17, "Buriram United", "Kashima", None, None, status="NS", date="2026-11-04T12:15:00+00:00"),
        fx(1630393, 18, "Arkadag", "Gol Gohar", 1, 1, date="2026-11-04T13:45:00+00:00"),   # 官网没有
        fx(999, 39, "Arsenal", "Chelsea", 1, 0),                                           # 非亚冠，忽略
    ]
    res = scores.crosscheck_scores(fixtures, claims())
    by = {r["match_id"]: r for r in res["results"]}
    assert set(by) == {1637494, 1630392, 1629973, 1629974, 1630393}
    assert by[1637494]["verdict"] == "已验证" and by[1629973]["verdict"] == "已验证"
    c = by[1630392]
    assert c["verdict"] == "冲突" and "afc 3-0" in c["reason"] and "API-Football 3-1" in c["reason"]
    assert "fixtures?id=1630392" in c["reason"] and "the-afc.com" in c["reason"]
    assert by[1629974]["verdict"] == "暂无" and "未完场" in by[1629974]["reason"]
    assert by[1630393]["verdict"] == "暂无" and "没有这场比分" in by[1630393]["reason"]
    assert by[1629973]["kickoff_bjt"] == "2026-11-04T18:00:00+08:00"
    assert res["summary"] == {"已验证": 2, "冲突": 1, "暂无": 2}
    um = {(x["home"], x["away"]) for x in res["unmatched_claims"]}
    assert ("Al Hussein", "Al Shorta") in um and ("al hilal sfc", "shabab al ahli") in um


def test_crosscheck_reversed_and_competition_guard():
    res = scores.crosscheck_scores([fx(1, 18, "Persib Bandung", "Melbourne Victory", 2, 1)], claims())
    assert res["results"][0]["verdict"] == "冲突" and "主客顺序" in res["results"][0]["reason"]
    # 同名对阵若出现在另一项赛事页（acle）上不应被用来核对 acl2 比赛
    res = scores.crosscheck_scores([fx(2, 18, "Pohang Steelers", "Gamba Osaka", 3, 3)], claims())
    assert res["results"][0]["verdict"] == "暂无"


def test_crosscheck_extra_time_note():
    res = scores.crosscheck_scores([fx(3, 17, "Pohang Steelers", "Gamba Osaka", 3, 3, status="AET", ft=(2, 2), et=(1, 1))],
                                   claims())
    r = res["results"][0]
    assert r["verdict"] == "已验证" and "90 分钟 2-2" in r["reason"]


def test_jleague_claim_as_second_official_source():
    post = (D / "jleague_acle_2026_110401_postgame.html").read_text(encoding="utf-8")
    fixtures = json.loads((D / "apif_fixtures_2026-11-04.json").read_text())
    rec = jleague.map_fixture(jleague.parse_match(post, "https://www.jleague.jp/en/match/acle/2026/110401/"), fixtures)
    c = scores.jleague_claim(rec)
    assert c["source"] == "jleague" and (c["home_goals"], c["away_goals"]) == (3, 3) and c["match_id"] == 1629973
    f = fx(1629973, 17, "Pohang Steelers", "Gamba Osaka", 3, 3)
    r = scores.crosscheck_scores([f], claims() + [c])["results"][0]
    assert r["verdict"] == "已验证" and {o["source"] for o in r["official"]} == {"afc", "jleague"}
    f2 = copy.deepcopy(f)
    f2["goals"] = {"home": 3, "away": 2}
    r2 = scores.crosscheck_scores([f2], [c])["results"][0]
    assert r2["verdict"] == "冲突" and "jleague 3-3" in r2["reason"]
    pre = jleague.parse_match((D / "jleague_j1_2026_110706_pregame.html").read_text(encoding="utf-8"),
                              "https://www.jleague.jp/en/match/j1/2026/110706/")
    assert scores.jleague_claim(pre) is None


class _Resp:
    def __init__(self, status, text, headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}


class _Sess:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        return self.routes[url]


def test_fetch_scores_one_request_per_competition(tmp_path):
    PoliteFetcher._last_call.clear()
    routes = {"https://www.the-afc.com/robots.txt": _Resp(200, "Sitemap: https://www.the-afc.com/sitemap.xml\n"),
              afc.COMP_PAGES["acle"]: _Resp(200, ACLE), afc.COMP_PAGES["acl2"]: _Resp(403, "denied")}
    f = PoliteFetcher("afc", afc.BASE, cache_dir=tmp_path, session=_Sess(routes), min_interval_s=0,
                      sleep=lambda s: None)
    got = afc.fetch_scores(fetcher=f)
    assert got["pages"]["acle"]["ok"] and got["pages"]["acle"]["claims"] == 3
    assert not got["pages"]["acl2"]["ok"] and "SiteUnavailable" in got["pages"]["acl2"]["error"]
    assert f.session.calls.count(afc.COMP_PAGES["acle"]) == 1
