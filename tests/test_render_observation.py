"""展示层：所有入口共用同一渲染器，免责标记逐条可见。"""
from __future__ import annotations

import json
from pathlib import Path

from ah import grounding, publish_telegram, render_observation as ro, recheck


def _report(n=2):
    return {"picks": [], "watchlist": [
        {"home": f"H{i}", "away": f"A{i}", "league": "英超", "kickoff_utc": "2026-11-08T14:00:00Z",
         "withheld_reason": "核验未通过",
         "observation": {"pick_team": f"H{i}", "line": -0.5, "line_open": -0.25, "odds": 0.95,
                         "score": 4.0 + i, "bookmaker": "pinnacle", "source": "oddspapi",
                         "home_form": "WLL", "away_form": "LDW", "price_ts": "2026-11-07T22:00:00Z"}}
        for i in range(n)]}


def test_single_source_renderer():
    blocks = ro.render_observation_section(_report())
    assert len(blocks) == 1
    assert blocks[0].count("⚽") == 2
    # 每条观察都带免责标记，不只 section 标题
    assert blocks[0].count(ro.DISCLAIMER) >= 3
    assert ro.validate(blocks[0]) == []


def test_publish_telegram_delegates():
    assert publish_telegram.observation_blocks(_report(3)) == ro.render_observation_section(_report(3))
    assert publish_telegram.observation_blocks(_report(), 7) == []


def test_grounding_delegates():
    bad = "🔎 比赛观察分析\n📈 模型观察 评分 3.9"
    assert any("非正式推荐" in e for e in grounding.check_observation_markup({}, bad))
    assert grounding.check_observation_markup({}, bad + "\n（非正式推荐 / 不计战绩）\n") == []
    assert grounding.check_observation_markup({}, "正常日报，无观察块") == []


def test_banned_assertion_is_caught():
    bad = "比赛观察分析 非正式推荐 不计战绩 🎯 <b>推荐 主队 -0.5</b>"
    assert any("🎯" in e for e in ro.validate(bad))


def test_missing_fields_are_not_invented():
    txt = ro.render_one({"home": "H", "away": "A", "observation": {}})
    assert "未取得" in txt and ro.DISCLAIMER in txt
    assert "None" not in txt


def test_recheck_uses_canonical_renderer(monkeypatch):
    seen = {}

    def fake(r):
        seen["called"] = True
        return ["ok"]
    monkeypatch.setattr(ro, "render_observation_section", fake)
    import importlib
    mod = importlib.import_module("ah.recheck")
    # collect() 会真的采集，这里只验证模块能导入且引用了规范化函数
    assert hasattr(mod, "collect")
    assert "render_observation_section" in Path("src/ah/recheck.py").read_text(encoding="utf-8")
