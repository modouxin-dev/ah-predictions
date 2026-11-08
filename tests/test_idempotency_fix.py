"""A1 回归：发送幂等不得被跨时段 0 场记录绕过。

复现场景：slot=1 已发 7 场，台账末条是 slot=4 的 0 场兜底记录。
修复前 delivery_mode 取 rows[-1] -> primary/upgrade_zero_pick_report（会重发完整日报）；
修复后只看本 slot -> skip。
"""
from __future__ import annotations

import json
from pathlib import Path

from ah import delivery


def _ledger(p: Path, rows: list[dict]) -> Path:
    p.write_text(json.dumps({"deliveries": rows}, ensure_ascii=False), encoding="utf-8")
    return p


def _sent(slot: str, picks: int, ids: list[int] | None = None) -> dict:
    return {"date": "2026-11-08", "slot": slot, "status": "sent",
            "message_ids": ids if ids is not None else [1], "picks_count": picks,
            "chat_id": "C", "sent_at": "2026-11-08T00:00:00Z"}


def test_cross_slot_zero_pick_does_not_bypass_idempotency(tmp_path):
    p = _ledger(tmp_path / "l.json", [_sent("1", 7, [547, 548]), _sent("4", 0, [545, 546])])
    assert delivery.delivery_mode("2026-11-08", "s", "C", p, 7, force=False, slot="1") == \
        ("skip", "already_delivered_today")


def test_same_slot_zero_pick_allows_upgrade(tmp_path):
    p = _ledger(tmp_path / "l.json", [_sent("1", 0, [1])])
    assert delivery.delivery_mode("2026-11-08", "s", "C", p, 7, force=False, slot="1") == \
        ("primary", "upgrade_zero_pick_report")


def test_same_slot_nonzero_is_idempotent(tmp_path):
    p = _ledger(tmp_path / "l.json", [_sent("1", 7)])
    assert delivery.delivery_mode("2026-11-08", "s", "C", p, 7, force=False, slot="1") == \
        ("skip", "already_delivered_today")


def test_no_delivery_record_primary_for_any_slot(tmp_path):
    p = _ledger(tmp_path / "l.json", [])
    assert delivery.delivery_mode("2026-11-08", "s", "C", p, 7, force=False, slot="3")[0] == "primary"


def test_morning_edition_not_swallowed_by_other_slot(tmp_path):
    p = _ledger(tmp_path / "l.json", [_sent("4", 7)])
    assert delivery.delivery_mode("2026-11-08", "s", "C", p, 7, force=False, slot="1") == \
        ("primary", "morning_edition_not_delivered")


def test_refresh_slot_not_yet_delivered(tmp_path):
    p = _ledger(tmp_path / "l.json", [_sent("1", 7)])
    assert delivery.delivery_mode("2026-11-08", "s", "C", p, 7, force=False, slot="2") == \
        ("refresh", "refresh_slot_not_delivered")


def test_unknown_slot_never_falls_back_to_cross_slot_zero_pick(tmp_path):
    p = _ledger(tmp_path / "l.json", [_sent("4", 0, [545, 546])])
    assert delivery.delivery_mode("2026-11-08", "s", "C", p, 7, force=False, slot="manual") == \
        ("skip", "already_delivered_today")


def test_backfill_mode_still_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("AH_DELIVERY_MODE", "backfill")
    monkeypatch.setenv("AH_SLOT", "manual")
    p = _ledger(tmp_path / "l.json", [_sent("manual", 7)])
    assert delivery.should_send("2026-11-08", "s", "C", p, 7, slot="manual")[0] is False
