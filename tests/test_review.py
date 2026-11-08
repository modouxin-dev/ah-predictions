"""预测准确率复盘层回归测试：对账、结算完整性、样本不足、防幻觉、原子落盘。

全部用本地临时 fixture，不联网、不发消息、不改模型算法。
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ah import review


FIELDS = ["date", "match_id", "league", "home", "away", "kickoff_utc",
          "pick_side", "pick", "line", "odds_if_known", "confidence", "result",
          "ah_outcome", "units", "settled", "odds_format", "odds_estimated",
          "status", "final_line", "final_odds", "track", "model_id",
          "model_version", "market", "overlap"]


def row(**kw):
    base = {k: "" for k in FIELDS}
    base.update({"date": "2026-11-07", "match_id": "m1", "league": "测试联赛",
                 "home": "A", "away": "B", "kickoff_utc": "2026-11-07T12:00:00Z",
                 "pick_side": "home", "pick": "A", "line": "-0.5",
                 "odds_if_known": "0.9", "confidence": "medium",
                 "result": "1-0", "ah_outcome": "win", "units": "0.9",
                 "settled": "true", "odds_format": "hk", "odds_estimated": "false",
                 "status": "active", "track": "official", "market": "ah"})
    base.update(kw)
    return base


def write_csv(p: Path, rows: list[dict]) -> None:
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def write_stats(p: Path, rows: list[dict]) -> None:
    """用 review.recompute 生成与 CSV 严格一致的权威 stats fixture。"""
    b = review.recompute(rows)
    p.write_text(json.dumps({
        "updated_at": "2026-11-07T22:55:43Z",
        "hit_rate_definition": review.HIT_RATE_DEFINITION,
        "roi_definition": review.ROI_DEFINITION,
        "overall": b,
        "odds_estimated": {"picks": 0},
        "by_confidence": {"中": {"decided": b["decided"], "hit_rate": b["hit_rate"], "units": b["units"]}},
        "by_league": {}, "by_depth": {}, "by_role": {}, "by_line": {},
        "series": [{"date": r["date"], "kickoff_utc": r["kickoff_utc"],
                    "match_id": r["match_id"], "outcome": r["ah_outcome"],
                    "units": float(r["units"] or 0)} for r in rows
                   if r["settled"] == "true" and r["ah_outcome"] != "void"
                   and r.get("track", "official") == "official"],
    }, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def fixture(tmp_path):
    rows = [row(match_id=f"m{i}", ah_outcome="win" if i % 2 else "loss",
                units="0.9" if i % 2 else "-1.0") for i in range(1, 7)]
    c, s = tmp_path / "predictions.csv", tmp_path / "stats.json"
    write_csv(c, rows); write_stats(s, rows)
    return c, s, rows


def test_reconcile_matches(fixture):
    c, s, rows = fixture
    rec = review.reconcile(review.load_rows(c), json.loads(s.read_text(encoding="utf-8")))
    assert rec["ok"] is True and rec["status"] == "RECONCILED"
    assert rec["independent_from_csv"]["picks"] == 6


def test_reconcile_mismatch_blocks_any_hit_rate_claim(fixture, tmp_path):
    c, s, rows = fixture
    stats = json.loads(s.read_text(encoding="utf-8"))
    stats["overall"]["hit_rate"] = 0.99          # 权威值被篡改
    s.write_text(json.dumps(stats, ensure_ascii=False), encoding="utf-8")
    payload = review.build_payload(c, s, "2026-11-07", now=datetime(2026, 11, 8, 4, tzinfo=timezone.utc))
    assert payload["reconciliation"]["status"] == "RECONCILIATION_MISMATCH"
    assert payload["quotable"] is False
    assert any(a["code"] == "RECONCILIATION_MISMATCH" for a in payload["alerts"])
    md = review.render_daily("2026-11-07", payload, "test-run")
    assert "未取得" in md and "不输出任何结论数字" in md


def test_insufficient_sample_blocks_conclusions(fixture):
    c, s, rows = fixture
    payload = review.build_payload(c, s, "2026-11-07", now=datetime(2026, 11, 8, 4, tzinfo=timezone.utc))
    assert payload["sample_status"] == "INSUFFICIENT_SAMPLE"
    assert payload["quotable"] is False
    assert any(a["code"] == "INSUFFICIENT_SAMPLE" for a in payload["alerts"])
    md = review.render_daily("2026-11-07", payload, "")
    assert "未取得" in md and review.DISCLAIMER in md
    # 总样本不足时，窗口与分桶也不得泄漏命中率数字
    assert all(w["hit_rate"] is None and w["status"] == "INSUFFICIENT_SAMPLE" for w in payload["windows"])
    assert all(v["hit_rate"] == review.MISSING for items in payload["buckets"].values() for v in items.values())
    assert "25.0%" not in md


def test_void_and_experiment_rows_excluded(tmp_path):
    rows = [row(match_id="ok", ah_outcome="win", units="1.0"),
            row(match_id="void", ah_outcome="void", units="0", status="cancelled", result=""),
            row(match_id="exp", ah_outcome="win", track="experiment", units="9.0"),
            row(match_id="pending", ah_outcome="", settled="false", result="")]
    c, s = tmp_path / "predictions.csv", tmp_path / "stats.json"
    write_csv(c, rows); write_stats(s, rows)
    integ = review.settlement_integrity(rows, datetime(2026, 11, 8, 4, tzinfo=timezone.utc))
    assert integ["total_rows"] == 4
    assert integ["decided_rows"] == 1 and integ["void_rows"] == 1
    assert integ["pending_rows"] == 1
    assert review.recompute(rows)["picks"] == 2  # ok + void


def test_missing_result_and_backlog_are_surfaced(tmp_path):
    rows = [row(match_id="noresult", ah_outcome="loss", result="", units="-1.0"),
            row(match_id="old", settled="false", ah_outcome="", result="",
                kickoff_utc="2026-11-06T10:00:00Z")]
    integ = review.settlement_integrity(rows, datetime(2026, 11, 8, 4, tzinfo=timezone.utc))
    assert integ["missing_result_rows"] == 1
    assert len(integ["overdue_pending"]) == 1
    assert integ["overdue_pending"][0]["match_id"] == "old"


def test_forbidden_claims_are_rejected():
    with pytest.raises(RuntimeError, match="禁止断言词"):
        review._assert_no_claims("本期准确率提升明显")


def test_run_is_idempotent_and_atomic(fixture, tmp_path):
    c, s, rows = fixture
    out = tmp_path / "docs" / "review"
    ledger = tmp_path / "data" / "accuracy_review.json"
    import ah
    old_data = ah.DATA
    ah.DATA = tmp_path / "data"
    try:
        now = datetime(2026, 11, 8, 4, tzinfo=timezone.utc)
        review.run("2026-11-07", c, s, out, "r1", now=now)
        review.run("2026-11-07", c, s, out, "r2", now=now)   # 同日重跑
        hist = json.loads(ledger.read_text(encoding="utf-8"))
        assert len(hist["snapshots"]) == 1, "同日快照必须幂等覆盖"
        assert hist["snapshots"][0]["run_id"] == "r2"
        assert (out / "2026-11-07.md").exists()
        assert len(list(out.glob("weekly-*.md"))) == 1
        assert not list(out.glob(".*.tmp")) and not list((tmp_path / "data").glob(".*.tmp"))
    finally:
        ah.DATA = old_data


def test_check_mode_writes_nothing(fixture, tmp_path, capsys):
    c, s, rows = fixture
    out = tmp_path / "docs" / "review"
    import ah
    old_data = ah.DATA
    ah.DATA = tmp_path / "data"
    try:
        review.run("2026-11-07", c, s, out, "", check=True)
        assert not out.exists()
        assert not (tmp_path / "data" / "accuracy_review.json").exists()
        assert "REVIEW_CHECK" in capsys.readouterr().out
    finally:
        ah.DATA = old_data


def test_daily_report_fields_are_traceable(fixture):
    c, s, rows = fixture
    payload = review.build_payload(c, s, "2026-11-07", now=datetime(2026, 11, 8, 4, tzinfo=timezone.utc))
    md = review.render_daily("2026-11-07", payload, "run-1")
    for key in ("结算完整性", "与 stats.json 对账", "告警", "边界声明", "run-1"):
        assert key in md
    assert "赛果只来自" in md
