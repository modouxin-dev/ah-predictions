"""D3/D5：结构化失败事实落盘（纯旁路，不改任何 provider，不推断原因）。

编排层在 job/step 失败时调用一次，把「哪个阶段、哪个 run、哪个 slot、什么时间」
写成事实。读不到异常类型就写「未取得」，绝不回显可能含密钥的 URL/响应体，
不推断失败原因、不补造赛程盘口比分、不发 Telegram、不阻断任何流程。
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import DATA

MISSING = "未取得"
MAX_EVENTS = 200

NOTE = (
    "只记录阶段名、UTC 时刻、run_id、slot 与异常类型（读不到写『未取得』）；"
    "不回显密钥或 URL，不推断失败原因，不补造任何数据，不发消息，不阻断流程。"
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix="." + p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def record(stage: str, run_id: str = "", slot: str = "", error_type: str = "",
           path: Path | None = None, now: datetime | None = None) -> dict:
    if not stage or not stage.strip():
        raise RuntimeError("stage 不能为空")
    p = Path(path) if path else DATA / "ops_failures.json"
    d = _load(p)
    event = {
        "stage": stage.strip(),
        "at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": str(run_id or MISSING).strip() or MISSING,
        "slot": str(slot or MISSING).strip() or MISSING,
        "error_type": str(error_type or MISSING).strip() or MISSING,
        "note": "仅记录事实；不推断原因、不回显密钥或 URL",
    }
    d.setdefault("events", []).append(event)
    d["events"] = d["events"][-MAX_EVENTS:]
    d["schema"] = 1
    d["updated_at"] = _now()
    d["note"] = NOTE
    _atomic_write(p, d)
    return event


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="记录一条结构化失败事实（不推断、不阻断）")
    ap.add_argument("--stage", required=True)
    ap.add_argument("--run-id", default="")
    ap.add_argument("--slot", default="")
    ap.add_argument("--error-type", default="")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    e = record(a.stage, a.run_id, a.slot, a.error_type,
               Path(a.out) if a.out else None)
    print(f"OPS_FAILURE stage={e['stage']} at={e['at']} slot={e['slot']} "
          f"error_type={e['error_type']}")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
