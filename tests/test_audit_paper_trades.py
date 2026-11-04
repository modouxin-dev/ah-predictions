import csv
import json
from datetime import datetime, timezone

import pytest

from ah.audit import paper_trades as pt

REP = {
    "date": "2026-11-04", "odds_format": "HK (港盘水位)",
    "picks": [
        {"match_id": "m1", "league": "L", "home": "A", "away": "B", "kickoff_utc": "2026-11-04T12:00:00Z",
         "pick_side": "away", "pick_team": "B", "line": -0.75, "line_open": -0.5, "odds": 0.78,
         "confidence": "medium", "cancelled": True, "cancel_reason": "x"},
        {"match_id": "m2", "league": "L", "home": "C", "away": "D", "kickoff_utc": "2026-11-04T20:00:00Z",
         "pick_side": "home", "line": -0.75, "line_open": -0.25, "odds": 1.06, "confidence": "medium"},
    ],
    "watched": [{"league": "L", "home": "E", "away": "F", "reason_skipped": "r"},
                {"league": "L", "home": "G", "away": "H", "reason_skipped": "dup"},
                {"league": "L", "home": "多场", "away": "-", "reason_skipped": "覆盖说明"}],
}
REP2 = {"date": "2026-11-05", "odds_format": "HK", "picks": [], "watched": [],
        "watchlist": [{"match_id": "m3", "league": "L", "home": "G 全名", "away": "H 全名",
                       "kickoff_utc": "2026-11-05T12:00:00Z", "ah_home_line": 1.0, "ah_home_line_open": 0.5,
                       "confidence": "low", "withheld_category": "深盘", "withheld_reason": "y"}]}
NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    d = tmp_path / "reports" / "2026" / "11"
    d.mkdir(parents=True)
    (d / "2026-11-04.json").write_text(json.dumps(REP), encoding="utf-8")
    (d / "2026-11-05.json").write_text(json.dumps(REP2), encoding="utf-8")
    return tmp_path


def run(env, **kw):
    kw.setdefault("model_dir", env / "models")
    return pt.build(env / "reports", env / "pt.csv", env / "snaps", now=NOW, **kw)


def by(rows):
    return {r["match_id"]: r for r in rows}


def test_rows_status_and_perspective(env):
    rows = by(run(env))
    assert set(rows) == {"m1", "m2", "m3", "watch-E-F"}  # G-H 去重
    assert rows["m1"]["status"] == "cancelled" and rows["m1"]["confidence"] == "medium"
    assert rows["m1"]["ah_outcome"] == "void" and rows["m1"]["units"] == "0"
    assert rows["m1"]["line"] == "-0.75" and rows["m1"]["home_line"] == "0.75"
    assert rows["m2"]["home_line"] == "-0.75" and rows["m2"]["odds_format"] == "hk"
    assert rows["m3"]["status"] == "watch" and rows["m3"]["pick_side"] == "" and rows["m3"]["home_line"] == "1"
    assert "no_model_direction" in rows["m3"]["note"]
    assert rows["m2"]["model_p"] == "" and rows["m2"]["result"] == ""


def test_idempotent_and_keeps_backfill(env):
    run(env)
    p = env / "pt.csv"
    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    for r in rows:
        if r["match_id"] == "m2":
            r["result"] = "2-1"
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=pt.FIELDS)
        w.writeheader()
        w.writerows(rows)
    rows = by(run(env))
    assert len(rows) == 4
    assert rows["m2"]["ah_outcome"] == "half_win" and rows["m2"]["units"] == "0.53"
    assert len(by(run(env))) == 4


SNAP_HDR = "match_id,captured_at,stage,bookmaker,ah_home_line,odds_home,odds_away,odds_format,source,verification,is_main,as_of,kickoff_utc\n"


def _snap(env, lines):
    s = env / "snaps"
    s.mkdir(exist_ok=True)
    (s / "2026-11-04.csv").write_text("\ufeff" + SNAP_HDR + "\n".join(lines) + "\n", encoding="utf-8")


