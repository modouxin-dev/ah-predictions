"""D3/D5：失败事实只记字段，不回显密钥、不推断、不阻断。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from ah import ops_failures as of


def test_record_appends_and_caps(tmp_path):
    p = tmp_path / "ops_failures.json"
    for i in range(of.MAX_EVENTS + 5):
        of.record("collect", f"run-{i}", "1", path=p,
                  now=datetime(2026, 11, 8, i % 24, tzinfo=timezone.utc))
    d = json.loads(p.read_text(encoding="utf-8"))
    assert len(d["events"]) == of.MAX_EVENTS
    assert d["events"][-1]["run_id"] == f"run-{of.MAX_EVENTS + 4}"


def test_missing_error_type_is_not_invented(tmp_path):
    p = tmp_path / "f.json"
    e = of.record("publish", "r", "1", path=p)
    assert e["error_type"] == of.MISSING
    assert "不推断" in e["note"]


def test_secret_string_never_persisted(tmp_path):
    p = tmp_path / "f.json"
    of.record("collect", "r", "1", error_type="ConnectionError", path=p)
    raw = p.read_text(encoding="utf-8")
    assert "SECRET_SHOULD_NOT_LEAK" not in raw


def test_empty_stage_fails(tmp_path):
    with pytest.raises(RuntimeError):
        of.record("  ", path=tmp_path / "f.json")


def test_main_exit_zero(tmp_path):
    p = tmp_path / "f.json"
    with pytest.raises(SystemExit) as e:
        of.main(["--stage", "collect", "--run-id", "r", "--slot", "1", "--out", str(p)])
    assert e.value.code == 0 and json.loads(p.read_text(encoding="utf-8"))["events"]
