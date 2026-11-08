"""工程层：静默异常降级事实只记类型、不回显内容、不阻断。"""
from __future__ import annotations

import json
from pathlib import Path

from ah import degrade


def test_record_is_deduped_per_process(tmp_path):
    degrade.reset_seen()
    p = tmp_path / "degradation_facts.json"
    a = degrade.record("m", "s", ValueError("secret-url?apiKey=XYZ"), p)
    b = degrade.record("m", "s", ValueError("other"), p)
    assert a and b is None
    d = json.loads(p.read_text(encoding="utf-8"))
    assert len(d["events"]) == 1 and d["events"][0]["error_type"] == "ValueError"
    assert "apiKey" not in p.read_text(encoding="utf-8") and "secret-url" not in p.read_text(encoding="utf-8")


def test_missing_exception_is_not_invented(tmp_path):
    degrade.reset_seen()
    p = tmp_path / "f.json"
    e = degrade.record("m", "s", None, p)
    assert e["error_type"] == degrade.MISSING
    assert "不推断" in json.loads(p.read_text(encoding="utf-8"))["note"]


def test_write_failure_never_raises(tmp_path):
    degrade.reset_seen()
    # 指向一个文件而非目录，mkdir 会失败；record 必须吞掉异常
    blocker = tmp_path / "block"; blocker.write_text("x", encoding="utf-8")
    assert degrade.record("m", "s", OSError(), blocker / "f.json") is None


def test_caps_events(tmp_path):
    degrade.reset_seen()
    p = tmp_path / "f.json"
    for i in range(degrade.MAX_EVENTS + 20):
        degrade.record("m", f"s{i}", None, p)
    assert len(json.loads(p.read_text(encoding="utf-8"))["events"]) == degrade.MAX_EVENTS


def test_all_provider_sites_are_instrumented():
    """静态契约：深度检查列出的 17 处静默异常，逐处都接入了共用降级函数。"""
    from pathlib import Path
    SITES = [
        ("src/ah/api_football.py", "config_read"),
        ("src/ah/audit/paper_trades.py", "api_football_score"),
        ("src/ah/consensus.py", "fivedollar_consensus"),
        ("src/ah/form_history.py", "infersports_day"),
        ("src/ah/odds_snapshots.py", "config_read"),
        ("src/ah/odds_snapshots.py", "infersports_events"),
        ("src/ah/oddspapi.py", "backoff_config"),
        ("src/ah/publish_telegram.py", "default_chat_config"),
        ("src/ah/scrapers/lineups.py", "api_football_fixture"),
        ("src/ah/settle.py", "fd_score_second_source"),
        ("src/ah/settle.py", "api_football_score"),
        ("src/ah/source_audit.py", "workflow_file_read"),
        ("src/ah/source_audit.py", "keyless_probe_close"),
        ("src/ah/source_policy.py", "client_file_read"),
        ("src/ah/source_status.py", "redact_url"),
        ("src/ah/source_status.py", "status_write"),
        ("src/ah/tg_bot.py", "state_file_read"),
    ]
    assert len(SITES) == 17
    for f, stage in SITES:
        txt = Path(f).read_text(encoding="utf-8")
        assert "_degrade_fact(" in txt and f'"{stage}"' in txt, (f, stage)
        # 降级调用必须在 except 的紧邻下一行，且原返回语句仍在
        lines = txt.splitlines()
        hit = [i for i, l in enumerate(lines) if f'"{stage}"' in l and "_degrade_fact(" in l]
        assert hit and lines[hit[0] - 1].lstrip().startswith("except "), (f, stage)
