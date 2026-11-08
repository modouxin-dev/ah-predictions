"""A2：日期一致性事实，不绕过 daily.py 保护、不推断原因、不阻断。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from ah import date_consistency as dc


def test_consistent_date_is_ok():
    now = datetime(2026, 11, 8, 4, tzinfo=timezone.utc)   # 北京 11-08 12:00
    out = dc.check("2026-11-08", now, slot="1")
    assert out["status"] == "OK" and out["consistent"] is True
    assert "2026-11-08" in out["allowed_by_entry_check"]


def test_divergence_is_fact_not_bypass():
    now = datetime(2026, 11, 10, 4, tzinfo=timezone.utc)
    out = dc.check("2026-11-08", now, slot="1")
    assert out["status"] == "DATE_DIVERGENCE"
    msg = " ".join(a["message"] for a in out["alerts"])
    assert "不绕过" in msg and "保护性" in msg and "不推断" in msg
    assert "2026-11-08" not in out["allowed_by_entry_check"]


def test_missing_or_invalid_date_is_not_invented():
    for v in ("", "bad", None):
        out = dc.check(v, datetime(2026, 11, 8, 4, tzinfo=timezone.utc))
        assert out["ah_date_valid"] is False
        assert out["status"] == "AH_DATE_MISSING_OR_INVALID"


def test_allowed_dates_utc_midnight_edge():
    # UTC 23:30 -> 北京已是次日
    now = datetime(2026, 11, 7, 23, 30, tzinfo=timezone.utc)
    assert dc.allowed_dates(now) == ["2026-11-07", "2026-11-08"]


def test_main_writes_fact_and_never_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("AH_DATE", "2026-11-08")
    monkeypatch.setenv("AH_SLOT", "1")
    out = tmp_path / "date_consistency.json"
    with pytest.raises(SystemExit) as e:
        dc.main(["--out", str(out)])
    assert e.value.code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["schema"] == 1


def test_note_forbids_inference():
    assert "不推断" in dc.NOTE and "不绕过" in dc.NOTE
