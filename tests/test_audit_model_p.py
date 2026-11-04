import csv

import pytest

from ah.audit import model_p as mp
from ah.audit import paper_trades as pt
from test_audit_paper_trades import NOW, by, env  # noqa: F401

HDR = ["date", "match_id", "kickoff_utc", "model_version", "baseline_label", "generated_at", "decision", "note",
       "valid_prekickoff", "pick_side", "line", "home_line", "p_win", "p_half_win", "p_push", "p_half_loss",
       "p_loss", "model_p", "pick_odds_pinnacle", "captured_at"]


def row(**kw):
    r = {"date": "2026-11-05", "match_id": "m3", "kickoff_utc": "2026-11-05T12:00:00Z",
         "model_version": "market-baseline-v0", "baseline_label": "基准", "generated_at": "2026-11-05T09:00:00+08:00",
         "decision": "baseline-only", "note": "", "valid_prekickoff": "True", "pick_side": "away", "line": "-1.0",
         "home_line": "1.0", "p_win": "0.4", "p_half_win": "0", "p_push": "0.2", "p_half_loss": "0", "p_loss": "0.4",
         "model_p": "0.5", "pick_odds_pinnacle": "1.95", "captured_at": "2026-11-05T08:50:00+08:00"}
    r.update(kw)
    return r


def write(d, name, rows):
    d.mkdir(exist_ok=True)
    with (d / name).open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HDR)
        w.writeheader()
        w.writerows(rows)


def test_valid_row():
    assert mp.validate_row(row()) == []


@pytest.mark.parametrize("kw,frag", [
    ({"valid_prekickoff": "False", "note": "x"}, "valid_prekickoff"),
    ({"decision": "数据缺失，不推"}, "数据缺失"),
    ({"p_loss": "0.5"}, "和="),
    ({"p_half_win": "0.1", "p_win": "0.3"}, "整数盘"),
    ({"line": "-0.75", "home_line": "0.75", "p_push": "0.2"}, "四分之一盘"),
    ({"line": "-0.5", "home_line": "0.5"}, "半球盘"),
    ({"model_p": "0.6"}, "model_p"),
    ({"home_line": "-1.0"}, "换算不一致"),
    ({"generated_at": "2026-11-05T21:00:00+08:00"}, "generated_at"),
    ({"captured_at": "2026-11-05T20:00:00+08:00"}, "captured_at"),
])
def test_invalid(kw, frag):
    assert any(frag in e for e in mp.validate_row(row(**kw)))


def test_latest_prekickoff_file(tmp_path):
    d = tmp_path / "m"
    write(d, "paper_trades_model_p_20261105T0900+0800.csv", [row(model_p="0.5")])
    write(d, "paper_trades_model_p_20261105T1000+0800.csv",
          [row(generated_at="2026-11-05T10:00:00+08:00", p_win="0.5", p_loss="0.3", model_p="0.6")])
    write(d, "paper_trades_model_p_20261105T2100+0800.csv",  # 开球后生成 → 排除
          [row(generated_at="2026-11-05T21:00:00+08:00", p_win="0.6", p_loss="0.2", model_p="0.7")])
    best, checks = mp.load(d)
    assert best[("2026-11-05", "m3")]["model_p"] == "0.6"
    assert sum(not c["ok"] for c in checks) == 1


def test_merge_into_ledger(env):  # noqa: F811
    write(env / "models", "paper_trades_model_p_20261105T0900+0800.csv",
          [row(), row(date="2026-11-04", match_id="m2", kickoff_utc="2026-11-04T20:00:00Z",
                      generated_at="2026-11-04T09:00:00+08:00", captured_at="2026-11-04T09:00:00+08:00",
                      pick_side="away", line="0.75", home_line="-0.75", p_win="0.3", p_half_win="0.2",
                      p_push="0", p_half_loss="0", p_loss="0.5", model_p="0.45")])
    p = env / "pt.csv"
    rows = by(pt.build(env / "reports", p, env / "snaps", now=NOW, model_dir=env / "models"))
    m3 = rows["m3"]
    assert m3["pick_side"] == "away" and m3["line"] == "-1" and m3["stake"] == "1" and m3["pick"] == "H 全名"
    assert m3["odds"] == "1.95" and m3["odds_format"] == "decimal"
    assert m3["model_version"] == "market-baseline-v0" and m3["model_label"] == "基准" and m3["model_p"] == "0.5"
    m2 = rows["m2"]  # 推荐行：方向仍是先知的 home，模型方向另记
    assert m2["pick_side"] == "home" and m2["model_pick_side"] == "away" and m2["model_line"] == "0.75"
    # 回填比分后：按模型方向算 Brier
    rs = list(csv.DictReader(p.open(encoding="utf-8")))
    for r in rs:
        r["result"] = {"m2": "1-0", "m3": "0-1"}.get(r["match_id"], r["result"])
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=pt.FIELDS)
        w.writeheader()
        w.writerows(rs)
    rows = by(pt.build(env / "reports", p, env / "snaps", now=NOW, model_dir=env / "models"))
    assert rows["m2"]["ah_outcome"] == "half_win"  # 先知方向 主-0.75
    assert rows["m2"]["model_y"] == "0.25" and float(rows["m2"]["brier"]) == pytest.approx(0.04)  # 客+0.75 输半
    assert rows["m3"]["ah_outcome"] == "push" and rows["m3"]["model_y"] == "0.5" and float(rows["m3"]["brier"]) == 0


def test_missing_data_rows_not_merged(env):  # noqa: F811
    write(env / "models", "paper_trades_model_p_20261105T0900+0800.csv",
          [row(decision="数据缺失，不推", valid_prekickoff="False", note="缓存中无 Pinnacle 亚盘快照")])
    m3 = by(pt.build(env / "reports", env / "pt.csv", env / "snaps", now=NOW, model_dir=env / "models"))["m3"]
    assert m3["model_p"] == "" and m3["stake"] == "" and "valid_prekickoff" in m3["model_note"]
