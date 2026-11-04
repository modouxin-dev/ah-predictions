import copy
import json
from pathlib import Path

from ah import consensus, odds_api
from ah.render_report import validate

FIX = json.loads((Path(__file__).parent / "data" / "odds_api_spreads.json").read_text())
LEAGUES = [{"id": "ucl", "name": "UEFA Champions League", "cn": "欧冠"},
           {"id": "acl_elite", "name": "AFC Champions League Elite", "cn": "亚冠精英"},
           {"id": "j1", "name": "J1 League", "cn": "日职联"}, {"id": "j2", "name": "J2 League", "cn": "日职乙"}]


def _events():
    ev = FIX[0]
    b = odds_api.normalize_spreads(ev)
    return {"oddsapi-ev1": {"home": ev["home_team"], "away": ev["away_team"], "commence": ev["commence_time"],
                            "consensus": odds_api.consensus(b), "books": b}}


def _report():
    pick = {"match_id": "t1", "league": "UEFA Champions League（欧冠 联赛阶段第4轮）", "home": "波尔图 Porto",
            "away": "那不勒斯 Napoli", "kickoff_utc": "2026-11-04T20:00:00Z", "pick_side": "away", "line": 0.25,
            "odds": 0.9, "confidence": "medium", "reasons": ["x"], "risks": [], "sources": ["https://a.example/"]}
    acl = dict(pick, match_id="t2", league="AFC Champions League Elite", home="A", away="B")
    return {"date": "2026-11-04", "generated_at": "2026-11-04T00:00:00Z", "picks": [pick, copy.deepcopy(pick) | {"match_id": "t3"}, acl], "watched": []}


def test_league_id():
    assert consensus.league_id("UEFA Champions League（欧冠）", LEAGUES) == "ucl"
    assert consensus.league_id("日职乙 第30轮", LEAGUES) == "j2"
    assert consensus.league_id("Qatar", LEAGUES) is None


def test_attach_one_call_per_league_and_pick_perspective():
    calls = []

    def fetch(sport):
        calls.append(sport)
        return _events()

    r = _report()
    consensus.attach(r, fetch=fetch, aliases={}, leagues=LEAGUES)
    assert calls == ["soccer_uefa_champs_league"]           # 两场欧冠只调一次，亚冠不调用
    mc = r["picks"][0]["market_consensus"]
    assert mc["bookmakers"] == 3 and mc["agree"] == 2 and mc["home_line"] == -0.25
    assert mc["pick_line"] == 0.25 and mc["pick_water"] == 0.895   # 客队视角
    assert mc["pinnacle"]["pick_line"] == 0.25
    assert "market_consensus" not in r["picks"][2]
    assert consensus.SOURCE_URL in r["picks"][0]["sources"]
    validate(r)


def test_no_match_or_failure_is_backward_compatible():
    r = _report()
    r["picks"][0]["kickoff_utc"] = "2026-11-05T20:00:00Z"   # 时间不符
    consensus.attach(r, fetch=lambda s: {}, aliases={}, leagues=LEAGUES)
    assert all("market_consensus" not in p for p in r["picks"])
    validate(r)


def test_fuzzy_threshold():
    ev = _events()
    assert consensus.find_event(ev, "Porto", "Napoli", "2026-11-04T20:05:00Z", aliases={})
    assert consensus.find_event(ev, "Benfica", "Napoli", "2026-11-04T20:00:00Z", aliases={}) is None


def test_template_renders_consensus():
    from jinja2 import Environment, FileSystemLoader
    from ah import TEMPLATES
    r = _report()
    consensus.attach(r, fetch=lambda s: _events(), aliases={}, leagues=LEAGUES)
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), trim_blocks=True, lstrip_blocks=True)
    md = env.get_template("report.md.j2").render(r=r)
    assert "多家公司共识" in md and "2/3 家一致" in md and "Pinnacle +0.25" in md
