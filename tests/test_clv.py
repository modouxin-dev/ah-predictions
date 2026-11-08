"""DATA SOURCE: CLV 台账回填只写真实值，缺收盘线写未取得。"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from ah import clv


BASE_COLS = ["date", "match_id", "home", "away", "kickoff_utc", "pick_side", "line",
             "odds_if_known", "confidence", "result", "ah_outcome", "units", "settled",
             "odds_format", "odds_estimated", "status", "final_line", "final_odds",
             "track", "model_id", "model_version", "market", "overlap"]


def _write(p: Path, header, rows):
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header); w.writeheader()
        for r in rows: w.writerow(r)


def _read(p: Path):
    with p.open(encoding="utf-8", newline="") as f:
        rd = csv.DictReader(f); return list(rd.fieldnames or []), list(rd)


def _fixture(tmp_path):
    led = tmp_path / "data" / "predictions.csv"
    led.parent.mkdir(parents=True, exist_ok=True)
    _write(led, BASE_COLS, [
        {**{c: "" for c in BASE_COLS}, "date": "2026-11-08", "match_id": "m1",
         "home": "A", "away": "B", "pick_side": "away", "line": "-0.5",
         "final_line": "-0.25", "final_odds": "0.92", "settled": "true"},
        {**{c: "" for c in BASE_COLS}, "date": "2026-11-08", "match_id": "m2",
         "home": "C", "away": "D", "pick_side": "home", "line": "-1.0", "settled": "false"},
    ])
    rep = tmp_path / "reports" / "2026" / "11" / "2026-11-08.json"
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(json.dumps({"date": "2026-11-08", "picks": [
        {"match_id": "m1", "line": -0.5, "line_open": -0.25, "price_ts": "2026-11-07T20:00:00Z"},
        {"match_id": "m2", "line": -1.0, "line_open": -0.75, "price_ts": "2026-11-07T21:00:00Z"},
    ]}, ensure_ascii=False), encoding="utf-8")
    return led, tmp_path / "reports", tmp_path / "data" / "snapshots" / "odds"


def test_backfill_writes_only_real_values(tmp_path):
    led, rep, snaps = _fixture(tmp_path)
    out = clv.backfill(led, rep, snaps, apply=False)
    assert out["applied"] is False and out["rows"] == 2
    assert out["counters"]["line_open_filled"] == 2
    assert out["counters"]["price_ts_filled"] == 2
    assert out["counters"]["line_move_computed"] == 1  # 只有 m1 有 final_line
    assert out["counters"]["clv_price_computed"] == 0  # 价格型 CLV 缺收盘去水，不编造
    assert out["counters"]["open_to_report_move_computed"] == 2
    assert out["counters"]["no_close_available"] == 1  # m2 无收盘线
    # dry-run 不写盘
    header, rows = _read(led)
    assert "line_open" not in header
    assert not (led.parent / "clv_backfill.json").exists()


def test_apply_adds_columns_and_preserves_existing(tmp_path):
    led, rep, snaps = _fixture(tmp_path)
    clv.backfill(led, rep, snaps, apply=True)
    header, rows = _read(led)
    for c in clv.NEW_COLS: assert c in header
    assert len(rows) == 2 and rows[0]["match_id"] == "m1"
    assert rows[0]["line_open"] == "-0.25" and rows[0]["price_ts"] == "2026-11-07T20:00:00Z"
    assert rows[0]["line_move"] == "-0.25"   # line(-0.5) - close(-0.25)
    assert rows[0]["clv"] == clv.MISSING      # 价格型 CLV 不拿线差冒充
    assert "final_line" in rows[0]["clv_basis"]
    assert rows[0]["open_to_report_move"] == "-0.25"
    assert rows[1]["close_line"] == clv.MISSING and rows[1]["line_move"] == clv.MISSING
    assert rows[1]["clv"] == clv.MISSING
    assert rows[1]["line_open"] == "-0.75"
    fact = json.loads((led.parent / "clv_backfill.json").read_text(encoding="utf-8"))
    assert fact["applied"] is True and "不另立口径" in fact["note"]


def test_apply_is_idempotent_and_never_overwrites(tmp_path):
    led, rep, snaps = _fixture(tmp_path)
    clv.backfill(led, rep, snaps, apply=True)
    _, rows1 = _read(led)
    clv.backfill(led, rep, snaps, apply=True)
    header2, rows2 = _read(led)
    assert rows1 == rows2
    assert len([c for c in header2 if c == "line_open"]) == 1


def test_snapshot_close_only_when_view_is_learnable(tmp_path):
    led, rep, snaps = _fixture(tmp_path)
    # m2：report 阶段有一条 ah_home_line=-1.0 与报告 line 完全对上 -> 学到 sign=+1
    snaps.mkdir(parents=True, exist_ok=True)
    (snaps / "2026-11-08.csv").write_text(
        "match_id,captured_at,stage,bookmaker,ah_home_line,odds_home,odds_away,price_ts\n"
        "m2,2026-11-07T22:00:00Z,report,pinnacle,-1.0,1.90,1.92,2026-11-07T22:00:00Z\n"
        "m2,2026-11-08T13:50:00Z,close,pinnacle,-0.75,1.85,1.97,2026-11-08T13:50:00Z\n",
        encoding="utf-8")
    out = clv.backfill(led, rep, snaps, apply=True)
    assert out["counters"]["close_from_snapshot"] == 1
    _, rows = _read(led)
    r2 = next(r for r in rows if r["match_id"] == "m2")
    assert r2["close_line"] == "-0.75"
    assert r2["line_move"] == "-0.25"   # line(-1.0) - close(-0.75)
    assert r2["clv"] == clv.MISSING
    assert "stage=close" in r2["clv_basis"]


def test_ambiguous_view_is_not_guessed(tmp_path):
    led, rep, snaps = _fixture(tmp_path)
    snaps.mkdir(parents=True, exist_ok=True)
    # 同场 report 里 +1 与 -1 都能对上 -> 视角歧义，绝不猜
    (snaps / "2026-11-08.csv").write_text(
        "match_id,captured_at,stage,bookmaker,ah_home_line,odds_home,odds_away,price_ts\n"
        "m2,2026-11-07T22:00:00Z,report,pinnacle,-1.0,1.90,1.92,x\n"
        "m2,2026-11-07T22:01:00Z,report,pinnacle,1.0,1.90,1.92,x\n"
        "m2,2026-11-08T13:50:00Z,close,pinnacle,-0.75,1.85,1.97,x\n",
        encoding="utf-8")
    clv.backfill(led, rep, snaps, apply=True)
    _, rows = _read(led)
    r2 = next(r for r in rows if r["match_id"] == "m2")
    assert r2["line_move"] == clv.MISSING and r2["close_line"] == clv.MISSING


def test_no_pick_facts_does_not_invent(tmp_path):
    led, rep, snaps = _fixture(tmp_path)
    out = clv.backfill(led, tmp_path / "empty_reports", snaps, apply=False)
    assert out["counters"]["line_open_filled"] == 0
    assert out["counters"]["price_ts_filled"] == 0
    assert "不补造" in out["note"]


# ---------- ROUND 6 / A1：真实 close 快照 join + 格式归一 + 不拿线差冒充 CLV ----------

def test_align_odds_format_is_mechanical_only():
    assert clv._align_odds_format(2.0, "decimal", "hk") == 1.0
    assert clv._align_odds_format(1.0, "hk", "decimal") == 2.0
    assert clv._align_odds_format(1.9, "hk", "hk") == 1.9
    assert clv._align_odds_format(None, "decimal", "hk") is None
    assert clv._align_odds_format(2.0, "unknown", "hk") is None   # 未知组合不猜测


def test_close_snapshot_join_writes_line_move_not_fake_clv(tmp_path):
    """有真实 close 行 -> close_line/line_move 有值；盘口不同 -> 价格型 CLV 必须「未取得」。"""
    led, rep, snaps = _fixture(tmp_path)
    snaps.mkdir(parents=True, exist_ok=True)
    hdr = ["match_id", "captured_at", "stage", "bookmaker", "ah_home_line",
           "odds_home", "odds_away", "odds_format", "source"]
    (snaps / "2026-11-08.csv").write_text(
        ",".join(hdr) + "\n"
        + "m2,2026-11-08T14:00:00Z,report,pinnacle,-1.0,1.90,1.90,decimal,oddspapi\n"
        + "m2,2026-11-08T15:00:00Z,close,pinnacle,-0.75,1.80,2.00,decimal,oddspapi\n",
        encoding="utf-8")
    out = clv.backfill(led, rep, snaps, apply=True)
    assert out["snapshot_close_matches"] == 1
    assert out["counters"]["close_from_snapshot"] == 1
    assert out["counters"]["clv_price_computed"] == 0
    _, rows = _read(led)
    m2 = next(r for r in rows if r["match_id"] == "m2")
    assert m2["close_line"] == "-0.75" and m2["line_move"] == "-0.25"
    assert m2["clv"] == "未取得"
    assert "≠" in m2["clv_basis"] and "不当作 CLV" in m2["clv_basis"]
    # 收盘水位由 decimal 机械归一到台账 hk，不混用格式
    assert m2["close_odds"] == "0.8"      # 1.80 decimal -> 0.80 hk
    jf = json.loads((led.parent / "clv_join_facts.json").read_text(encoding="utf-8"))
    assert jf["joined_unique_match_ids"] == 1
    assert jf["close_snapshot_rows_in_repo"] == 1


def test_price_clv_only_when_close_line_equals_bet_line(tmp_path):
    """同一口径闭环：盘口相同 + 两侧水位齐全才允许 clv 有值。"""
    from ah.audit.experiment_track import clv as price_clv
    probe = {"market": "ah", "line": -0.5, "close_line": -0.5,
             "close_odds": 0.9, "close_odds_opp": 0.9,
             "odds_if_known": 0.95, "odds_format": "hk"}
    assert price_clv(probe) is not None
    probe["close_line"] = -0.25
    assert price_clv(probe) is None


def test_join_facts_reasons_cover_every_ledger_row(tmp_path):
    led, rep, snaps = _fixture(tmp_path)
    clv.backfill(led, rep, snaps, apply=True)
    jf = json.loads((led.parent / "clv_join_facts.json").read_text(encoding="utf-8"))
    assert sum(jf["price_clv_status_by_row"].values()) == jf["ledger_rows"] == 2


def test_final_line_odds_not_reformatted_by_snapshot(tmp_path):
    """台账 final_odds 本来就是台账格式，不能被快照的 decimal 格式误减 1。"""
    led, rep, snaps = _fixture(tmp_path)
    snaps.mkdir(parents=True, exist_ok=True)
    hdr = ["match_id", "captured_at", "stage", "bookmaker", "ah_home_line",
           "odds_home", "odds_away", "odds_format", "source"]
    (snaps / "2026-11-08.csv").write_text(
        ",".join(hdr) + "\n"
        + "m1,2026-11-08T15:00:00Z,close,pinnacle,-0.25,1.80,2.00,decimal,oddspapi\n",
        encoding="utf-8")
    clv.backfill(led, rep, snaps, apply=True)
    _, rows = _read(led)
    m1 = next(r for r in rows if r["match_id"] == "m1")
    assert m1["close_odds"] == "0.92"          # 原样保留，不变成 -0.08
    assert "final_line" in m1["clv_basis"]


def test_zero_hk_water_is_not_treated_as_missing():
    """decimal 1.0 -> hk 0.0 是合法值，不能被 `or MISSING` 吞掉。"""
    assert clv._align_odds_format(1.0, "decimal", "hk") == 0.0
