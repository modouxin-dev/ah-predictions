"""编排契约：YAML 可解析、并发/超时、取消策略，以及新增事实步骤存在。"""
from __future__ import annotations

from pathlib import Path

import yaml

WF = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# 涉及 Telegram 发送或 07:00 等待：不取消在途 run，避免漏发
KEEP_RUNNING = {"daily.yml": "daily-report", "publish-recheck.yml": "publish-recheck"}
# 幂等 / 只读任务：取消在途旧 run，避免延迟排队堆积
CANCEL_STALE = {"settle.yml", "source-probe.yml", "collection-check.yml",
                "telegram-preflight.yml", "telegram-group-check.yml",
                "research-backtest.yml", "review.yml", "tests.yml", "pages.yml",
                "lock.yml", "close-snapshots.yml", "rule-lab.yml"}


def _load(name):
    return yaml.safe_load((WF / name).read_text(encoding="utf-8"))


def _on(d):
    # PyYAML 1.1 会把 unquoted `on:` 解析成布尔 True
    return d.get("on") or d.get(True) or {}


def test_all_workflows_parse_and_have_contracts():
    files = sorted(WF.glob("*.yml"))
    assert len(files) >= 11
    for p in files:
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert "concurrency" in d and d["concurrency"].get("group"), p.name
        for job, j in (d.get("jobs") or {}).items():
            assert "timeout-minutes" in j, (p.name, job)


def test_send_workflows_keep_in_progress_runs():
    for name, group in KEEP_RUNNING.items():
        c = _load(name)["concurrency"]
        assert c["group"] == group
        assert c.get("cancel-in-progress") is False, name


def test_idempotent_workflows_cancel_stale_runs():
    for name in CANCEL_STALE:
        assert (WF / name).exists(), name
        assert _load(name)["concurrency"].get("cancel-in-progress") is True, name


def test_daily_has_new_fact_steps():
    d = _load("daily.yml")
    text = "\n".join(
        (s.get("run") or "") + " " + (s.get("name") or "")
        for job in d["jobs"].values() for s in job.get("steps", []))
    for cmd in ("date-consistency", "data-quality", "ops-failures",
                "source-audit", "delivery-gaps", "runfacts mark"):
        assert cmd in text, cmd


def test_review_has_data_quality():
    text = "\n".join((s.get("run") or "")
                     for j in _load("review.yml")["jobs"].values() for s in j["steps"])
    assert "data-quality" in text


def test_lock_workflow_is_manual_and_never_invents_hashes():
    d = _load("lock.yml")
    assert "workflow_dispatch" in _on(d)
    assert d["jobs"]["lock"]["timeout-minutes"] <= 30
    text = "\n".join((s.get("run") or "")
                     for s in d["jobs"]["lock"]["steps"])
    assert "piptools compile" in text and "--generate-hashes" in text
    assert "不伪造" in text or "never invent" in text


def test_round3_data_source_and_model_change_wiring():
    daily = _load("daily.yml")
    dtext = "\n".join((s.get("run") or "") for j in daily["jobs"].values() for s in j["steps"])
    for cmd in ("clv --apply", "source-policy", "data-quality", "date-consistency"):
        assert cmd in dtext, cmd
    settle = _load("settle.yml")
    stext = "\n".join((s.get("run") or "") for j in settle["jobs"].values() for s in j["steps"])
    assert "score-truth" in stext and "source-policy" in stext
    review = _load("review.yml")
    rtext = "\n".join((s.get("run") or "") for j in review["jobs"].values() for s in j["steps"])
    assert "rule-lab" in rtext
    assert (WF / "close-snapshots.yml").exists() and (WF / "rule-lab.yml").exists()
