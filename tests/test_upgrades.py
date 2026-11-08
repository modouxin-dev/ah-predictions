"""收尾升级回归测试：运行事实、新鲜度、补发模式、观察块防回退。

纯本地、无网络、不发消息、不改模型算法。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from ah import delivery, grounding, runfacts


def test_mark_records_events_and_caps_runs(tmp_path):
    p = tmp_path / "run_facts.json"
    now = datetime(2026, 11, 8, 4, tzinfo=timezone.utc)
    for i in range(3):
        runfacts.mark(f"stage_{i}", "run-A", "1", p, now + timedelta(seconds=i))
    d = json.loads(p.read_text(encoding="utf-8"))
    assert [e["stage"] for e in d["runs"]["run-A"]["events"]] == ["stage_0", "stage_1", "stage_2"]
    # 超过上限只保留最近 N 个 run
    for i in range(runfacts.MAX_RUNS + 3):
        runfacts.mark("x", f"run-{i}", "1", p, now + timedelta(minutes=i))
    d = json.loads(p.read_text(encoding="utf-8"))
    assert len(d["runs"]) <= runfacts.MAX_RUNS


def test_mark_rejects_empty_stage(tmp_path):
    with pytest.raises(RuntimeError):
        runfacts.mark("  ", "r", "1", tmp_path / "f.json")


def test_freshness_flags_stale_report_without_blocking(tmp_path):
    now = datetime(2026, 11, 8, 10, tzinfo=timezone.utc)
    old = (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rep = {"date": "2026-11-08", "generated_at": old, "picks": [{"price_ts": old}]}
    out = runfacts.freshness(rep, now=now)
    assert out["status"] == "STALE_OR_MISSING"
    assert any(a["code"] == "STALE_REPORT" for a in out["alerts"])
    assert out["report_age_minutes"] == 300
    assert out["price_ts_stale"] == 1


def test_freshness_missing_fields_are_not_invented():
    now = datetime(2026, 11, 8, 10, tzinfo=timezone.utc)
    out = runfacts.freshness({"date": "2026-11-08", "picks": [{}]}, now=now)
    assert out["generated_at"] == runfacts.MISSING
    assert out["price_ts_missing"] == 1
    assert any(a["code"] == "GENERATED_AT_MISSING" for a in out["alerts"])


def test_freshness_fresh_report_is_ok():
    now = datetime(2026, 11, 8, 10, tzinfo=timezone.utc)
    ts = (now - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = runfacts.freshness({"date": "2026-11-08", "generated_at": ts,
                              "picks": [{"price_ts": ts}]}, now=now)
    assert out["status"] == "OK" and out["alerts"] == []


def test_backfill_mode_does_not_force_resend(monkeypatch, tmp_path):
    monkeypatch.setenv("AH_DELIVERY_MODE", "backfill")
    monkeypatch.setenv("AH_SLOT", "manual")
    assert delivery._force() is False, "补发必须按 slot 幂等，不能强制重发"


def test_invalid_delivery_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("AH_DELIVERY_MODE", "hack")
    with pytest.raises(RuntimeError, match="非法 AH_DELIVERY_MODE"):
        delivery.delivery_mode_env()


def test_manual_slot_still_forces_when_not_backfill(monkeypatch):
    monkeypatch.delenv("AH_DELIVERY_MODE", raising=False)
    monkeypatch.setenv("AH_SLOT", "manual")
    assert delivery._force() is True  # 兼容既有行为


def test_observation_markup_guard_when_present():
    rep = {"date": "2026-11-08", "picks": [], "watched": [], "watchlist": []}
    bad = "🔎 比赛观察分析\n📈 模型观察 评分 3.9"
    errs = grounding.check_observation_markup(rep, bad)
    assert any("非正式推荐" in e for e in errs)
    assert any("不计战绩" in e for e in errs)
    good = bad + "\n（非正式推荐 / 不计战绩）\n"
    assert grounding.check_observation_markup(rep, good) == []


def test_observation_guard_does_not_block_normal_daily():
    """当前群消息不含观察块，检查必须放行，不能误拦正常日报。"""
    rep = {"date": "2026-11-08", "picks": [], "watched": [], "watchlist": []}
    assert grounding.check_observation_markup(rep, "【先知】⚽ 亚盘日报 · 0 场正式推荐") == []


def test_observation_guard_rejects_recommendation_wording():
    rep = {"date": "2026-11-08", "picks": [], "watched": [], "watchlist": []}
    bad = "比赛观察分析 非正式推荐 不计战绩 🎯 <b>推荐 主队 -0.5</b>"
    assert any("推荐" in e for e in grounding.check_observation_markup(rep, bad))
