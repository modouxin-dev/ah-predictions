"""换号验证：只记录事实、不回显 key、不写断言。"""
import json
from datetime import datetime, timezone

from ah import credential_verify as cv


def _patch(monkeypatch, probe_status=True, rows=3, status_file=None):
    monkeypatch.setenv("API_FOOTBALL_KEY", "SECRET_VALUE_MUST_NOT_APPEAR")
    monkeypatch.delenv("AH_API_FOOTBALL_DISABLED", raising=False)
    monkeypatch.setattr(cv.source_probe, "run", lambda: {
        "sources": [{"source": "api_football", "status_checked": probe_status,
                     "http_status": 200 if probe_status else None,
                     "account_active": True if probe_status else None,
                     "subscription_active": True if probe_status else None,
                     "quota": {"used": 0, "remaining": 100, "limit": 100},
                     "error_type": None if probe_status else "ApiFootballError",
                     "note": "n/a"},
                    {"source": "oddspapi", "account_checked": True}]})
    monkeypatch.setattr(cv.fetch_fixtures, "fetch_api",
                        lambda day: [{"status": "NS"}] * rows)
    if status_file is not None:
        monkeypatch.setenv("AH_SOURCE_STATUS_FILE", str(status_file))
    else:
        monkeypatch.setenv("AH_SOURCE_STATUS_FILE", "/nonexistent/ah-status.json")


def test_verify_never_echoes_secret_and_has_no_recovery_claim(monkeypatch, tmp_path):
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"sources": {"api_football": {
        "last_ok_at": "2026-11-09T02:10:00Z", "last_error": "",
        "quota": {"remaining": 99}}}, "updated_at": "2026-11-09T02:10:00Z"}), encoding="utf-8")
    _patch(monkeypatch, status_file=status)
    o = cv.verify(now=datetime(2026, 11, 9, 2, 10, tzinfo=timezone.utc), days=2)
    blob = json.dumps(o, ensure_ascii=False)
    assert "SECRET_VALUE_MUST_NOT_APPEAR" not in blob
    assert o["secret"]["api_football_key_injected"] is True
    assert o["secret"]["kill_env_set"] is False
    assert o["status_probe"]["account_active"] is True
    assert o["runtime_status_snapshot"]["suspended_error_present"] is False
    assert sum(v["rows"] for v in o["fixture_probe"]["days"].values()) == 6
    assert "已恢复" in o["assertions"] and "不包含任何" in o["assertions"]
    assert "不回显" in o["secret"]["note"]


def test_verify_reports_failure_as_fact_not_inference(monkeypatch, tmp_path):
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"sources": {"api_football": {
        "last_ok_at": "2026-11-04T12:36:23Z",
        "last_error": "status errors: {\"access\": \"Your account is suspended\"}"}}}), encoding="utf-8")
    _patch(monkeypatch, probe_status=False, rows=0, status_file=status)
    o = cv.verify(now=datetime(2026, 11, 9, 2, 10, tzinfo=timezone.utc), days=1)
    assert o["status_probe"]["status_checked"] is False
    assert o["runtime_status_snapshot"]["suspended_error_present"] is True
    assert "不推断" in o["fixture_probe"]["note"] or "不推断" in o["status_probe"]["note"]


def test_cmd_writes_atomic_json(monkeypatch, tmp_path):
    _patch(monkeypatch)
    out = tmp_path / "cred.json"
    o = cv.cmd(out=out, days=1)
    assert json.loads(out.read_text(encoding="utf-8")) == o
    assert not list(tmp_path.glob(".cred-*"))
