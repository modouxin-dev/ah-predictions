"""模型实验盘：ou/1x2 结算、v3 列兼容、正式统计隔离、每日上限、重叠、CLV/Brier、防事后修正。"""
import csv
import json
import logging
import shutil
from pathlib import Path

import pytest

from ah.audit import experiment_track as ex
from ah.migrate_csv import migrate_csv
from ah.settle import LEDGER_FIELDS, profit, read_ledger, settle_1x2, settle_market, settle_ou, settle_row
from ah.stats import compute, compute_experiment


# ---------- 大小球（手算） ----------

@pytest.mark.parametrize("total,line,side,exp", [
    # 2.25 = 2 / 2.5 各半
    (3, 2.25, "over", "win"), (2, 2.25, "over", "half_loss"), (1, 2.25, "over", "loss"),
    (2, 2.25, "under", "half_win"), (3, 2.25, "under", "loss"), (1, 2.25, "under", "win"),
    # 2.5 只有输赢
    (3, 2.5, "over", "win"), (2, 2.5, "over", "loss"), (2, 2.5, "under", "win"), (3, 2.5, "under", "loss"),
    # 2.75 = 2.5 / 3 各半
    (3, 2.75, "over", "half_win"), (4, 2.75, "over", "win"), (2, 2.75, "over", "loss"),
    (3, 2.75, "under", "half_loss"), (2, 2.75, "under", "win"), (4, 2.75, "under", "loss"),
    # 3 整数线可走盘
    (3, 3, "over", "push"), (4, 3, "over", "win"), (2, 3, "over", "loss"),
    (3, 3, "under", "push"), (2, 3, "under", "win"), (4, 3, "under", "loss"),
])
def test_settle_ou(total, line, side, exp):
    assert settle_ou(total, line, side) == exp


@pytest.mark.parametrize("hg,ag,side,exp", [
    (2, 1, "home", "win"), (2, 1, "draw", "loss"), (2, 1, "away", "loss"),
    (1, 1, "home", "loss"), (1, 1, "draw", "win"), (1, 1, "away", "loss"),
    (0, 2, "home", "loss"), (0, 2, "draw", "loss"), (0, 2, "away", "win"),
])
def test_settle_1x2(hg, ag, side, exp):
    assert settle_1x2(hg, ag, side) == exp


def test_settle_market_dispatch_and_errors():
    assert settle_market("ah", 1, 0, "home", -0.75) == "half_win"
    assert settle_market("OU", 2, 1, "over", 2.75) == "half_win"
    assert settle_market("1X2", 0, 0, "draw") == "win"
    for args in [("ou", 1, 1, "home", 2.5), ("1x2", 1, 0, "over"), ("ah", 1, 0, "draw", 0)]:
        with pytest.raises(ValueError):
            settle_market(*args)


def test_profit_units_per_market():
    assert profit("win", 2.10, odds_format="decimal") == 1.1         # 1x2 欧赔
    assert profit("half_win", 0.95, odds_format="hk") == 0.475       # ou 港盘
    assert profit("half_loss", 1.95, odds_format="decimal") == -0.5


def test_settle_row_ou_and_1x2_default_decimal():
    r = {"result": "2-1", "pick_side": "over", "line": "2.75", "odds_if_known": "0.95", "settled": "false",
         "market": "ou", "track": "experiment"}
    settle_row(r)
    assert r["ah_outcome"] == "half_win" and float(r["units"]) == 0.475
    r = {"result": "1-1", "pick_side": "draw", "line": "", "odds_if_known": "3.4", "settled": "false",
         "market": "1x2", "track": "experiment"}  # 无 odds_format -> 1x2 默认 decimal
    settle_row(r)
    assert r["ah_outcome"] == "win" and float(r["units"]) == 2.4


def test_experiment_row_without_odds_not_estimated():
    r = {"result": "2-0", "pick_side": "home", "line": "-0.5", "odds_if_known": "", "settled": "false",
         "market": "ah", "track": "experiment"}
    settle_row(r)
    assert r["ah_outcome"] == "win" and r["units"] == "" and r["odds_estimated"] == "false"


# ---------- v3 列与隔离 ----------

