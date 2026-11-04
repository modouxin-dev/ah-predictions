import copy
import json
from pathlib import Path

from ah import build_site, policy, publish_telegram, render_report
from ah.stats import compute

POL = policy.load()
HIST = json.loads(Path("reports/2026/11/2026-11-04.json").read_text())


def pk(mid, conf, odds=0.9, line=-0.5, mc=None, score=8):
    p = {"match_id": mid, "league": "L", "home": "H" + mid, "away": "A" + mid, "kickoff_utc": "2026-11-05T12:00:00Z",
         "pick_side": "home", "line": line, "odds": odds, "confidence": conf, "score": score,
         "reasons": ["r"], "risks": [], "sources": ["https://x.example/"]}
    if mc is not None:
        p["market_consensus"] = {"source": "s", "bookmakers": 3, "agree": 2, "home_line": -mc, "pick_line": mc}
    return p


def rep(picks):
    return {"date": "2026-11-05", "generated_at": "2026-11-05T00:00:00Z", "picks": picks, "watched": []}


def test_settings_policy():
    assert POL["min_confidence_to_publish"] == "高" and POL["max_picks_per_day"] == 3
    assert POL["require_real_odds"] and POL["prematch_cancel_on_confidence_drop"]
    assert "0.25" in POL["consensus_alignment"]


def test_apply_only_high_and_watchlist():
    r = rep([pk("1", "high"), pk("2", "medium"), pk("3", "low"), pk("4", "high", odds=None),
             pk("5", "high", mc=-1.0), pk("6", "high", mc=-1.25), pk("7", "high", mc=-0.5)])
    policy.apply(r, POL)
    assert [p["match_id"] for p in r["picks"]] == ["1", "7"]
    why = {w["match_id"]: w["withheld_reason"] for w in r["watchlist"]}
    assert set(why) == {"2", "3", "4", "5", "6"}
    assert "赔率" in why["4"] and "信心中" in why["5"] and "偏离" in why["6"]
    render_report.validate(r)


def test_max_three():
    r = policy.apply(rep([pk(str(i), "high", score=i) for i in range(5)]), POL)
    assert [p["match_id"] for p in r["picks"]] == ["4", "3", "2"] and len(r["watchlist"]) == 2


def test_history_untouched():
    h = copy.deepcopy(HIST)
    policy.apply(h, POL)
    assert h == HIST


def test_outputs_hide_non_high_and_carry_disclaimer(tmp_path):
    r = policy.apply(rep([pk("1", "high"), pk("2", "medium")]), POL)
    r["picks"].append(pk("9", "medium"))           # 即使误入 picks 也不展示
    t = publish_telegram.summary(r, stats=compute([]))
    assert "H1" in t and "H2" not in t and "H9" not in t and policy.DISCLAIMER in t and "历史战绩" in t
    assert policy.DISCLAIMER in publish_telegram.summary(rep([]))
    h = build_site.report_html(r)
    picks_part, _, watch_part = h.partition("观察名单")
    assert "H1" in picks_part and "H9" not in h and "H2" not in picks_part   # 非高信心不作为推荐卡片展示
    assert "H2" in watch_part and "不推荐" in h                              # 只出现在观察名单（不推荐）
    md = render_report.render_md(r)
    assert "H9" not in md and policy.DISCLAIMER in md and md.rstrip().endswith("—— 先知")


def test_cancel_on_drop():
    p = pk("1", "high")
    assert policy.cancel_on_drop(p, "中", POL) and p["cancelled"] and "高" in p["cancel_reason"]
    q = pk("2", "high")
    assert not policy.cancel_on_drop(q, "high", POL) and "cancelled" not in q
    p2 = pk("3", "high"); p2["cancelled"] = True
    assert policy.published(rep([p2]), POL) == []


def test_history_final_check_cancel_hidden():
    ids = [p["match_id"] for p in policy.published(HIST, POL)]
    assert "titan007-3068888" not in ids and "titan007-3073152" in ids
