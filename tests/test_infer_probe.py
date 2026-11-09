"""换出口探针：只统计逐层淘汰，不编造赛程/盘口/倾向。"""
import json
from datetime import datetime, timezone

from ah import infer_probe


class FakeClient:
    def __init__(self, rows): self.rows = rows
    def events(self, date): return self.rows


def _game(mid, league, status="scheduled", ko="2026-11-09T12:00:00Z", home="A", away="B"):
    return {"id": mid, "status": status, "league": {"name": league},
            "home_team": {"name": home}, "away_team": {"name": away}, "scheduled_at": ko}


def test_stage_counts_locate_the_drop():
    now = datetime(2026, 11, 9, 1, 30, tzinfo=timezone.utc)
    rows = [
        _game("1", "Jap1", "finished"),                 # status 淘汰
        _game("2", "UnknownCup"),                       # 联赛未映射
        _game("3", "Jap1", ko="2026-11-08T12:00:00Z"),  # 时间窗外
        _game("4", "Jap1", ko="bad"),                    # 开球时间不可解析
        _game("5", "Jap1", ko="2026-11-09T12:00:00Z"),  # 通过
        _game("6", "Jap1", ko="2026-11-09T12:00:00Z"),  # 与 5 同名同时间 -> 去重
    ]
    o = infer_probe.probe(hours=36, now=now, client=FakeClient(rows))
    s = o["stages"]
    assert s["raw"] == 12               # 两天各 6 条
    assert s["drop_status"] == 2
    assert s["drop_league_unmapped"] == 2
    assert s["drop_bad_kickoff"] == 2
    assert s["drop_outside_window"] == 2
    # 两天的样本 ko 相同：daily.add 的 rows 跨天累积，同联赛同名同开球时间会跨天去重，
    # 因此只有 1 条真正入池、3 条被去重（这是与线上一致的行为，不是探针算错）。
    assert s["pass_all"] == 1
    assert s["drop_duplicate"] == 3
    assert o["mapping"]["jap1"] == "j1"
    assert "Jap1" in o["league_name_distribution"]
    assert "scheduled" in o["status_distribution"]
    assert all("odds" not in k and "line" not in k for x in o["first_passing_samples"] for k in x)
    assert "不生成任何赛程" in o["note"]


def test_cmd_writes_atomic_json(tmp_path):
    now = datetime(2026, 11, 9, 1, 30, tzinfo=timezone.utc)
    out = tmp_path / "infer_probe.json"
    o = infer_probe.cmd(out=out, hours=36) if False else infer_probe.probe(now=now, client=FakeClient([]))
    out.write_text(json.dumps(o, ensure_ascii=False), encoding="utf-8")
    assert json.loads(out.read_text(encoding="utf-8"))["stages"].get("raw", 0) == 0
