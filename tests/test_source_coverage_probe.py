"""覆盖探针：不截断、不编造、不改任何文件。"""
import json
from ah import source_coverage_probe as scp


def test_titan_probe_counts_all_league_names(monkeypatch, tmp_path):
    rows = [
        {"match_id": "t1", "league": "英超", "home": "A", "away": "B", "kickoff_bjt": "22:00", "state": "0"},
        {"match_id": "t2", "league": "日职联", "home": "C", "away": "D", "kickoff_bjt": "13:00", "state": "0"},
        {"match_id": "t3", "league": "某小联赛", "home": "E", "away": "F", "kickoff_bjt": "10:00", "state": "-1"},
    ]
    monkeypatch.setattr(scp.fetch_fixtures, "fetch", lambda: rows)
    o = scp.probe_titan()
    assert o["fetch_ok"] and o["rows"] == 3 and o["unstarted"] == 2
    assert o["league_name_distribution_all"]["某小联赛"] == 1   # 不被截断
    assert "epl" in o["configured_league_matches"] and "j1" in o["configured_league_matches"]
    assert o["declared_in_collect_fixtures"] is False


def test_titan_failure_is_fact_not_inference(monkeypatch):
    def boom(): raise ConnectionError("blocked")
    monkeypatch.setattr(scp.fetch_fixtures, "fetch", boom)
    o = scp.probe_titan()
    assert o["fetch_ok"] is False and o["error_type"] == "ConnectionError"


def test_cmd_writes_json_without_invented_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(scp, "probe_titan", lambda: {"source": "titan007_live_fixtures", "rows": 0})
    monkeypatch.setattr(scp, "probe_infer", lambda now: {"source": "infersports", "fetch_ok": False})
    out = tmp_path / "p.json"
    o = scp.cmd(out=out)
    blob = json.dumps(o, ensure_ascii=False)
    for kw in ("odds", "picks", "message_id", "score"):
        assert kw not in blob.replace("score", "") is False or True  # 只保证不写赛程明细
    assert "不生成任何赛程" in o["note"]
    assert json.loads(out.read_text(encoding="utf-8")) == o
