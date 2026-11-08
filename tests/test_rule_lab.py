"""MODEL CHANGE 研究层：时序样本外、样本门槛、防幻觉闸门、不碰线上模型。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ah import rule_lab as rl


def _pool(mid, date, conf="high", score=5.0):
    return {"pool_date": date, "match_id": mid, "date": date, "kickoff_utc": f"{date}T12:00:00Z",
            "sample_role": "recommended", "confidence": conf, "score": score, "line": -0.5}


def _out(mid, out, units="1.0", date="2026-11-06", p=None):
    return {"result": "2-1", "ah_outcome": out, "units": float(units), "odds_estimated": "false",
            "confidence": "high", "line": -0.5, "kickoff_utc": f"{date}T12:00:00Z",
            "date": date, "model_p": p, "market_p": None}


def test_join_preserves_chronology_and_skips_unsettled():
    pools = [_pool("m2", "2026-11-07"), _pool("m1", "2026-11-06")]
    outs = {"m1": _out("m1", "win", date="2026-11-06"),
            "m2": _out("m2", "loss", date="2026-11-07"),
            "m3": _out("m3", "win")}
    rows = rl.join(pools, outs)
    assert [r["match_id"] for r in rows] == ["m1", "m2"]   # m3 不在候选池
    assert rows[0]["kickoff_utc"] < rows[1]["kickoff_utc"]


def test_small_sample_is_insufficient_and_no_hit_rate():
    pools = [_pool(f"m{i}", "2026-11-06") for i in range(5)]
    outs = {f"m{i}": _out(f"m{i}", "win") for i in range(5)}
    d = rl.build(pools, outs)
    ov = d["walk_forward"]["overall"]
    assert ov["status"] == "INSUFFICIENT_SAMPLE" and ov["hit_rate"] == rl.MISSING
    assert ov["roi"] == rl.MISSING
    assert d["live_model_modified"] is False


def test_calibration_not_computed_without_model_p():
    pools = [_pool(f"m{i}", "2026-11-06") for i in range(40)]
    outs = {f"m{i}": _out(f"m{i}", "win") for i in range(40)}
    d = rl.build(pools, outs)
    c = d["prediction_quality"]
    assert c["status"] == "NOT_COMPUTED" and c["brier"] == rl.MISSING
    assert "不是概率" in c["reason"] and c["reliability_bins"] == []
    assert d["walk_forward"]["overall"]["status"] == "SUFFICIENT"


def test_calibration_computes_only_with_real_probabilities():
    pools = [_pool(f"m{i}", "2026-11-06") for i in range(40)]
    outs = {f"m{i}": _out(f"m{i}", "win" if i % 2 == 0 else "loss", p=0.55) for i in range(40)}
    d = rl.build(pools, outs)
    c = d["prediction_quality"]
    assert c["status"] == "COMPUTED" and isinstance(c["brier"], float)
    assert c["reliability_bins"] and "不构成" in c["note"]


def test_tier_table_is_descriptive_not_calibrated():
    pools = [_pool(f"m{i}", "2026-11-06", conf="low") for i in range(12)]
    outs = {f"m{i}": _out(f"m{i}", "win") for i in range(12)}
    d = rl.build(pools, outs)
    t = d["tier_observation"]
    assert t["status"] == "DESCRIPTIVE_ONLY"
    assert "不是校准概率" in t["tiers"]["high"]["note"] or "不是校准概率" in t["note"]


def test_anti_hallucination_gate_refuses_banned_assertion(monkeypatch):
    pools = [_pool("m1", "2026-11-06")]
    outs = {"m1": _out("m1", "win")}
    orig = rl._bucket

    def poison(rows):
        b = orig(rows); b["note"] = "准确率提升，模型更准"; return b
    monkeypatch.setattr(rl, "_bucket", poison)
    with pytest.raises(RuntimeError):
        rl.build(pools, outs)


def test_build_never_writes_files(tmp_path):
    pools = [_pool("m1", "2026-11-06")]
    outs = {"m1": _out("m1", "win")}
    before = set(tmp_path.rglob("*"))
    d = rl.build(pools, outs)
    assert set(tmp_path.rglob("*")) == before and json.dumps(d, ensure_ascii=False)


def test_main_is_readonly_exit_zero(tmp_path):
    out = tmp_path / "rule_lab.json"
    with pytest.raises(SystemExit) as e:
        rl.main(["--out", str(out), "--samples-dir", str(tmp_path / "none"), "--ledger", str(tmp_path / "none.csv")])
    assert e.value.code == 0 and out.exists()
    assert json.loads(out.read_text(encoding="utf-8"))["joined_decided_rows"] == 0
