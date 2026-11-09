"""准点调度：只做 (date,slot) 幂等决策，绝不重复触发、不编造数据。"""
import json
from datetime import datetime, timedelta, timezone

from ah import punctual


def _bj(y, m, d, hh, mm):
    """北京时间 -> UTC aware（项目统一 utc+8 口径）。"""
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc) - timedelta(hours=8)


def _run(rid, created, event="schedule"):
    return {"id": rid, "event": event, "created_at": created}


def test_dispatches_inside_window_when_not_delivered(tmp_path):
    led = tmp_path / "l.json"
    led.write_text(json.dumps({"deliveries": []}), encoding="utf-8")
    now = _bj(2026, 11, 9, 11, 10)   # slot 2 目标 11:00 后 10 分钟
    o = punctual.decide(now=now, ledger_path=led, runs=[])
    acts = {a["slot"]: a for a in o["actions"]}
    assert "2" in acts
    assert acts["2"]["body"] == {"ref": "main", "inputs": {"slot": "2"}}
    assert acts["2"]["date"] == "2026-11-09"
    assert acts["2"]["minutes_from_target"] == 10.0
    # 其余时段都不在窗口内
    assert {s["slot"] for s in o["skips"]} == {"1", "3", "4"}


def test_skips_before_target_and_outside_window(tmp_path):
    led = tmp_path / "l.json"
    led.write_text(json.dumps({"deliveries": []}), encoding="utf-8")
    before = punctual.decide(now=_bj(2026, 11, 9, 5, 55), ledger_path=led, runs=[])
    assert all(s["reason"] == "before_target" for s in before["skips"])
    assert before["actions"] == []
    after = punctual.decide(now=_bj(2026, 11, 9, 12, 30), ledger_path=led, runs=[])
    assert all(s["reason"] in ("before_target", "outside_window") for s in after["skips"])
    assert after["actions"] == []


def test_ledger_record_blocks_second_dispatch(tmp_path):
    led = tmp_path / "l.json"
    led.write_text(json.dumps({"deliveries": [
        {"date": "2026-11-09", "slot": "2", "status": "sent", "message_ids": [549]}]}), encoding="utf-8")
    o = punctual.decide(now=_bj(2026, 11, 9, 11, 10), ledger_path=led, runs=[])
    assert o["actions"] == []
    assert any(s["reason"] == "already_delivered" and s["slot"] == "2" for s in o["skips"])


def test_dispatch_log_blocks_same_slot(tmp_path):
    led = tmp_path / "l.json"
    led.write_text(json.dumps({"deliveries": []}), encoding="utf-8")
    log = [{"date": "2026-11-09", "slot": "2", "status": 204, "at_utc": "2026-11-09T03:00:00Z"}]
    o = punctual.decide(now=_bj(2026, 11, 9, 11, 10), ledger_path=led, dispatch_log=log)
    assert o["actions"] == []
    s = next(x for x in o["skips"] if x["slot"] == "2")
    assert s["reason"] == "already_dispatched"


def test_other_slots_and_other_runs_do_not_block(tmp_path):
    """关键：链路自检发的 slot=1 不能把 11:00 的 slot=2 挡掉。"""
    led = tmp_path / "l.json"
    led.write_text(json.dumps({"deliveries": []}), encoding="utf-8")
    now = _bj(2026, 11, 9, 11, 5)
    runs = [_run(999, (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"))]
    log = [{"date": "2026-11-09", "slot": "1", "status": 204, "at_utc": "2026-11-08T22:00:00Z"},
           {"date": "2026-11-08", "slot": "2", "status": 204, "at_utc": "2026-11-08T03:00:00Z"}]
    o = punctual.decide(now=now, ledger_path=led, runs=runs, dispatch_log=log)
    assert [a["slot"] for a in o["actions"]] == ["2"]


def test_append_dispatch_is_atomic_and_never_echoes_token(tmp_path):
    p = tmp_path / "log.json"
    log = punctual.append_dispatch([], "2026-11-09", "2", 204,
                                   _bj(2026, 11, 9, 11, 5), path=p)
    assert len(log) == 1 and log[0]["status"] == 204
    blob = p.read_text(encoding="utf-8")
    assert "Bearer" not in blob and "github_pat" not in blob
    assert not list(tmp_path.glob(".pd-*"))
    assert punctual.already_dispatched(punctual.load_dispatch_log(p), "2026-11-09", "2")


def test_late_night_does_not_roll_slot4_to_wrong_date(tmp_path):
    """22:00 的 slot 4 延迟到次日凌晨：60 分钟窗口已过，不自动补（避免归错日期）。"""
    led = tmp_path / "l.json"
    led.write_text(json.dumps({"deliveries": []}), encoding="utf-8")
    o = punctual.decide(now=_bj(2026, 11, 9, 0, 45), ledger_path=led, runs=[])
    assert o["actions"] == []
    assert o["checked_at_bj"].startswith("2026-11-09")
    assert "不自动补" in o["note"] or "窗口外的漏发" in o["note"]


def test_no_fabricated_fields_in_decision(tmp_path):
    led = tmp_path / "l.json"
    led.write_text("{}", encoding="utf-8")
    o = punctual.decide(now=_bj(2026, 11, 9, 11, 10), ledger_path=led, runs=[])
    blob = json.dumps(o, ensure_ascii=False)
    for kw in ("odds", "picks", "fixture", "message_id", "score"):
        assert kw not in blob
    assert "不生成赛程" in o["note"]
