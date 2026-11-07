"""样本池 / 结算健康盘点：只留事实、不编造、不改算法。"""
import csv
import json

from ah import samples


REPORT = {
    "date": "2026-11-07",
    "generated_at": "2026-11-06T23:42:49Z",
    "picks": [{
        "match_id": "m1", "league": "英超", "home": "A", "away": "B",
        "kickoff_utc": "2026-11-07T12:00:00Z", "confidence": "high", "score": 4.05,
        "pick_side": "away", "pick_team": "B", "line": -1.0, "line_open": -0.75,
        "odds": 1.07, "verification_advisory": True, "sources": ["https://x"],
        "reasons": ["近期赛果 A LLDLL"], "risks": ["尚未自动核实伤停及首发"],
    }],
    "watched": [],
    "watchlist": [
        {"match_id": "m2", "league": "日职联", "home": "C", "away": "D",
         "kickoff_utc": "2026-11-07T04:00:00Z", "withheld_reason": "近期状态数据不足",
         "withheld_category": "近期状态不足"},
        {"match_id": "m3", "league": "英冠", "home": "E", "away": "F",
         "kickoff_utc": "2026-11-07T15:00:00Z", "withheld_reason": "评分不足",
         "observation": {"score": 2.22, "line": -0.75, "line_open": -0.5,
                         "odds": 0.91, "pick_team": "E", "source": "oddspapi"}},
    ],
}


def test_val_keeps_zero_and_false():
    assert samples._val({"line": 0.0}, "line") == 0.0
    assert samples._val({"advisory": False}, "advisory") is False
    assert samples._val({}, "missing") == samples.MISSING


def test_pool_covers_all_candidates_and_invents_nothing(tmp_path):
    import copy
    before = copy.deepcopy(REPORT)
    reports = tmp_path / "reports"
    (reports / "2026" / "11").mkdir(parents=True)
    (reports / "2026" / "11" / "2026-11-07.json").write_text(
        json.dumps(REPORT, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "samples"
    p = samples.write_pool("2026-11-07", reports=reports, out_dir=out)
    assert p["counts"]["total"] == 3
    assert p["counts"]["recommended"] == 1 and p["counts"]["not_recommended"] == 2
    assert REPORT == before                      # 不修改原报告

    saved = json.loads((out / "2026-11-07.json").read_text(encoding="utf-8"))
    rows = {r["match_id"]: r for r in saved["rows"]}
    assert rows["m1"]["sample_role"] == "recommended"
    assert rows["m1"]["score"] == 4.05 and rows["m1"]["line"] == -1.0
    assert rows["m1"]["verification_advisory"] is True
    assert rows["m1"]["home"] == "A" and rows["m1"]["away"] == "B"   # 逐字回溯
    assert rows["m1"]["result"] == samples.MISSING and rows["m1"]["settled"] is False
    assert rows["m2"]["withheld_reason"] == "近期状态数据不足"
    assert rows["m2"]["line"] == samples.MISSING                      # 缺字段不填 0
    assert rows["m3"]["observation"]["score"] == 2.22                 # 已有观察分，不当正式 score
    assert rows["m3"]["score"] == samples.MISSING
    assert all(r["result"] == samples.MISSING for r in saved["rows"])


def test_pool_deduplicates_and_does_not_create_predictions(tmp_path):
    rep = {"date": "2026-11-07", "generated_at": "2026-11-06T23:00:00Z",
           "picks": [{"match_id": "x", "home": "A", "away": "B"}],
           "watched": [], "watchlist": [{"match_id": "x", "home": "A", "away": "B"}]}
    rows = samples.pool_rows(rep)
    assert len(rows) == 1 and rows[0]["sample_role"] == "recommended"
    assert "prediction" not in rows[0] and "pick_recommendation" not in rows[0]


def test_health_lists_unsettled_without_inventing_scores(tmp_path):
    csvp = tmp_path / "predictions.csv"
    with csvp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "match_id", "home", "away",
                                          "kickoff_utc", "settled", "status", "result", "confidence"])
        w.writeheader()
        w.writerow({"date": "2026-11-04", "match_id": "a", "home": "X", "away": "Y",
                    "kickoff_utc": "2026-11-04T20:00:00Z", "settled": "true",
                    "status": "active", "result": "4-1", "confidence": "medium"})
        w.writerow({"date": "2026-11-07", "match_id": "b", "home": "P", "away": "Q",
                    "kickoff_utc": "2026-11-07T15:00:00Z", "settled": "false",
                    "status": "active", "result": "", "confidence": "low"})
        w.writerow({"date": "2026-11-07", "match_id": "c", "home": "R", "away": "S",
                    "kickoff_utc": "2026-11-07T15:00:00Z", "settled": "false",
                    "status": "active", "result": "", "confidence": "low"})
    out = tmp_path / "settle_health.json"
    h = samples.write_health(csv_path=csvp, out=out, run_id="1", conclusion="success", event="workflow_dispatch")
    assert h["total_rows"] == 3 and h["settled_rows"] == 1 and h["unsettled_rows"] == 2
    assert {x["match_id"] for x in h["unsettled"]} == {"b", "c"}
    assert all(x["result"] == samples.MISSING for x in h["unsettled"])
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert "4-1" not in json.dumps(saved, ensure_ascii=False)   # 不把已结算比分混进未结算清单
    assert saved["job_conclusion"] == "success"


def test_health_missing_csv_is_explicit_not_a_crash(tmp_path):
    h = samples.write_health(csv_path=tmp_path / "nope.csv", out=tmp_path / "h.json")
    assert h["total_rows"] == 0 and h["unsettled_rows"] == 0
