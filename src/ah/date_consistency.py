"""A2：编排日期与采集入口日期的一致性事实（只读，不绕过保护、不改 daily.py）。

背景：daily.yml 用 `resolve_slot()` 按「cron 目标时刻」解析 AH_DATE（补偿 GitHub cron
延迟跨日）；而 `daily.py` 的入口校验用**运行时刻**算 allowed = {UTC 今天, 北京今天}。
两者在极端延迟下可能分叉，此时 `daily.py` 的 parser.error 是**防误采历史数据的保护**，
不应被绕过。本模块只把分叉变成可观测事实并落盘，不推断原因、不自动改日期、不阻断。
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import DATA

MISSING = "未取得"
BJ = timezone(timedelta(hours=8))

NOTE = (
    "只比对 AH_DATE 与采集入口的 allowed 口径；分叉时 daily.py 的日期校验是保护性拒绝，"
    "本模块不绕过、不自动改日期、不推断延迟原因、不补造赛程或盘口。"
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_date(s: str | None):
    if not s or s == MISSING:
        return None
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def allowed_dates(now: datetime) -> list[str]:
    """复刻 daily.py:532 的口径（按运行时刻），只用于比对，不修改其实现。"""
    return sorted({now.date().isoformat(), (now + timedelta(hours=8)).date().isoformat()})


def check(ah_date: str = "", now: datetime | None = None,
          slot: str = "") -> dict:
    now = now or datetime.now(timezone.utc)
    allowed = allowed_dates(now)
    d = _parse_date(ah_date)
    out = {
        "schema": 1,
        "checked_at": _now(),
        "evidence_grade": "【已验证】本地时间与环境变量布尔事实",
        "ah_date": ah_date or MISSING,
        "ah_date_valid": d is not None,
        "allowed_by_entry_check": allowed,
        "slot": slot or MISSING,
        "consistent": bool(d and d.isoformat() in allowed),
        "status": "OK",
        "alerts": [],
        "note": NOTE,
        "unverified": [
            "cron 实际触发时刻与延迟（本模块不读运行日志）",
            "采集是否因日期校验被拒绝（只给一致性事实，不读 job 结论）",
            "Telegram 接收端（本命令不发送任何消息）",
        ],
    }
    if not d:
        out["status"] = "AH_DATE_MISSING_OR_INVALID"
        out["alerts"].append({"code": "AH_DATE_MISSING_OR_INVALID", "severity": "warning",
                              "message": "AH_DATE 未取得或格式无效；只记录事实，不推断原因", "evidence": {}})
    elif not out["consistent"]:
        out["status"] = "DATE_DIVERGENCE"
        out["alerts"].append({
            "code": "DATE_DIVERGENCE", "severity": "warning",
            "message": (f"AH_DATE={d.isoformat()} 不在采集入口 allowed={allowed} 内；"
                        "daily.py 的日期校验会拒绝采集（保护性行为，不绕过），"
                        "只记录事实，不推断延迟原因"),
            "evidence": {"ah_date": d.isoformat(), "allowed": allowed},
        })
    else:
        out["alerts"].append({"code": "NO_ALERT", "severity": "info",
                              "message": "编排日期与采集入口口径一致", "evidence": {}})
    return out


def _atomic_write(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix="." + p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, p); tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="编排日期一致性事实（只读，不绕过 daily.py 保护）")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    out = check(os.environ.get("AH_DATE", ""), slot=os.environ.get("AH_SLOT", ""))
    if a.out:
        _atomic_write(Path(a.out), out)
    for x in out["alerts"]:
        if x["severity"] != "info":
            print(f"::{x['severity']}::{x['code']} {x['message']}")
    print(f"DATE_CONSISTENCY status={out['status']} ah_date={out['ah_date']} "
          f"allowed={','.join(out['allowed_by_entry_check'])}")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
