"""数据源依赖审计 + 漏发自愈盘点 回归测试。

纯本地、不联网、不发消息、不改 provider / 源开关 / 核验口径 / 台账。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ah import delivery_gaps, source_audit


CFG = {
    "api_football": {"enabled": True, "base_url": "https://v3.football.api-sports.io"},
    "the_odds_api": {"base_url": "https://api.the-odds-api.com/v4"},
    "football_data": {}, "fivedollar": {}, "oddspapi": {},
    "infersports": {"base_url": "https://api.infersports.dev/v1", "auth": "keyless 可用"},
    "working": {"titan007_odds": {"url": "https://live.titan007.com/vbsxml/goal3.xml?r=007XXXX", "use": "亚盘"},
                "uefa_fixtures": {"url": "https://www.uefa.com/uefachampionsleague/", "use": "欧冠赛程"}},
    "scrapers": {"jleague": {"enabled": True, "base_url": "https://www.jleague.jp"},
                 "afc": {"enabled": True, "probe_url": "https://www.the-afc.com/en/club/afc_champions_league_elite.html"}},
}


# ---------------------------------------------------------------- source_audit

def test_registered_rows_never_echo_secret_value():
    rows = source_audit.registered_rows(CFG, {"API_FOOTBALL_KEY": "SUPER_SECRET_123"})
    r = next(x for x in rows if x["source"] == "api_football")
    assert r["credential_present"] is True
    assert r["credential_env"] == "API_FOOTBALL_KEY"
    assert r["credential_value"] == "REDACTED"
    assert "SUPER_SECRET_123" not in json.dumps(rows, ensure_ascii=False)
    assert all(x["auth_class"] == "registered" for x in rows)


def test_keyless_rows_mark_no_registration():
    rows = source_audit.keyless_rows(CFG)
    assert len(rows) == len(source_audit.KEYLESS_SOURCES)
    assert all(x["auth_class"] == "keyless" and x["requires_registration"] is False for x in rows)
    # 需特殊请求参数 / 条款禁止的站点不得进入连通探测白名单
    assert "titan007" not in " ".join(source_audit.KEYLESS_PROBE_URLS)
    assert "spl.com.sa" not in " ".join(source_audit.KEYLESS_PROBE_URLS.values())
    assert "oddsportal" not in " ".join(source_audit.KEYLESS_PROBE_URLS.values())


def test_missing_credentials_warn_but_never_block():
    out = source_audit.audit(CFG, env={}, probes=[])
    assert out["summary"]["registered_configured"] == 0
    assert out["summary"]["registered_missing"] == 5
    assert any(a["code"] == "REGISTERED_CREDENTIALS_MISSING" for a in out["alerts"])
    assert any(a["code"] == "ALL_REGISTERED_UNAVAILABLE" for a in out["alerts"])
    assert out["policy"].startswith("免注册优先")
    # 结论里不得出现「密钥无效」「站点不可用」这类推断
    assert "密钥无效" not in json.dumps(out, ensure_ascii=False)


def test_all_configured_has_no_missing_alert():
    env = {v: "x" for v in source_audit.REGISTERED.values()}
    out = source_audit.audit(CFG, env=env, probes=[])
    assert out["summary"]["registered_missing"] == 0
    assert not any(a["code"] == "REGISTERED_CREDENTIALS_MISSING" for a in out["alerts"])


class FakeResp:
    def __init__(self, status=200, chunks=(b"ok",)):
        self.status_code = status
        self._chunks = iter(chunks)
        self.closed = False

    def iter_content(self, n):
        return self._chunks

    def close(self):
        self.closed = True


def test_probe_keyless_records_only_facts():
    rows = source_audit.probe_keyless(fetcher=lambda u: FakeResp(200, (b"hello",)))
    assert len(rows) == len(source_audit.KEYLESS_PROBE_URLS)
    assert all(r["http_status"] == 200 and r["bytes_read"] == 5 for r in rows)
    assert all(r["error_type"] is None for r in rows)
    # 不得把页面内容写进事实
    assert "hello" not in json.dumps(rows, ensure_ascii=False)


def test_probe_failure_is_missing_not_inference():
    def boom(u):
        raise ConnectionError("secret-in-url-should-not-leak")
    rows = source_audit.probe_keyless(fetcher=boom)
    assert all(r["http_status"] == source_audit.MISSING for r in rows)
    assert all(r["error_type"] == "ConnectionError" for r in rows)
    assert "secret-in-url-should-not-leak" not in json.dumps(rows, ensure_ascii=False)
    assert all("不推断" in r["note"] for r in rows)


def test_audit_failure_still_writes_fact_file_and_never_blocks(tmp_path):
    out = tmp_path / "source_dependency.json"
    with pytest.raises(SystemExit) as e:
        source_audit.main(["--config", str(tmp_path / "missing.yaml"),
                           "--json", str(out)])
    assert e.value.code == 0, "审计失败不得阻断采集"
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["status"] == "AUDIT_FAILED"
    assert "不推断" in d["alerts"][0]["message"]
    assert d["registered"] == [] and d["keyless"] == []


def test_atomic_write_leaves_no_tmp(tmp_path):
    p = tmp_path / "source_dependency.json"
    source_audit._atomic_write(p, {"x": 1})
    assert json.loads(p.read_text(encoding="utf-8")) == {"x": 1}
    assert not list(tmp_path.glob(".*.tmp"))


# ---------------------------------------------------------------- delivery_gaps

def _ledger(p: Path, rows: list[dict]) -> Path:
    p.write_text(json.dumps({"deliveries": rows}, ensure_ascii=False), encoding="utf-8")
    return p


def _now_on(date: str, hh: int, mm: int = 0) -> datetime:
    """构造北京 hh:mm 对应的 UTC datetime。"""
    bj = datetime.strptime(date, "%Y-%m-%d").replace(
        hour=hh, minute=mm, tzinfo=timezone(timedelta(hours=8)))
    return bj.astimezone(timezone.utc)


def test_not_due_slots_are_not_reported(tmp_path):
    # 北京 06:00：slot 1 目标 07:00 尚未到点，四个班次都不该被报成漏发
    p = _ledger(tmp_path / "l.json", [])
    out = delivery_gaps.check("2026-11-08", p, _now_on("2026-11-08", 6))
    assert out["missing_slots"] == []
    assert all(s["status"] == "NOT_DUE" for s in out["slots"])


def test_due_missing_slot_is_reported_without_auto_dispatch(tmp_path):
    p = _ledger(tmp_path / "l.json", [])
    out = delivery_gaps.check("2026-11-08", p, _now_on("2026-11-08", 12))
    assert out["missing_slots"] == ["1", "2"]
    assert out["suggested_dispatch"] == [
        {"ref": "main", "inputs": {"slot": "1"}},
        {"ref": "main", "inputs": {"slot": "2"}},
    ]
    # 只给建议，绝不触发；台账未被改写
    assert json.loads(p.read_text(encoding="utf-8")) == {"deliveries": []}


def test_delivered_slot_not_reported(tmp_path):
    p = _ledger(tmp_path / "l.json", [
        {"date": "2026-11-08", "slot": "1", "status": "sent",
         "message_ids": [547, 548], "picks_count": 7, "chat_id": "-100"}])
    out = delivery_gaps.check("2026-11-08", p, _now_on("2026-11-08", 12))
    assert out["missing_slots"] == ["2"]
    assert out["slots"][0]["status"] == "DELIVERED"


def test_zero_pick_sent_row_counts_as_not_delivered(tmp_path):
    p = _ledger(tmp_path / "l.json", [
        {"date": "2026-11-08", "slot": "1", "status": "sent",
         "message_ids": [1], "picks_count": 0, "chat_id": "-100"}])
    out = delivery_gaps.check("2026-11-08", p, _now_on("2026-11-08", 12))
    assert "1" in out["missing_slots"]


def test_empty_message_ids_counts_as_not_delivered(tmp_path):
    p = _ledger(tmp_path / "l.json", [
        {"date": "2026-11-08", "slot": "1", "status": "sent",
         "message_ids": [], "picks_count": 7, "chat_id": "-100"}])
    out = delivery_gaps.check("2026-11-08", p, _now_on("2026-11-08", 12))
    assert "1" in out["missing_slots"]


def test_off_schedule_delivery_is_not_wrapped_as_normal(tmp_path):
    # 目标 22:00 的 slot 4，在北京 12:00 时台账已有记录（历史凌晨误发场景）
    p = _ledger(tmp_path / "l.json", [
        {"date": "2026-11-08", "slot": "4", "status": "sent",
         "message_ids": [545, 546], "picks_count": 7, "chat_id": "-100"}])
    out = delivery_gaps.check("2026-11-08", p, _now_on("2026-11-08", 12))
    s4 = next(s for s in out["slots"] if s["slot"] == "4")
    assert s4["status"] == "DELIVERED_BEFORE_TARGET"
    assert out["off_schedule_slots"] == ["4"]
    assert any(a["code"] == "OFF_SCHEDULE_DELIVERY" for a in out["alerts"])
    assert "不推断" in out["alerts"][0]["message"]
    assert "4" not in out["missing_slots"]


def test_missing_ledger_does_not_invent_records(tmp_path):
    p = tmp_path / "nope.json"
    out = delivery_gaps.check("2026-11-08", p, _now_on("2026-11-08", 23))
    assert out["missing_slots"] == ["1", "2", "3", "4"]
    assert "不补造" in out["note"]
    assert not p.exists()


def test_invalid_date_is_handled_as_fact():
    out = delivery_gaps.check("not-a-date", Path("/nonexistent"), _now_on("2026-11-08", 23))
    assert all(s["status"] == "INVALID_DATE" for s in out["slots"])
    assert out["missing_slots"] == []
