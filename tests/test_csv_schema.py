"""predictions.csv v2：odds_format / odds_estimated / status / final_line / final_odds（向后兼容 v1 15 列）。"""
import csv
import json
import logging
import shutil
from pathlib import Path

import pytest

from ah import records
from ah.migrate_csv import final_from_pick, migrate_csv, parse_current_line
from ah.render_report import append_ledger
from ah.settle import (LEDGER_FIELDS, LEGACY_FIELDS, check_odds, norm_odds_format, read_ledger, settle_csv,
                       settle_row, write_ledger)
from ah.stats import compute

V1_HDR = ",".join(LEGACY_FIELDS) + "\n"
KASHIMA_V1 = ("2026-11-04,titan007-3068888,ACL,武里南联 Buriram United,鹿岛鹿角 Kashima Antlers,2026-11-04T12:15:00Z,"
              "away,鹿岛鹿角,-0.75,0.78,cancelled,,void,0,true\n")
PORTO_V1 = ("2026-11-04,titan007-3073152,UCL,波尔图 Porto,那不勒斯 Napoli,2026-11-04T20:00:00Z,"
            "home,波尔图,-0.75,1.06,medium,,,,false\n")
R04 = json.loads(Path("reports/2026/11/2026-11-04.json").read_text())


def v1(tmp_path, *lines):
    p = tmp_path / "predictions.csv"
    p.write_text(V1_HDR + "".join(lines), encoding="utf-8")
    return p


def rows_of(p):
    return {r["match_id"]: r for r in csv.DictReader(p.open(encoding="utf-8"))}


# ---------- 老格式兼容 ----------

def test_legacy_read_defaults(tmp_path):
    rows = {r["match_id"]: r for r in read_ledger(v1(tmp_path, KASHIMA_V1, PORTO_V1))}
    k, p = rows["titan007-3068888"], rows["titan007-3073152"]
    assert k["odds_format"] == "hk" and k["status"] == "cancelled" and k["odds_estimated"] == "false"
    assert p["status"] == "active" and p["final_line"] == "" and p["final_odds"] == ""


def test_legacy_settle_csv_upgrades_header_and_keeps_values(tmp_path):
    p = v1(tmp_path, KASHIMA_V1, PORTO_V1.replace(",medium,,,,false", ",medium,2-0,,,false"))
    assert settle_csv(p) == 1
    hdr = p.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert hdr == LEDGER_FIELDS
    r = rows_of(p)
    assert r["titan007-3073152"]["ah_outcome"] == "win" and float(r["titan007-3073152"]["units"]) == 1.06
    assert r["titan007-3068888"]["ah_outcome"] == "void" and r["titan007-3068888"]["status"] == "cancelled"