def test_v3_defaults_and_migrate_idempotent(tmp_path):
    p = tmp_path / "p.csv"
    shutil.copy("data/predictions.csv", p)
    rows = read_ledger(p)
    assert all(r["track"] == "official" and r["market"] == "ah" and r["model_id"] == "" for r in rows)
    migrate_csv(p, Path("reports"))
    before = p.read_bytes()
    assert migrate_csv(p, Path("reports")) == [] and p.read_bytes() == before
    assert p.read_text(encoding="utf-8").splitlines()[0].split(",") == LEDGER_FIELDS


def test_official_stats_ignore_experiment_rows():
    base = [dict(r) for r in csv.DictReader(open("data/predictions.csv", encoding="utf-8"))]
    leak = {**base[0], "match_id": "x", "track": "experiment", "model_id": "A", "status": "active",
            "ah_outcome": "win", "units": "0.9", "settled": "true", "confidence": "high"}
    a, b = compute(base), compute(base + [leak])
    a.pop("updated_at"), b.pop("updated_at")
    assert a == b


def test_records_sync_skips_experiment_rows(tmp_path):
    from ah import records
    p = tmp_path / "p.csv"
    shutil.copy("data/predictions.csv", p)
    rows = read_ledger(p)
    exp = {**rows[1], "track": "experiment", "model_id": "A", "confidence": "low"}
    from ah.settle import write_ledger
    write_ledger(p, [exp] + rows)
    r = json.loads(Path("reports/2026/11/2026-11-04.json").read_text())
    records.set_prematch(r, "titan007-3073152", "调整", "x", new_line=-0.5, new_odds=0.95,
                         pol={"prematch_cancel_on_confidence_drop": True, "effective_from": "2026-11-05"})
    records.sync_csv(r, p)
    got = read_ledger(p)
    e = next(x for x in got if x["track"] == "experiment")
    o = next(x for x in got if x["track"] == "official" and x["match_id"] == "titan007-3073152")
    assert e["line"] == "-0.75" and e["confidence"] == "low" and o["line"] == "-0.5"


# ---------- 实验盘记录 ----------

def pick(model, market, mid, side, line, odds, date="2026-11-06", gen="2026-11-06T02:00:00Z",
         ko="2026-11-06T19:00:00Z", **kw):
    return {"date": date, "match_id": mid, "league": "L", "home": "H", "away": "A", "kickoff_utc": ko,
            "pick_side": side, "pick": side, "line": line, "odds_if_known": odds, "confidence": "",
            "model_id": model, "model_version": "v0", "market": market, "generated_at": gen, **kw}


def test_daily_cap_rejects_fourth(tmp_path, caplog):
    f = tmp_path / "e.csv"
    picks = [pick("A", "ou", f"m{i}", "over", "2.5", "0.9", gen=f"2026-11-06T0{i}:00:00Z") for i in range(1, 5)]
    picks.append(pick("A", "ah", "m4", "home", "-0.5", "0.9"))      # 不同玩法单独计数
    picks.append(pick("B", "ou", "m4", "under", "2.5", "0.95"))     # 不同模型单独计数
    with caplog.at_level(logging.WARNING, logger="ah.experiment"):
        keep, rej = ex.add_picks(picks, f, official_path=tmp_path / "none.csv")
    assert len(keep) == 5 and len(rej) == 1 and "每日上限" in rej[0]["reason"] and "m4" in rej[0]["key"]
    assert any("每日上限" in m for m in caplog.messages)
    # 再追加一场同日 A/ou 仍被拒，已有行不被改写
    before = [{k: r[k] for k in ex.PICK_FIELDS} for r in ex.read_experiment(f)]
    keep2, rej2 = ex.add_picks([pick("A", "ou", "m9", "over", "3", "0.9", gen="2026-11-06T09:00:00Z")], f,
                               official_path=tmp_path / "none.csv")
    assert keep2 == [] and len(rej2) == 1
    assert [{k: r[k] for k in ex.PICK_FIELDS} for r in ex.read_experiment(f)] == before


