"""C 阶段：数据源开关与刷新编排的测试（不触碰模型算法）。"""
import json

import pytest
import yaml

from ah import CONFIG, api_football, daily, source_probe


def test_api_football_switch_is_enabled_in_config():
    """静态事实：配置已重新启用；不在此断言新 key 有效（需运行时代理自检）。"""
    cfg = yaml.safe_load((CONFIG / "sources.yaml").read_text(encoding="utf-8"))
    assert cfg["api_football"]["enabled"] is True
    assert cfg["api_football"]["kill_env"] == "AH_API_FOOTBALL_DISABLED"


def test_kill_switch_no_longer_blocked_by_yaml(tmp_path, monkeypatch):
    monkeypatch.delenv(api_football.KILL_ENV, raising=False)
    monkeypatch.setattr(api_football, "DISABLED_FLAG", tmp_path / "no_flag.json")
    assert api_football.kill_switch_reason() is None
    c = api_football.Client(key="dummy-not-a-real-key")
    assert c.enabled is True
    # 防断言：enabled=True 只代表「开关未阻断」，不代表密钥有效或数据充足
    assert "dummy-not-a-real-key" not in repr(c)


def test_kill_env_still_works_as_emergency_switch(tmp_path, monkeypatch):
    monkeypatch.setenv(api_football.KILL_ENV, "1")
    monkeypatch.setattr(api_football, "DISABLED_FLAG", tmp_path / "no_flag.json")
    assert "AH_API_FOOTBALL_DISABLED" in (api_football.kill_switch_reason() or "")


def test_refresh_overwrites_existing_report(tmp_path, monkeypatch):
    """编排层：--refresh 显式覆盖；不带 --refresh 保持原样。不改动 build_report 算法。"""
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date().isoformat()
    monkeypatch.setattr(daily, "report_paths", lambda d: (tmp_path / "d.json", tmp_path / "d.md"))
    (tmp_path / "d.json").write_text('{"old": true}', encoding="utf-8")

    fake = {"date": today, "generated_at": today + "T13:00:00Z",
            "picks": [], "watched": [], "watchlist": [], "collection_errors": [],
            "collection_notes": [], "odds_format": "HK", "odds_source_note": "",
            "not_covered": ""}
    monkeypatch.setattr(daily, "build_report", lambda *_a, **_k: fake)

    daily.main(["--date", today, "--refresh"])
    assert json.loads((tmp_path / "d.json").read_text(encoding="utf-8")) == fake


def test_without_refresh_report_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(daily, "report_paths", lambda d: (tmp_path / "d.json", tmp_path / "d.md"))
    (tmp_path / "d.json").write_text('{"old": true}', encoding="utf-8")
    called = []
    monkeypatch.setattr(daily, "build_report", lambda *_a, **_k: called.append(1))
    daily.main(["--date", "2026-11-06"])
    assert called == []
    assert json.loads((tmp_path / "d.json").read_text(encoding="utf-8")) == {"old": True}


def test_probe_never_exposes_key_and_labels_evidence(monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_KEY", "SECRET-DO-NOT-LEAK")
    monkeypatch.delenv("ODDSPAPI_KEY", raising=False)  # 不给真实 key，避免联网
    monkeypatch.setenv(api_football.KILL_ENV, "1")    # 阻断 API-Football 远端调用
    out = source_probe.run()
    blob = json.dumps(out, ensure_ascii=False)
    assert "SECRET-DO-NOT-LEAK" not in blob
    assert out["evidence_grade"] == "【运行时代理自检】"
    assert out["unverified"]
    assert "不包含任何" in out["assertions"]
    assert out["sources"][0]["key_env_present"] is True
    assert out["sources"][0]["status_checked"] is False
    assert out["sources"][1]["account_checked"] is False