def test_unknown_extra_columns_preserved(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text(V1_HDR.strip() + ",note\n" + PORTO_V1.strip() + ",hello\n", encoding="utf-8")
    write_ledger(p, read_ledger(p))
    assert rows_of(p)["titan007-3073152"]["note"] == "hello"


def test_stats_accepts_legacy_rows():
    row = {"league": "ACL", "confidence": "cancelled", "line": "-0.75", "ah_outcome": "void", "units": "0", "settled": "true"}
    s = compute([row])
    assert s["by_confidence"]["取消/其他"]["cancelled"] == 1 and s["overall"]["decided"] == 0
    assert "status" not in row  # compute 不修改输入


# ---------- odds_format 两种结算 ----------

@pytest.mark.parametrize("fmt,odds,score,exp_out,exp_units", [
    ("hk", "0.92", "2-0", "win", 0.92), ("hk", "0.92", "1-0", "half_win", 0.46),
    ("decimal", "1.92", "2-0", "win", 0.92), ("decimal", "1.92", "1-0", "half_win", 0.46),
    ("decimal", "1.92", "0-0", "loss", -1.0), ("hk", "0.92", "0-0", "loss", -1.0),
])
def test_settle_by_odds_format(fmt, odds, score, exp_out, exp_units):
    r = {"result": score, "pick_side": "home", "line": "-0.75", "odds_if_known": odds, "settled": "false",
         "odds_format": fmt}
    settle_row(r)
    assert r["ah_outcome"] == exp_out and float(r["units"]) == exp_units and r["odds_estimated"] == "false"


def test_norm_odds_format():
    assert norm_odds_format("") == norm_odds_format(None) == "hk"
    assert norm_odds_format(R04["odds_format"]) == "hk"
    assert norm_odds_format("Decimal") == norm_odds_format("欧赔") == "decimal"
    with pytest.raises(ValueError):
        norm_odds_format("american")


# ---------- 范围报警 ----------

@pytest.mark.parametrize("odds,fmt,bad", [("1.92", "hk", True), ("1.5", "hk", True), ("1.49", "hk", False),
                                          ("0.78", "hk", False), ("0", "hk", True), ("1.06", "decimal", True),
                                          ("1.19", "decimal", True), ("1.2", "decimal", False), ("", "hk", False)])
def test_check_odds(odds, fmt, bad):
    assert bool(check_odds(odds, fmt)) is bad


def test_settle_warns_on_suspicious_hk_odds(caplog):
    r = {"match_id": "m1", "result": "2-0", "pick_side": "home", "line": "-0.5", "odds_if_known": "1.92",
         "settled": "false"}
    with caplog.at_level(logging.WARNING, logger="ah.settle"):
        settle_row(r)
    assert any("m1" in m and "1.92" in m for m in caplog.messages)
    assert r["ah_outcome"] == "win"  # 只报警，不拦截


def test_settle_no_warning_on_normal_odds(caplog):
    r = {"result": "2-0", "pick_side": "home", "line": "-0.5", "odds_if_known": "0.92", "settled": "false"}
    with caplog.at_level(logging.WARNING, logger="ah.settle"):
        settle_row(r)
    assert not caplog.records


# ---------- odds_estimated ----------

def test_odds_estimated_flag_and_stats(tmp_path):
    p = v1(tmp_path, PORTO_V1.replace(",1.06,medium,,,,false", ",,high,1-0,,,false"),
           PORTO_V1.replace("titan007-3073152", "m2").replace(",medium,,,,false", ",high,0-1,,,false"))
    assert read_ledger(p)[0]["odds_estimated"] == "true"
    settle_csv(p)
    r = rows_of(p)
    assert r["titan007-3073152"]["odds_estimated"] == "true" and float(r["titan007-3073152"]["units"]) == 0.45
    assert r["m2"]["odds_estimated"] == "false" and float(r["m2"]["units"]) == -1
    s = compute(read_ledger(p))
    assert s["odds_estimated"]["picks"] == 1 and s["odds_estimated"]["units"] == 0.45
    assert s["odds_estimated"]["match_ids"] == ["titan007-3073152"]
    assert s["overall"]["odds_estimated"] == 1 and s["by_confidence"]["高"]["odds_estimated"] == 1
    assert [x["odds_estimated"] for x in s["series"]] == [True, False]


# ---------- status / 原信心分层统计 ----------

def srow(conf, status, out, units, line="-0.5"):
    return {"league": "L", "confidence": conf, "status": status, "line": line, "ah_outcome": out, "units": units,
            "settled": "true"}


def test_status_cancelled_counted_under_original_confidence():
    s = compute([srow("medium", "cancelled", "void", "0"), srow("medium", "active", "win", "0.9"),
                 srow("high", "active", "loss", "-1"), srow("high", "void", "void", "0")])
    m, h = s["by_confidence"]["中"], s["by_confidence"]["高"]
    assert m["picks"] == 2 and m["cancelled"] == 1 and m["decided"] == 1 and m["hit_rate"] == 1.0
    assert h["picks"] == 2 and h["cancelled"] == 0 and h["void"] == 1 and h["decided"] == 1
    assert s["overall"]["cancelled"] == 1 and s["overall"]["decided"] == 2 and "取消/其他" not in s["by_confidence"]


def test_cancelled_row_never_counts_even_if_outcome_stale():
    s = compute([srow("high", "cancelled", "win", "0.9")])
    assert s["overall"]["decided"] == 0 and s["overall"]["units"] == 0 and s["by_confidence"]["高"]["cancelled"] == 1
    assert s["series"] == []


def test_settle_row_cancelled_status_voids():
    r = {"result": "3-0", "pick_side": "home", "line": "-0.5", "odds_if_known": "0.9", "settled": "false",
         "status": "cancelled", "confidence": "high"}
    settle_row(r)
    assert r["ah_outcome"] == "void" and r["units"] == "0" and r["confidence"] == "high"


def test_sync_csv_keeps_original_confidence(tmp_path):
    p = v1(tmp_path, KASHIMA_V1.replace(",cancelled,,void,0,true", ",medium,,,,false"), PORTO_V1)
    records.sync_csv(json.loads(json.dumps(R04)), p)
    k = rows_of(p)["titan007-3068888"]
    assert k["confidence"] == "medium" and k["status"] == "cancelled" and k["ah_outcome"] == "void"
    assert k["final_line"] == "-0.25" and k["final_odds"] == "0.92"


# ---------- final_line / final_odds ----------

def test_parse_current_line_perspective():
    k = records.find_pick(R04, "titan007-3068888")
    assert parse_current_line("鹿岛 -0.25（武里南受让平半）", k) == -0.25
    assert parse_current_line("武里南 +0.25", k) == -0.25          # 对手视角 -> 取反
    assert parse_current_line(-0.5, k) == -0.5
    assert parse_current_line("未知队 -0.25", k) is None and parse_current_line("", k) is None
    assert final_from_pick(k) == (-0.25, 0.92)


def test_final_line_on_adjust_roundtrip(tmp_path):
    p = tmp_path / "p.csv"
    shutil.copy("data/predictions.csv", p)
    r = json.loads(json.dumps(R04))
    pol = {"min_confidence_to_publish": "高", "prematch_cancel_on_confidence_drop": True, "effective_from": "2026-11-05"}
    records.set_prematch(r, "titan007-3073152", "调整", "降盘", new_line=-0.5, new_odds=0.95, pol=pol)
    records.sync_csv(r, p)
    row = rows_of(p)["titan007-3073152"]
    assert row["final_line"] == "-0.5" and row["final_odds"] == "0.95" and row["status"] == "active"
    again = {x["match_id"]: x for x in read_ledger(p)}["titan007-3073152"]
    assert again["final_line"] == "-0.5"


# ---------- 迁移（幂等）与写入方 ----------

def test_migrate_idempotent_and_backfills_kashima(tmp_path):
    p = v1(tmp_path, KASHIMA_V1, PORTO_V1)
    assert migrate_csv(p, Path("reports"), dry_run=True)
    assert p.read_text(encoding="utf-8").startswith(V1_HDR)  # dry run 不写
    ch = migrate_csv(p, Path("reports"))
    assert any("confidence cancelled -> medium" in c for c in ch)
    k = rows_of(p)["titan007-3068888"]
    assert (k["confidence"], k["status"], k["final_line"], k["final_odds"], k["odds_format"]) == \
        ("medium", "cancelled", "-0.25", "0.92", "hk")
    before = p.read_bytes()
    assert migrate_csv(p, Path("reports")) == [] and p.read_bytes() == before


def test_repo_csv_is_migrated():
    k = {r["match_id"]: r for r in csv.DictReader(open("data/predictions.csv", encoding="utf-8"))}["titan007-3068888"]
    assert k["confidence"] == "medium" and k["status"] == "cancelled" and k["final_line"] == "-0.25"


def test_append_ledger_writes_new_columns(tmp_path):
    p = v1(tmp_path, KASHIMA_V1)
    rep = {"date": "2026-11-09", "odds_format": "HK (港盘水位)", "picks": [
        {"match_id": "x1", "league": "L", "home": "A", "away": "B", "kickoff_utc": "2026-11-09T12:00:00Z",
         "pick_side": "home", "line": -0.5, "odds": 0.88, "confidence": "high"}]}
    assert append_ledger(rep, p) == 1
    r = rows_of(p)
    assert list(r["x1"].keys()) == LEDGER_FIELDS
    assert (r["x1"]["odds_format"], r["x1"]["odds_estimated"], r["x1"]["status"]) == ("hk", "false", "active")
    assert r["titan007-3068888"]["status"] == "cancelled"  # 旧行未丢
    assert append_ledger(rep, p) == 0