def test_closing_snapshot_and_clv(env):
    K = "2026-11-04T20:00:00Z"
    _snap(env, [
        f"m2,2026-11-04T19:00:00Z,close,pinnacle,-0.5,1.9,1.9,decimal,oddspapi,verified,1,2026-11-04T19:00:00Z,{K}",
        f"m2,2026-11-04T19:55:00Z,close,pinnacle,-1.0,1.95,1.91,decimal,oddspapi,verified,1,2026-11-04T19:55:00Z,{K}",
        f"m2,2026-11-04T19:55:00Z,close,pinnacle,-0.75,1.90,1.96,decimal,oddspapi,verified,0,2026-11-04T19:55:00Z,{K}",
        f"m2,2026-11-04T19:56:00Z,close,sbobet,-0.75,1.80,2.00,decimal,oddspapi,verified,1,2026-11-04T19:56:00Z,{K}",
        f"m2,2026-11-04T20:10:00Z,close,pinnacle,-1.25,1.9,1.9,decimal,oddspapi,verified,1,2026-11-04T20:10:00Z,{K}",
        f"m2,2026-11-04T19:58:00Z,report,pinnacle,-0.75,1.5,2.5,decimal,oddspapi,verified,1,2026-11-04T19:58:00Z,{K}",
        "m1,2026-11-04T11:50:00Z,close,pinnacle,0.25,1.9,1.92,decimal,odds_api,single,1,2026-11-04T11:50:00Z,2026-11-04T12:00:00Z",
    ])
    rows = by(run(env))
    m2 = rows["m2"]  # pinnacle 优先于 sbobet；开球后的、report 阶段的忽略
    assert m2["close_bookmaker"] == "pinnacle" and m2["close_home_line"] == "-1" and m2["close_line"] == "-1"
    assert m2["line_move"] == "-0.25" and m2["close_captured_at"] == "2026-11-05T03:55:00+08:00"
    p = (1 / 1.90) / (1 / 1.90 + 1 / 1.96)  # 同公司同时刻 -0.75 相邻线
    assert float(m2["clv"]) == pytest.approx(2.06 * p - 1, abs=1e-4)
    assert m2["close_odds"] == "0.9" and m2["odds_close"] == "0.9"
    m1 = rows["m1"]  # 客队 -0.75 下注；收盘主队 +0.25 = 客队 -0.25，无 -0.75 这条线
    assert m1["close_line"] == "-0.25" and m1["line_move"] == "0.5"
    assert m1["clv"] == "" and m1["close_note"].startswith("line_moved_needs_model")


def test_no_snapshot_dir(env):
    assert by(run(env))["m2"]["close_note"] == "odds_snapshots 目录不存在"


def test_clv_formula():
    assert pt.clv(2.0, 2.0, 2.0) == pytest.approx(0.0)
    assert pt.fair_prob(1.9, 1.9) == pytest.approx(0.5)
    assert pt.to_decimal(0.78, "hk") == pytest.approx(1.78)
    assert pt.to_decimal(1.78, "decimal") == 1.78


def test_brier_settled_score():
    assert pt.brier(0.6, "win") == pytest.approx(0.16)
    assert pt.brier(0.6, "half_win") == pytest.approx(0.0225)
    assert pt.brier(0.6, "push") == pytest.approx(0.01)
    assert pt.brier(0.6, "half_loss") == pytest.approx(0.1225)
    assert pt.brier(0.6, "loss") == pytest.approx(0.36)


def test_score_requires_two_sources(env):
    calls = []

    def one_source(r):
        calls.append(r["match_id"])
        return None, "", "仅有来源：API-Football，需第二来源"
    rows = by(run(env, score_fetcher=one_source))
    assert rows["m2"]["result"] == "" and "第二来源" in rows["m2"]["score_note"]
    assert "m1" in calls or rows["m1"]["ah_outcome"] == "void"


def test_two_source_score_agreement(monkeypatch):
    class Api:
        def fixture_result(self, fid):
            return {"finished": True, "home_goals": 2, "away_goals": 1}
    r = {"match_id": "apif-1", "league": "L", "home": "A", "away": "B", "kickoff_utc": "2026-11-04T20:00:00Z"}
    monkeypatch.setattr("ah.settle._fd_score", lambda fd, row: (2, 1))
    assert pt.two_source_score(r, Api(), None)[0] == "2-1"
    monkeypatch.setattr("ah.settle._fd_score", lambda fd, row: (1, 1))
    assert pt.two_source_score(r, Api(), None)[0] is None
    monkeypatch.setattr("ah.settle._fd_score", lambda fd, row: None)
    assert pt.two_source_score(r, Api(), None)[0] is None


def test_engineer_columns_and_bj_time(env):
    rows = by(run(env))
    for c in ["date", "match_id", "league", "home", "away", "pick", "line", "odds_taken", "odds_close",
              "clv", "stake", "result", "pnl", "settled_at"]:
        assert c in pt.FIELDS
    m2, m1, m3 = rows["m2"], rows["m1"], rows["m3"]
    assert m2["kickoff_bj"] == "2026-11-05T04:00:00+08:00"
    assert m2["updated_at"].endswith("+08:00")
    assert m2["pick"] == "C" and m2["odds_taken"] == "1.06" and m2["stake"] == "1" and m2["settled_at"] == ""
    assert m1["stake"] == "0" and m1["pnl"] == "0" and m1["settled_at"].endswith("+08:00")
    assert m3["stake"] == "" and m3["pick"] == ""
