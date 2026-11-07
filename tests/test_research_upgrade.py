"""2026-11-07 二次升级：因子证据 / 时间外回测 / 缺口告警的防幻觉约束。

核心不变量：不编造数据、缺证据不进评分、样本不足不输出准确率结论、不改写真实配置。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import yaml

from ah import backtest, factors, sample_gaps

NOW = datetime(2026, 11, 7, 0, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------- 因子
def test_line_value_uses_real_consensus_and_never_invents():
    p = {"line": -1.0, "odds": 1.07, "kickoff_utc": "2026-11-07T15:00:00Z",
         "market_consensus": {"bookmakers": 12, "agree": 11, "pick_line": -1.0, "pick_water": 1.06}}
    ev = factors.line_value_feature(p)
    assert ev["value"] is not None and ev["coverage_status"] == "full"
    assert ev["source_id"] == "report.market_consensus" and ev["as_of"] == p["kickoff_utc"]
    assert "12" in ev["note"] and "11" in ev["note"]          # 逐字可回溯
    assert "去水" in ev["note"]                                # 明示未去水


def test_line_value_missing_or_thin_consensus_is_none_not_zero():
    assert factors.line_value_feature({"line": -0.5})["value"] is None
    thin = {"line": -0.5, "market_consensus": {"bookmakers": 2, "agree": 2}}
    ev = factors.line_value_feature(thin)
    assert ev["value"] is None and ev["coverage_status"] == "uncertain"


def test_home_away_requires_three_real_home_and_away_games():
    rows = [
        {"kickoff_utc": "2026-11-01T12:00:00Z", "home": "A", "away": "X", "home_goals": 2, "away_goals": 0},
        {"kickoff_utc": "2026-11-02T12:00:00Z", "home": "A", "away": "Y", "home_goals": 1, "away_goals": 1},
        {"kickoff_utc": "2026-11-03T12:00:00Z", "home": "A", "away": "Z", "home_goals": 3, "away_goals": 1},
        {"kickoff_utc": "2026-11-01T14:00:00Z", "home": "P", "away": "B", "home_goals": 0, "away_goals": 2},
        {"kickoff_utc": "2026-11-02T14:00:00Z", "home": "Q", "away": "B", "home_goals": 0, "away_goals": 1},
        {"kickoff_utc": "2026-11-03T14:00:00Z", "home": "R", "away": "B", "home_goals": 2, "away_goals": 2},
    ]
    ev = factors.home_away_feature(rows, "A", "B", "2026-11-07T15:00:00Z", "home")
    assert ev["coverage_status"] == "full" and isinstance(ev["value"], float)
    assert "3 场" in ev["note"] or "3" in ev["note"]
    empty = factors.home_away_feature(rows[:2], "A", "B", "2026-11-07T15:00:00Z", "home")
    assert empty["value"] is None and empty["coverage_status"] == "partial"
    assert factors.home_away_feature([], "A", "B", "2026-11-07T15:00:00Z", "home")["value"] is None


def test_motivation_requires_real_standings_positions():
    f = {"standings": {"home": {"position": 3, "points": 30}, "away": {"position": 15, "points": 12}}}
    ev = factors.motivation_feature(f, "home", "2026-11-07T15:00:00Z")
    assert ev["coverage_status"] == "full" and ev["value"] > 0
    assert "3" in ev["note"] and "15" in ev["note"]
    assert factors.motivation_feature({"standings": {}}, "home", "x")["value"] is None
    assert factors.motivation_feature(None, "home", "x")["value"] is None


def test_injuries_and_xg_stay_missing():
    for k in ("injuries", "xg"):
        ev = factors.unavailable(k, "2026-11-07T15:00:00Z")
        assert ev["value"] is None and ev["coverage_status"] == "missing"
        assert "未取得" in ev["source_id"]


def test_observation_mode_does_not_touch_features(monkeypatch):
    monkeypatch.setattr(factors, "mode", lambda: "observation")
    ev = {"home_away": {"value": 1.5, "coverage_status": "full"},
          "motivation": {"value": None, "coverage_status": "missing"}}
    assert factors.apply_to_features({"form": 0.4}, ev) == {"form": 0.4}


def test_scoring_mode_writes_only_full_evidence(monkeypatch):
    monkeypatch.setattr(factors, "mode", lambda: "scoring")
    ev = {"home_away": {"value": 1.5, "coverage_status": "full"},
          "motivation": {"value": None, "coverage_status": "missing"},
          "line_value": {"value": 0.8, "coverage_status": "partial"}}
    out = factors.apply_to_features({"form": 0.4}, ev)
    assert out == {"form": 0.4, "home_away": 1.5}
    assert "motivation" not in out and "line_value" not in out   # None / partial 不填 0


def test_pre_match_values_are_side_specific_and_never_invent():
    match = {"kickoff_utc": "2026-11-07T15:00:00Z", "home": "A", "away": "B"}
    pre = factors.pre_match_values(match, None, [])
    assert set(pre) == {"home", "away"}
    for side in pre:
        assert pre[side]["features"] == {}                    # 无赛果/无积分榜 → 不写
        assert pre[side]["evidence"]["home_away"]["value"] is None
        assert pre[side]["evidence"]["motivation"]["value"] is None


def test_final_evidence_never_mutates_report():
    pick = {"match_id": "m1", "pick_side": "away", "home": "A", "away": "B",
            "kickoff_utc": "2026-11-07T15:00:00Z", "line": -0.5, "score": 3.2,
            "market_consensus": {"bookmakers": 8, "agree": 7}}
    before = json.dumps(pick, sort_keys=True, ensure_ascii=False)
    ev = factors.final_evidence(pick, None, [])
    assert json.dumps(pick, sort_keys=True, ensure_ascii=False) == before
    assert ev["factors"]["injuries"]["value"] is None
    assert ev["factors"]["xg"]["value"] is None
    assert ev["mode"] == factors.mode()


# ---------------------------------------------------------------- 自动验收
def test_readiness_not_ready_with_one_day(monkeypatch, tmp_path):
    cov = tmp_path / "cov"
    cov.mkdir()
    (cov / "2026-11-07.json").write_text(json.dumps({
        "date": "2026-11-07", "mode": "observation",
        "rows": [{"match_id": "x", "factors": {"line_value": {"coverage": "full"},
                                               "home_away": {"coverage": "missing"},
                                               "motivation": {"coverage": "missing"}}}]}),
        encoding="utf-8")
    monkeypatch.setattr(factors, "COVERAGE_DIR", cov)
    monkeypatch.setattr(factors, "READINESS", tmp_path / "readiness.json")
    r = factors.readiness()
    assert r["status"] == "NOT_READY" and not r["all_eligible"]
    assert r["factors"]["line_value"]["days_observed"] == 1


def test_promote_rejected_and_settings_untouched(monkeypatch, tmp_path):
    cfg = tmp_path / "config"; cfg.mkdir()
    settings = {"confidence_thresholds": {"low": 2.0},
                "feature_scoring": {"mode": "observation", "promotion": {}}}
    (cfg / "settings.yaml").write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")
    cov = tmp_path / "cov"; cov.mkdir()
    monkeypatch.setattr(factors, "CONFIG", cfg)
    monkeypatch.setattr(factors, "DATA", tmp_path)
    monkeypatch.setattr(factors, "COVERAGE_DIR", cov)
    monkeypatch.setattr(factors, "READINESS", tmp_path / "readiness.json")
    r = factors.enable_scoring()
    assert r["status"] == "REJECTED" and "未达标" in r["reason"]
    assert yaml.safe_load((cfg / "settings.yaml").read_text(encoding="utf-8")) == settings


def test_replay_does_not_rewrite_report(tmp_path):
    rep = tmp_path / "reports" / "2026" / "11"
    rep.mkdir(parents=True)
    f = rep / "2026-11-07.json"
    data = {"date": "2026-11-07", "picks": [
        {"match_id": "m1", "score": 4.05, "confidence": "high", "line": -1.0,
         "market_consensus": {"bookmakers": 12, "agree": 11}}]}
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    out = factors.replay("2026-11-07", tmp_path / "reports")
    assert out["status"] == "REPLAY_OK" and out["picks_before"] == 1
    assert json.loads(f.read_text(encoding="utf-8")) == data
    assert "未经时间外校准" in out["note"]          # 必须披露 line_value 偏正


# ---------------------------------------------------------------- 回测
def test_backtest_insufficient_sample_outputs_no_accuracy_claims():
    rows = [{"settled": True, "result": "2-1", "ah_outcome": "win", "units": "1.0",
             "confidence": "high", "odds": "0.9"} for _ in range(5)]
    ev = backtest.evaluate(rows)
    assert ev["status"] == "INSUFFICIENT_SAMPLE"
    assert ev["brier_score"] == "NOT_COMPUTED" and ev["reliability_curve"] == "NOT_COMPUTED"
    assert "market_baseline" not in ev and "overall" not in ev
    assert "不输出命中率结论" in ev["note"]


def test_backtest_never_invents_result_for_missing_ledger():
    pools = [{"date": "2026-11-07", "pool_date": "2026-11-07", "match_id": "m1", "confidence": "low"}]
    rows = backtest.join(pools, {})
    assert rows[0]["ledger_found"] is False
    assert rows[0]["result"] == backtest.MISSING and rows[0]["settled"] is False
    assert rows[0]["ledger_found"] is False


def test_time_split_is_chronological_not_random():
    rows = [{"date": d} for d in ("2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08")]
    s = backtest.time_split(rows)
    assert s["dates"] == sorted(s["dates"])
    assert s["train"][-1] < s["test"][0]
    assert not (set(s["train"]) & set(s["test"]))
    assert "不随机打散" in s["note"]


def test_market_implied_labels_single_side():
    assert backtest.market_implied(0.9) == round(1 / 1.9, 4)
    assert backtest.market_implied(backtest.MISSING) is None


# ---------------------------------------------------------------- 缺口与归因
def test_gap_has_no_eta_when_no_recent_settled():
    pools = [{"date": "2026-11-07", "pool_date": "2026-11-07", "match_id": "m1",
              "confidence": "low", "result": "未取得", "settled": False}]
    d = sample_gaps.gap_payload(pools=pools, ledger={}, now=NOW)
    assert d["estimated_days_to_total_threshold"] is None
    assert "无法推算" in d["estimate_note"]
    assert d["alert"].startswith("SAMPLE_INSUFFICIENT")


def test_health_classifies_not_started_without_inventing_reason(tmp_path):
    import csv
    csvp = tmp_path / "p.csv"
    with csvp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "match_id", "home", "away", "kickoff_utc",
                                          "settled", "status", "result", "confidence"])
        w.writeheader()
        w.writerow({"date": "2026-11-07", "match_id": "m1", "home": "A", "away": "B",
                    "kickoff_utc": "2026-11-07T15:00:00Z", "settled": "false",
                    "status": "active", "result": "", "confidence": "low"})
    out = tmp_path / "h.json"
    h = sample_gaps.enriched_health(csv_path=csvp, out=out, now=NOW)
    assert h["autofill_eligible_unsettled"] == 0
    assert "尚未开赛" in h["unsettled"][0]["attribution"]
    assert h["unsettled"][0]["result"] == "未取得"
    assert "不推断" in h["note"]
