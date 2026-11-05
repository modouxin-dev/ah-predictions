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
    assert POL["min_confidence_to_publish"] == "中" and POL["max_picks_per_day"] == 10
    assert POL["max_abs_line_to_publish"] == 1.75
    assert POL["require_real_odds"] and POL["prematch_cancel_on_confidence_drop"]
    assert "0.25" in POL["consensus_alignment"]


def test_apply_medium_and_watchlist():
    """中信心可通过；低仍拦；深盘 |line|≥1.75 拦；偏离规则保留。"""
    r = rep([
        pk("1", "high"),
        pk("2", "medium"),
        pk("3", "low"),
        pk("4", "high", odds=None),
        pk("5", "high", mc=-1.0),          # gap=0.5 → 降为中，可通过
        pk("6", "high", mc=-1.25),         # gap=0.75 → 偏离撤下
        pk("7", "high", mc=-0.5),
        pk("8", "medium", line=-1.75),     # 深盘挡
        pk("9", "high", line=-2.0),        # 深盘挡
    ])
    policy.apply(r, POL)
    ids = [p["match_id"] for p in r["picks"]]
    assert "1" in ids and "2" in ids and "7" in ids
    assert "3" not in ids and "4" not in ids and "6" not in ids
    assert "8" not in ids and "9" not in ids
    why = {w["match_id"]: w["withheld_reason"] for w in r["watchlist"]}
    assert "赔率" in why["4"] and "偏离" in why["6"]
    assert "信心低" in why["3"]
    assert "深盘" in why["8"] and "深盘" in why["9"]
    # gap=0.5 降为中后应通过（或若被日上限挤出则在 watchlist）
    assert "5" in ids or "超过每日上限" in why.get("5", "")
    render_report.validate(r)


def test_deep_line_threshold():
    """|line|=1.5 可通过；≥1.75 不可。line 为 None 不按深盘拦（由无赔率等规则处理）。"""
    ok = policy.check(pk("a", "medium", line=-1.5), POL)
    assert ok[0] == "medium" and ok[1] == ""
    block175 = policy.check(pk("b", "high", line=-1.75), POL)
    assert block175[0] is None and "深盘" in block175[1]
    block20 = policy.check(pk("c", "high", line=-2.0), POL)
    assert block20[0] is None and "深盘" in block20[1]
    # line=None：不触发深盘（若有赔率则只看信心）
    no_line = pk("d", "medium", line=None)
    conf, why = policy.check(no_line, POL)
    assert conf == "medium" and why == ""


def test_max_ten():
    r = policy.apply(rep([pk(str(i), "high", score=i) for i in range(12)]), POL)
    assert [p["match_id"] for p in r["picks"]] == [str(i) for i in range(11, 1, -1)]
    assert len(r["picks"]) == 10 and len(r["watchlist"]) == 2


def test_history_untouched():
    h = copy.deepcopy(HIST)
    policy.apply(h, POL)
    assert h == HIST


def test_outputs_show_medium_hide_low_and_carry_disclaimer(tmp_path):
    r = policy.apply(rep([pk("1", "high"), pk("2", "medium"), pk("3", "low")]), POL)
    r["picks"].append(pk("9", "low"))           # 即使误入 picks 也不展示（低信心）
    t = publish_telegram.summary(r, stats=compute([]))
    assert "H1" in t and "H2" in t and "H3" not in t and "H9" not in t
    assert policy.DISCLAIMER in t and "历史战绩" in t
    assert policy.DISCLAIMER in publish_telegram.summary(rep([]))
    h = build_site.report_html(r)
    picks_part, _, watch_part = h.partition("观察名单")
    assert "H1" in picks_part and "H2" in picks_part
    assert "H9" not in h and "H3" not in picks_part
    assert "H3" in watch_part and "不推荐" in h
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
