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
