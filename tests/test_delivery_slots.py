"""一天 4 次推送（北京 07/11/16/22）：时段幂等、复用采集、仅列未开赛场次。"""
from datetime import datetime, timezone

from ah import delivery

CHAT = "-100"
D = "2026-11-07"
NOW = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)   # 北京 11:00


def sent(path, slot, picks=2):
    delivery.record_sent(D, "sha", CHAT, [1], picks_count=picks, path=path, slot=slot)


def test_first_slot_primary_then_refresh_slots_each_once(tmp_path):
    led = tmp_path / "l.json"
    assert delivery.delivery_mode(D, "s", CHAT, led, 2, force=False, slot="1") == ("primary", "no_delivery_record")
    sent(led, "1")
    assert delivery.delivery_mode(D, "s", CHAT, led, 2, force=False, slot="1")[0] == "skip"
    for slot in ("2", "3", "4"):
        assert delivery.delivery_mode(D, "s", CHAT, led, 2, force=False, slot=slot)[0] == "refresh"
        sent(led, slot)
        assert delivery.delivery_mode(D, "s", CHAT, led, 2, force=False, slot=slot)[0] == "skip"


def test_morning_failed_later_slot_falls_back_to_full_primary(tmp_path):
    led = tmp_path / "l.json"
    assert delivery.delivery_mode(D, "s", CHAT, led, 2, force=False, slot="3")[0] == "primary"
    sent(led, "3")
    assert delivery.delivery_mode(D, "s", CHAT, led, 2, force=False, slot="4")[0] == "refresh"


def test_manual_dispatch_forces_and_legacy_rows_count_as_slot_1(tmp_path):
    led = tmp_path / "l.json"
    delivery.save_ledger({"schema": 1, "deliveries": [
        {"date": D, "chat_id": CHAT, "status": "sent", "message_ids": [9], "picks_count": 3}]}, led)
    assert delivery.delivery_mode(D, "s", CHAT, led, 3, force=False, slot="1")[0] == "skip"
    assert delivery.delivery_mode(D, "s", CHAT, led, 3, force=True, slot="manual")[0] == "primary"
    assert delivery.should_send(D, "s", CHAT, led, 3, force=False)[0] is False     # 旧接口行为不变


def test_zero_pick_morning_can_be_upgraded_by_primary_only(tmp_path):
    led = tmp_path / "l.json"
    sent(led, "1", picks=0)
    assert delivery.delivery_mode(D, "s", CHAT, led, 2, force=False, slot="1") == ("primary", "upgrade_zero_pick_report")
    assert delivery.delivery_mode(D, "s", CHAT, led, 2, force=False, slot="2")[0] == "refresh"


def test_refresh_view_keeps_only_unstarted_and_never_adds_content():
    rep = {"picks": [{"match_id": "a", "kickoff_utc": "2026-11-07T02:00:00Z"},     # 已开球
                     {"match_id": "b", "kickoff_utc": "2026-11-07T12:30:00Z"},
                     {"match_id": "c", "kickoff_utc": "坏时间"}],                      # 无法证明未开赛 → 丢弃
           "watchlist": [{"match_id": "d", "kickoff_utc": "2026-11-07T13:00:00Z"},
                         {"match_id": "e", "kickoff_utc": "2026-11-07T01:00:00Z"}]}
    v = delivery.refresh_view(rep, NOW)
    assert [p["match_id"] for p in v["picks"]] == ["b"]
    assert [w["match_id"] for w in v["watchlist"]] == ["d"]
    assert len(rep["picks"]) == 3 and len(rep["watchlist"]) == 2                   # 原报告不被修改


def test_refresh_note_states_source_time_and_scope():
    note = delivery.refresh_note({"generated_at": "2026-11-06T22:20:00Z"}, "2", NOW)
    assert "第 2 次更新" in note and "北京 11:00" in note and "06:20" in note and "未开赛" in note


def test_bj_date_is_utc_plus_8(monkeypatch):
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 6, 23, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(delivery, "datetime", FakeDT)
    assert delivery._bj_date() == "2026-11-07"


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_resolve_slot_uses_cron_target_not_run_time():
    # 22:00 UTC 的第 1 次：北京次日 06:00 起 → 日期为北京次日
    assert delivery.resolve_slot("schedule", "0 22 * * *", "", _utc(2026, 11, 7, 22, 5)) == ("1", "2026-11-08", 300)
    # 实测事故：14:00 UTC 的第 4 次延迟到 18:44 UTC（北京已是 11-08 02:44）→ 仍归属 11-07
    slot, date, late = delivery.resolve_slot("schedule", "0 14 * * *", "", _utc(2026, 11, 7, 18, 44))
    assert (slot, date) == ("4", "2026-11-07") and late == 4 * 3600 + 44 * 60
    # 03:00 UTC 的第 2 次延迟到 09:51 UTC：仍是北京同日
    assert delivery.resolve_slot("schedule", "0 3 * * *", "", _utc(2026, 11, 7, 9, 51))[:2] == ("2", "2026-11-07")
    # 第 1 次延迟到跨 UTC 午夜
    assert delivery.resolve_slot("schedule", "0 22 * * *", "", _utc(2026, 11, 8, 1, 0))[:2] == ("1", "2026-11-08")


def test_resolve_slot_dispatch_and_unknown_cron():
    import pytest
    assert delivery.resolve_slot("workflow_dispatch", "", "3", _utc(2026, 11, 7, 8, 0)) == ("3", "2026-11-07", 0)
    assert delivery.resolve_slot("workflow_dispatch", "", "", _utc(2026, 11, 7, 17, 0))[:2] == ("manual", "2026-11-08")
    with pytest.raises(RuntimeError):
        delivery.resolve_slot("schedule", "5 5 * * *", "", _utc(2026, 11, 7, 8, 0))


def test_ah_date_env_overrides_clock(monkeypatch):
    import pytest
    monkeypatch.setenv("AH_DATE", "2026-11-07")
    assert delivery._bj_date() == "2026-11-07"
    monkeypatch.setenv("AH_DATE", "bad")
    with pytest.raises(RuntimeError):
        delivery._bj_date()


def test_morning_edition_not_swallowed_by_earlier_fallback_slot(tmp_path):
    """实测事故：延迟的第 4 次先兜底发出后，当日 07:00 早间版不能被吞掉。"""
    led = tmp_path / "l.json"
    sent(led, "4", picks=6)
    assert delivery.delivery_mode(D, "s", CHAT, led, 6, force=False, slot="1") == ("primary", "morning_edition_not_delivered")
    sent(led, "1", picks=6)
    assert delivery.delivery_mode(D, "s", CHAT, led, 6, force=False, slot="1")[0] == "skip"
