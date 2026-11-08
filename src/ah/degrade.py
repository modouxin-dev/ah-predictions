"""provider 静默降级 → 结构化事实（工程层，不改任何采集/评分/核验算法）。

要解决的问题
------------
深度检查扫到 17 处 `except Exception: pass / return None / continue`，失败原因完全消失，
结算积压、数据源降级、配置读失败都无从追溯。本模块提供一个**共用的、零侵入的**
落盘函数：只记「哪个模块、哪个阶段、什么异常类型、什么时间」，不回显异常消息、
URL、响应体或密钥，不推断原因，不阻断原流程。

设计要点
--------
- 每个进程内按 (source, stage, error_type) 去重，避免高频失败刷爆文件；
- 原子写入 `data/degradation_facts.json`，事件上限 200 条；
- 自身永不抛异常（写盘失败也要静默），原 `except` 块的返回值/continue 完全不变；
- 缺异常对象时 `error_type` 写「未取得」，不猜。
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import DATA

MISSING = "未取得"
MAX_EVENTS = 200
_SEEN: set[tuple[str, str, str]] = set()

NOTE = ("只记模块/阶段/UTC 时刻/异常类型；不回显异常消息、URL、响应体或密钥；"
        "不推断失败原因，不补造任何赛程盘口水位比分，不阻断原流程，不发 Telegram。")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def error_type(exc) -> str:
    if exc is None:
        return MISSING
    return type(exc).__name__


def _load(p: Path) -> dict:
    if not p.exists():
        return {"schema": 1, "events": []}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("events"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"schema": 1, "events": []}


def _atomic_write(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def record(source: str, stage: str, exc=None, path: Path | None = None) -> dict | None:
    """记录一条降级事实。**永不抛异常**；同键每进程只记一次。"""
    src = str(source or MISSING).strip() or MISSING
    stg = str(stage or MISSING).strip() or MISSING
    et = error_type(exc)
    key = (src, stg, et)
    if key in _SEEN:
        return None
    _SEEN.add(key)
    event = {"source": src, "stage": stg, "error_type": et, "at": _now(),
             "note": "只记录事实，不推断原因"}
    try:
        p = Path(path) if path else DATA / "degradation_facts.json"
        d = _load(p)
        d.setdefault("events", []).append(event)
        d["events"] = d["events"][-MAX_EVENTS:]
        d["schema"] = 1
        d["updated_at"] = _now()
        d["note"] = NOTE
        _atomic_write(p, d)
    except Exception:  # noqa: BLE001 —— 事实落盘本身失败也绝不阻断业务
        return None
    return event


def reset_seen() -> None:
    """仅供测试：清空进程内去重集合。"""
    _SEEN.clear()
