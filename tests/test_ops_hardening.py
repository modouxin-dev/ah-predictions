"""编排与对账层加固回归测试（冻结清单外，纯本地、无网络、无真实推送）。

锁定 2026-11-08 全面加固的四类契约：
1. slot 输入白名单，非法值 fail-closed；
2. 台账原子写 + 排他锁，并发不丢记录；
3. 报告来源写入台账（采集失败沿用 main 报告时必须诚实标注）；
4. 所有 workflow 都有并发边界与超时；build_site 在 3.10/3.12 均可导入。
"""
from __future__ import annotations

import json
import multiprocessing as mp
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from ah import build_site, delivery


ROOT = Path(__file__).resolve().parents[1]


def test_validate_slot_whitelist():
    assert delivery.validate_slot("1") == "1"
    assert delivery.validate_slot(" 3 ") == "3"
    assert delivery.validate_slot("", from_dispatch=True) == "manual"
    for bad in ("0", "5", "9", "hack", "1; rm -rf /", "../../etc"):
        with pytest.raises(RuntimeError, match="非法 slot"):
            delivery.validate_slot(bad, from_dispatch=True)


def test_resolve_slot_rejects_unknown_dispatch_slot():
    now = datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc)
    assert delivery.resolve_slot("workflow_dispatch", "", "3", now) == ("3", "2026-11-08", 0)
    with pytest.raises(RuntimeError, match="非法 slot"):
        delivery.resolve_slot("workflow_dispatch", "", "9", now)


def test_save_ledger_is_atomic_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "telegram_delivery.json"
    payload = {"schema": 1, "deliveries": [{"date": "2026-11-08", "message_ids": [1]}]}
    delivery.save_ledger(payload, p)
    assert json.loads(p.read_text(encoding="utf-8")) == payload
    assert not list(tmp_path.glob("." + p.name + ".*.tmp"))


def test_record_sent_writes_provenance_when_present(tmp_path, monkeypatch):
    p = tmp_path / "l.json"
    monkeypatch.setenv("AH_REPORT_SOURCE", "main-checkout")
    monkeypatch.setenv("AH_COLLECT_RESULT", "failure")
    row = delivery.record_sent("2026-11-08", "sha", "-100", [547, 548],
                               picks_count=7, path=p, slot="1")
    assert row["report_source"] == "main-checkout"
    assert row["collect_result"] == "failure"
    assert row["message_ids"] == [547, 548]
    assert json.loads(p.read_text(encoding="utf-8"))["deliveries"][0]["report_source"] == "main-checkout"


def test_record_sent_keeps_legacy_shape_without_provenance(tmp_path, monkeypatch):
    monkeypatch.delenv("AH_REPORT_SOURCE", raising=False)
    monkeypatch.delenv("AH_COLLECT_RESULT", raising=False)
    p = tmp_path / "l.json"
    row = delivery.record_sent("2026-11-08", "sha", "-100", [9], path=p)
    assert "report_source" not in row
    assert "collect_result" not in row


def test_concurrent_record_sent_does_not_lose_records(tmp_path):
    p = tmp_path / "l.json"

    def worker(i: int) -> None:
        delivery.record_sent("2026-11-08", "sha", "-100", [i],
                             picks_count=1, path=p, slot=str(i))

    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=worker, args=(i,)) for i in (1, 2)]
    for x in procs:
        x.start()
    for x in procs:
        x.join(20)
        assert x.exitcode == 0, "并发写进程异常退出"
    rows = json.loads(p.read_text(encoding="utf-8"))["deliveries"]
    assert len(rows) == 2, "排他锁失效，出现丢失更新"
    assert sorted(r["message_ids"][0] for r in rows) == [1, 2]


def test_chat_hash_does_not_echo_chat_id():
    chat = "-1004312560482"
    h = delivery._chat_hash(chat)
    assert len(h) == 12
    assert h != chat and chat not in h


def test_build_site_page_renders_on_supported_python():
    html = build_site.page("标题", "<p>正文</p>", cur="index.html")
    assert "class=\"cur\"" in html
    assert "<p>正文</p>" in html
    assert "先知" in html


def test_all_workflows_have_concurrency_and_timeout():
    for p in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert "concurrency" in doc, f"{p.name} 缺少 workflow 级并发边界"
        jobs = doc.get("jobs") or {}
        assert jobs, f"{p.name} 没有 job"
        for name, job in jobs.items():
            assert "timeout-minutes" in job, f"{p.name}/{name} 缺少超时保护"
