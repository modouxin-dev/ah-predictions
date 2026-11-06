"""发送台账与目标会话一致性测试（不涉及模型算法）。"""
import json

import pytest

from ah import delivery


def test_empty_ledger_allows_first_send(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"schema": 1, "deliveries": []}), encoding="utf-8")
    assert delivery.should_send("2026-11-06", "abc123", "-100123", p) == (True, "no_delivery_record")


def test_sent_record_blocks_resend(tmp_path):
    p = tmp_path / "ledger.json"
    row = delivery.record_sent("2026-11-06", "abc123", "-100123", [111, 112], path=p)
    assert row["message_ids"] == [111, 112]
    assert row["status"] == "sent"
    assert row["evidence"] == "Telegram sendMessage HTTP 200 / ok=true / message_id"
    assert delivery.should_send("2026-11-06", "abc123", "-100123", p) == (False, "ledger_hit")


def test_changed_report_is_a_new_delivery(tmp_path):
    p = tmp_path / "ledger.json"
    delivery.record_sent("2026-11-06", "oldsha", "-100123", [111], path=p)
    assert delivery.should_send("2026-11-06", "newsha", "-100123", p)[0] is True


def test_record_sent_rejects_missing_message_ids(tmp_path):
    p = tmp_path / "ledger.json"
    with pytest.raises(RuntimeError, match="空 message_ids"):
        delivery.record_sent("2026-11-06", "sha", "-100123", [], path=p)
    assert not p.exists(), "不得写入无证据的送达记录"


def test_resolve_chat_rejects_conflict(monkeypatch):
    # config/settings.yaml 中 telegram.chat_id 为 -1004312560482
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
