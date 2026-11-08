"""数据质量旁路：只读、不写回台账/报告、不补造、不推断。"""
from __future__ import annotations

import json
from pathlib import Path

from ah import data_quality as dq


def _write(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _report(root: Path, date: str = "2026-11-08"):
    p = dq.report_path(date, root)
    _write(p, {
        "date": date, "generated_at": "2026-11-07T23:14:23Z",
        "picks": [{"source": "oddspapi", "bookmaker": "pinnacle", "match_id": "m1"},
                  {"source": "oddspapi", "bookmaker": "pinnacle", "match_id": "m2"}],
        "collection_errors": [],
        "collection_notes": ["API-Football 未启用"],
        "not_covered": ("筛查上限 32 场；窗口内共发现 51 场。盘口核验：no_quotes 1场；"
                        "verified:0<1 6场。"),
        "feature_evidence": {"by_match": {"m1": {
            "mode": "observation",
            "factors": {"a": {"value": 1.0}, "b": {"value": None}, "c": {"value": "未取得"}}}}},
    })
    return p


def _ledger(root: Path):
    p = root / "data" / "predictions.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "date,match_id,line,odds_if_known,final_line,final_odds,settled,ah_outcome,odds_estimated\n"
        "2026-11-08,m1,-0.5,0.95,,,true,win,false\n"
        "2026-11-08,m2,-0.25,1.02,-0.5,0.98,true,loss,false\n",
        encoding="utf-8")
    return p


def _snapshots(root: Path):
    d = root / "data" / "snapshots" / "odds"
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-11-08.csv").write_text("match_id,line,odds\nm1,-0.5,0.95\n", encoding="utf-8")


def test_full_build_records_facts_without_inference(tmp_path):
    _report(tmp_path); _ledger(tmp_path); _snapshots(tmp_path)
    out = dq.build("2026-11-08", root=tmp_path)
    assert out["report_parsed"] is True
    assert out["candidate_coverage"]["not_covered_count"] == 19
    assert out["verification_facts"]["items"]["no_quotes"] == 1
    assert out["verification_facts"]["items"]["verified:0<1"] == 6
    assert out["clv_field_coverage"]["field_filled"]["line"] == 2
    assert out["clv_field_coverage"]["field_filled"]["line_open"] == 0
    assert out["clv_field_coverage"]["joinable_rows"] == 1
    assert out["source_concentration"]["distinct_sources"] == 1
    assert out["factor_gaps"]["factors_value_missing"] == 2
    assert out["sample_precision"]["decided"] == 2
    assert 0 < out["sample_precision"]["wilson_95_width"] <= 1
    assert "不构成" in out["sample_precision"]["note"]
    all_text = json.dumps(out, ensure_ascii=False)
    assert "准确率提升" not in all_text and "已恢复" not in all_text


def test_does_not_write_back_ledger_or_report(tmp_path):
    rp = _report(tmp_path); lp = _ledger(tmp_path); _snapshots(tmp_path)
    before_r, before_l = rp.read_bytes(), lp.read_bytes()
    dq.build("2026-11-08", root=tmp_path)
    assert rp.read_bytes() == before_r and lp.read_bytes() == before_l


def test_missing_report_is_reported_not_invented(tmp_path):
    _ledger(tmp_path)
    out = dq.build("2026-11-09", root=tmp_path)
    assert out["report_parsed"] is False
    assert out["source_concentration"] == {"status": "REPORT_MISSING"}
    assert any(a["code"] == "REPORT_MISSING" for a in out["alerts"])
    assert "不补造" in out["alerts"][0]["message"]


def test_zero_pick_lists_facts_not_inference(tmp_path):
    p = dq.report_path("2026-11-08", tmp_path)
    _write(p, {"date": "2026-11-08", "picks": [], "collection_errors": [],
               "collection_notes": ["API-Football 未启用"], "not_covered": "无"})
    _ledger(tmp_path)
    out = dq.build("2026-11-08", root=tmp_path)
    z = out["zero_pick_reasons"]
    assert z["is_zero_pick"] is True and z["collection_notes"] == ["API-Football 未启用"]
    assert "不推断" in z["note"]


def test_single_source_alert_is_info_only(tmp_path):
    _report(tmp_path); _ledger(tmp_path); _snapshots(tmp_path)
    out = dq.build("2026-11-08", root=tmp_path)
    a = next(x for x in out["alerts"] if x["code"] == "SINGLE_SOURCE_IN_REPORT")
    assert a["severity"] == "info" and "不代表数据不足" in a["message"]


def test_main_is_readonly_and_exit_zero(tmp_path):
    _report(tmp_path); _ledger(tmp_path); _snapshots(tmp_path)
    import pytest
    out = tmp_path / "dq.json"
    with pytest.raises(SystemExit) as e:
        dq.main(["--date", "2026-11-08", "--out", str(out),
                 "--ledger", str(tmp_path / "data" / "predictions.csv"),
                 "--snapshots-dir", str(tmp_path / "data" / "snapshots" / "odds")])
    assert e.value.code == 0 and out.exists()
