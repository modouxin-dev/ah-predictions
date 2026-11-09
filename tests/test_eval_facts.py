"""ROUND 6 / A3-A5：评估边界、联赛事实、结构化告警（只读、不夸大、只通知）。"""
import json
from datetime import datetime, timezone

import pytest

from ah import eval_facts


def _seed(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "backtest_report.json").write_text(json.dumps({
        "evaluation": {"status": "INSUFFICIENT_SAMPLE",
                       "thresholds": {"min_per_tier": 80, "min_total": 300}}}), encoding="utf-8")
    (d / "rule_lab.json").write_text(json.dumps({
        "model_under_test": "线上规则档位（analyze.py / policy.py），未改动任何一行",
        "live_model_modified": False, "joined_decided_rows": 9,
        "walk_forward": {"overall": {"status": "INSUFFICIENT_SAMPLE"}}}), encoding="utf-8")
    (d / "stats.json").write_text(json.dumps({"by_league": {
        "英超": {"picks": 3, "decided": 3, "hit_rate": 0.6667},
        "日职乙": {"picks": 1, "decided": 1, "hit_rate": 0.0}}}), encoding="utf-8")
    (d / "selection_funnel.json").write_text(json.dumps({
        "days": [{"date": f"2026-11-0{i}", "categories": {"近期状态数据不足": n},
                  "zero_pick_day": n == 0}
                 for i, n in enumerate((9, 10, 10, 0), start=6)],
        "summary": {"max_outside_screen": 47,
                    "data_gap_by_league": {"日职乙": 11, "英超": 1},
                    "observations": ["连续 3 天『近期状态数据不足』≥5 场（依据：报告字段）"]}}), encoding="utf-8")
    (d / "clv_join_facts.json").write_text(json.dumps({
        "joined_unique_match_ids": 8,
        "price_clv_status_by_row": {"收盘盘口≠下注盘口（按口径不算 CLV）": 7,
                                   "无收盘快照": 17}}), encoding="utf-8")
    rep = tmp_path / "reports" / "2026" / "11"
    rep.mkdir(parents=True)
    (rep / "2026-11-08.json").write_text(json.dumps({
        "date": "2026-11-08", "not_covered": "尚未自动采集伤停或首发"}), encoding="utf-8")
    return d, tmp_path / "reports"


def test_model_scope_is_explicit_and_not_migratable(tmp_path):
    d, rep = _seed(tmp_path)
    o = eval_facts.build(data=d, reports=rep, now=datetime(2026, 11, 8, tzinfo=timezone.utc))
    ms = o["model_scope"]
    assert ms["status"] == "FACTS_ONLY"
    assert "不是同一套可互证结论" in ms["boundary"] and "不能迁移" in ms["boundary"]
    assert any("live_model_modified：False" in x for x in ms["research_facts"])
    assert "不构成统计预测准确率" in ms["live_publication_note"]


def test_league_facts_never_quote_small_samples(tmp_path):
    d, rep = _seed(tmp_path)
    o = eval_facts.build(data=d, reports=rep)
    lf = o["league_facts"]
    assert lf["min_sample"] == 30 and lf["consecutive_data_gap_days"] == 3
    assert lf["by_league"]["英超"]["hit_rate_conclusion"] == "未取得"
    assert lf["by_league"]["英超"]["sample_status"] == "INSUFFICIENT_SAMPLE"
    assert lf["by_league"]["日职乙"]["data_gap_withheld"] == 11
    assert "不输出联赛优劣" in lf["note"]


def test_alerts_are_notify_only_and_evidence_backed(tmp_path):
    d, rep = _seed(tmp_path)
    o = eval_facts.build(data=d, reports=rep)
    codes = {a["code"] for a in o["alerts"]}
    assert {"ZERO_PICK_DAY", "PERSISTENT_DATA_GAP", "UPSTREAM_SCREEN_CAP",
            "PRICE_CLV_NOT_QUOTED", "MODEL_SCOPE_BOUNDARY", "COLLECTION_SELF_REPORT"} <= codes
    assert all(a["action"] == "notify_only" and a["evidence"] for a in o["alerts"])
    assert "不自动补发" in o["alert_policy"]


def test_no_fabrication_when_files_missing(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    r = tmp_path / "reports"
    r.mkdir()
    o = eval_facts.build(data=d, reports=r)
    assert o["model_scope"]["status"] == "暂无研究层事实文件"
    assert o["league_facts"]["by_league"] == {}
    assert o["league_facts"]["consecutive_data_gap_days"] == 0
    assert o["alerts"] == []


def test_forbidden_claims_fail_closed(monkeypatch, tmp_path):
    d, rep = _seed(tmp_path)
    monkeypatch.setattr(eval_facts, "_model_scope",
                        lambda b, r: {"boundary": "准确率提升，模型更准", "research_facts": [],
                                      "live_publication_note": "", "files": [], "status": "x"})
    with pytest.raises(RuntimeError, match="禁止断言"):
        eval_facts.build(data=d, reports=rep)


def test_cmd_atomic_and_deterministic(tmp_path):
    d, rep = _seed(tmp_path)
    out = tmp_path / "data" / "eval_facts.json"
    o = eval_facts.cmd(out_path=out, data=d, reports=rep,
                       now=datetime(2026, 11, 8, 1, 2, 3, tzinfo=timezone.utc))
    assert json.loads(out.read_text(encoding="utf-8")) == o
    assert o["checked_at"] == "2026-11-08T01:02:03Z"
    assert not list(d.glob(".eval-*"))


def test_zero_fixture_window_alert_is_notify_only(tmp_path):
    d, rep = _seed(tmp_path)
    f = json.loads((d / "selection_funnel.json").read_text(encoding="utf-8"))
    f["days"].append({"date": "2026-11-10", "categories": {}, "zero_pick_day": True,
                      "upstream": {"window_fixtures": 0}})
    (d / "selection_funnel.json").write_text(json.dumps(f, ensure_ascii=False), encoding="utf-8")
    o = eval_facts.build(data=d, reports=rep)
    a = next(x for x in o["alerts"] if x["code"] == "ZERO_FIXTURE_WINDOW")
    assert "扩大初筛上限无效" in a["message"] and a["action"] == "notify_only"
    assert "不编造赛程" in a["message"]