@pytest.mark.parametrize("bad,reason", [
    ({"generated_at": "2026-11-06T19:00:00Z"}, "不早于开球"), ({"generated_at": ""}, "generated_at"),
    ({"odds_if_known": ""}, "不补值"), ({"model_id": ""}, "model_id"), ({"pick_side": "home"}, "pick_side"),
    ({"line": "2.3"}, "非法盘口"), ({"model_p": "1.2"}, "model_p"), ({"market": "btts"}, "market"),
])
def test_validation_rejects(bad, reason):
    r = {**pick("A", "ou", "m1", "over", "2.5", "0.9"), "track": "experiment", **bad}
    acc, rej = ex.validate([r])
    assert acc == [] and reason in rej[0]["reason"]


def test_duplicate_rejected():
    rows = [{**pick("A", "ah", "m1", "home", "-0.5", "0.9"), "track": "experiment"} for _ in range(2)]
    acc, rej = ex.validate(rows)
    assert len(acc) == 1 and "重复" in rej[0]["reason"]


def test_overlap_models_and_official(tmp_path):
    off = tmp_path / "p.csv"
    shutil.copy("data/predictions.csv", off)  # 波尔图 titan007-3073152 正式推荐 home
    f = tmp_path / "e.csv"
    ex.add_picks([pick("A", "ah", "titan007-3073152", "home", "-0.75", "1.06"),
                  pick("B", "ah", "titan007-3073152", "away", "0.75", "0.82"),
                  pick("C", "ah", "titan007-3073152", "home", "-0.5", "0.9"),
                  pick("C", "ou", "titan007-3073152", "over", "2.5", "0.9")], f, official_path=off)
    rows = {(r["model_id"], r["market"]): r for r in ex.read_experiment(f)}
    assert rows[("A", "ah")]["overlap"] == "B:opposite;C:same;official:same"
    assert rows[("B", "ah")]["overlap"] == "A:opposite;C:opposite;official:opposite"
    assert rows[("C", "ou")]["overlap"] == ""
    s = compute_experiment(ex.read_experiment(f), read_ledger(off))
    assert s["by_model_market"]["A"]["ah"]["overlap_official"] == 1 and s["by_model_market"]["A"]["ah"]["overlap_models"] == 1


def test_settle_experiment_only_touches_result_columns(tmp_path):
    f = tmp_path / "e.csv"
    ex.add_picks([pick("A", "ou", "m1", "over", "2.75", "0.95"), pick("A", "1x2", "m1", "home", "", "2.1")], f,
                 official_path=tmp_path / "none.csv")
    rows = ex.read_experiment(f)
    for r in rows:
        r["result"] = "2-1"
    ex.write_experiment(f, rows)
    assert ex.settle_experiment(f) == 2
    got = {r["market"]: r for r in ex.read_experiment(f)}
    assert got["ou"]["ah_outcome"] == "half_win" and float(got["ou"]["units"]) == 0.475
    assert got["1x2"]["ah_outcome"] == "win" and float(got["1x2"]["units"]) == 1.1
    assert got["1x2"]["odds_format"] == "decimal"


# ---------- 指标 ----------

def test_clv_and_line_move():
    r = {"market": "ah", "pick_side": "home", "line": "-0.75", "odds_if_known": "1.0", "odds_format": "hk",
         "close_line": "-0.75", "close_odds": "0.9", "close_odds_opp": "0.9"}
    assert ex.clv(r) == pytest.approx(2.0 * 0.5 - 1)                 # 无水 0.5，下注 2.0 -> 0
    r2 = {**r, "odds_if_known": "1.1"}
    assert ex.clv(r2) == pytest.approx(2.1 * 0.5 - 1)
    assert ex.clv({**r, "close_line": "-1"}) is None                  # 盘口变了不算价格 CLV
    assert ex.line_move({**r, "close_line": "-1"}) == 0.25            # 拿到 -0.75，收盘 -1 -> 更好
    assert ex.line_move({"market": "ou", "pick_side": "over", "line": "2.5", "close_line": "2.75"}) == 0.25
    assert ex.line_move({"market": "ou", "pick_side": "under", "line": "2.5", "close_line": "2.75"}) == -0.25
    x = {"market": "1x2", "odds_if_known": "2.2", "odds_format": "decimal", "close_fair_p": "0.5"}
    assert ex.clv(x) == pytest.approx(0.1)
    y = {"market": "1x2", "odds_if_known": "2.0", "odds_format": "decimal", "close_odds": "2.0",
         "close_odds_opp": "4.0;4.0"}
    assert ex.clv(y) == pytest.approx(2.0 * 0.5 - 1)


