"""发送台账与目标会话一致性测试（不涉及模型算法）。"""
import json

import pytest

from ah import delivery


def test_empty_ledger_allows_first_send(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"schema": 1, "deliveries": []}), encoding="utf-8")
    assert delivery.should_send("2026-11-07", "abc123", "-100123", p,
                                picks_count=3, force=False) == (True, "no_delivery_record")


def test_sent_record_blocks_resend_even_if_report_changed(tmp_path):
    """幂等键是 (date, chat_id)，不是 report_sha：GitHub 补跑不会刷屏。"""
    p = tmp_path / "ledger.json"
    row = delivery.record_sent("2026-11-07", "abc123", "-100123", [111, 112],
                               picks_count=3, path=p)
    assert row["message_ids"] == [111, 112]
    assert row["picks_count"] == 3
    assert row["status"] == "sent"
    assert delivery.should_send("2026-11-07", "different_sha", "-100123", p,
                                picks_count=3, force=False) == (False, "already_delivered_today")


def test_zero_pick_delivery_may_be_upgraded_once(tmp_path):
    """已推过 0 场、本次重采有正式推荐 -> 允许补发一条，防止「又不出现比赛」。"""
    p = tmp_path / "ledger.json"
    delivery.record_sent("2026-11-07", "sha0", "-100123", [111], picks_count=0, path=p)
    assert delivery.should_send("2026-11-07", "sha1", "-100123", p,
                                picks_count=2, force=False) == (True, "upgrade_zero_pick_report")
    delivery.record_sent("2026-11-07", "sha1", "-100123", [112], picks_count=2, path=p)
    assert delivery.should_send("2026-11-07", "sha2", "-100123", p,
                                picks_count=2, force=False) == (False, "already_delivered_today")


def test_manual_dispatch_forces_resend(tmp_path):
    p = tmp_path / "ledger.json"
    delivery.record_sent("2026-11-07", "sha", "-100123", [111], picks_count=3, path=p)
    assert delivery.should_send("2026-11-07", "sha", "-100123", p,
                                picks_count=3, force=True) == (True, "forced_by_manual_dispatch")


def test_unknown_legacy_picks_count_does_not_trigger_upgrade(tmp_path):
    """旧台账没有 picks_count 时保守处理，不猜测上次是 0 场。"""
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"deliveries": [
        {"date": "2026-11-07", "chat_id": "-100123", "status": "sent",
         "message_ids": [111], "sent_at": "2026-11-07T02:00:00Z"}]}), encoding="utf-8")
    assert delivery.should_send("2026-11-07", "sha", "-100123", p,
                                picks_count=2, force=False) == (False, "already_delivered_today")


def test_record_sent_rejects_missing_message_ids(tmp_path):
    p = tmp_path / "ledger.json"
    with pytest.raises(RuntimeError, match="空 message_ids"):
        delivery.record_sent("2026-11-07", "sha", "-100123", [], path=p)
    assert not p.exists(), "不得写入无证据的送达记录"


def test_resolve_chat_rejects_conflict(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-999000111")
    with pytest.raises(RuntimeError, match="不一致"):
        delivery.resolve_chat()


def test_resolve_chat_reports_source_and_consistency(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1004312560482")
    chat, source, facts = delivery.resolve_chat()
    assert chat == "-1004312560482"
    assert source == "env:TELEGRAM_CHAT_ID"
    assert facts["consistent"] is True
    assert facts["config_chat_present"] is True
