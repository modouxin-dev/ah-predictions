#!/usr/bin/env python3
"""事实一致性闸门（fail-closed）。

用法：
    python scripts/check_grounding.py                 # 默认当天 UTC 日报
    python scripts/check_grounding.py <报告JSON路径>

未通过时退出码 1；调用方必须停止发送，不允许降级放行。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ah import grounding  # noqa: E402
from ah.render_report import report_paths  # noqa: E402


def main() -> None:
    if len(sys.argv) > 1:
        report = sys.argv[1]
    else:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        report = str(report_paths(date)[0])
    grounding.main(["--report", report])


if __name__ == "__main__":
    main()