def test_brier_y_mapping():
    assert [ex.outcome_y({"market": "ou", "ah_outcome": o}) for o in ("win", "half_win", "push", "half_loss", "loss")] \
        == [1.0, 0.75, 0.5, 0.25, 0.0]
    assert ex.outcome_y({"market": "1x2", "ah_outcome": "win"}) == 1.0
    assert ex.outcome_y({"market": "ah", "ah_outcome": "void"}) is None


def srow(model, market, out, units, model_p="", market_p="", **kw):
    return {**pick(model, market, kw.pop("mid", "m1"), kw.pop("side", "over" if market == "ou" else "home"),
                   "" if market == "1x2" else "2.5" if market == "ou" else "-0.5", "0.9" if market != "1x2" else "2.0"),
            "track": "experiment", "ah_outcome": out, "units": units, "settled": "true", "result": "1-0",
            "model_p": model_p, "market_p": market_p, **kw}


def test_compute_experiment_by_model_market():
    rows = [srow("A", "ou", "win", "0.9", "0.6", "0.5", mid="m1", close_line="2.5", close_odds="0.9", close_odds_opp="0.9"),
            srow("A", "ou", "loss", "-1", "0.6", "0.5", mid="m2"),
            srow("A", "1x2", "win", "1.0", "0.55", "0.5", mid="m3"),
            {**srow("B", "ah", "", "", mid="m4"), "settled": "false"}]
    s = compute_experiment(rows)
    a = s["by_model_market"]["A"]["ou"]
    assert a["picks"] == 2 and a["decided"] == 2 and a["hit_rate"] == 0.5 and a["units"] == -0.1 and a["roi"] == -0.05
    assert a["brier"] == pytest.approx(((0.6 - 1) ** 2 + 0.6 ** 2) / 2, abs=1e-4)
    assert a["market_brier"] == 0.25 and a["brier_skill"] == pytest.approx(1 - 0.26 / 0.25, abs=1e-4)
    assert a["clv_n"] == 1 and a["clv_mean"] == pytest.approx(1.9 * 0.5 - 1) and a["clv_ci95"] is None
    assert a["max_drawdown"] == 1.0 and a["significant"] is False
    one = s["by_model_market"]["A"]["1x2"]
    assert one["brier"] == pytest.approx(0.2025) and one["clv_mean"] is None
    b = s["by_model_market"]["B"]["ah"]
    assert b["pending"] == 1 and b["hit_rate"] is None and b["roi"] is None and b["brier"] is None and b["units"] is None
    json.dumps(s)  # 可序列化


def test_significant_needs_min_sample():
    rows = [srow("A", "ah", "win", "0.9", mid=f"m{i}", date=f"2026-12-{1 + i // 3:02d}",
                 gen=f"2026-12-{1 + i // 3:02d}T00:00:00Z", kickoff_utc=f"2026-12-{1 + i // 3:02d}T18:00:00Z",
                 odds_if_known="1.0", close_line="-0.5", close_odds="0.8", close_odds_opp="1.0") for i in range(90)]
    s = compute_experiment(rows)["by_model_market"]["A"]["ah"]
    assert s["clv_n"] == 90 and s["clv_mean"] > 0 and s["significant"] is False


def test_rejected_listed_not_counted():
    rows = [srow("A", "ah", "win", "0.9", mid=f"m{i}", gen=f"2026-11-06T0{i}:00:00Z") for i in range(4)]
    s = compute_experiment(rows)
    assert s["by_model_market"]["A"]["ah"]["picks"] == 3 and len(s["rejected"]) == 1


def test_repo_experiment_files_exist_and_empty():
    assert ex.read_experiment(Path("data/experiment_picks.csv")) == []
    hdr = Path("data/experiment_picks.csv").read_text(encoding="utf-8").splitlines()[0].split(",")
    assert hdr == ex.EXPERIMENT_FIELDS
    s = json.loads(Path("data/experiment_stats.json").read_text())
    assert s["track"] == "experiment" and s["by_model_market"] == {}
