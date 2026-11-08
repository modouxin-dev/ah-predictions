"""pytest 全局夹具：把测试硬依赖的静态历史数据做成自包含基线。

背景：66 个测试里有一批直接读仓库的 reports/** 与 data/**（真实历史快照）。
浅 checkout、清理历史数据、或只取源码跑测试时会在 collection 阶段直接报
FileNotFoundError。这里在 pytest_sessionstart 把 tests/fixtures/baseline/ 下的
静态快照**只补齐缺失路径**——已有文件一律不覆盖，不改写任何运行时数据，
不生成任何赛程、盘口、水位、比分或推荐。

基线是 2026-11-08 main 上已有文件的原样副本，仅供测试，不参与运行时采集/推送。
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import os
import tempfile

# 测试中的客户端调用不得改写仓库里的 data/source_status.json
os.environ["AH_SOURCE_STATUS_FILE"] = str(Path(tempfile.mkdtemp()) / "source_status.json")

BASELINE = Path(__file__).resolve().parent / "fixtures" / "baseline"


def pytest_sessionstart(session):
    """只补缺失文件；目录不存在时静默跳过（完整 checkout 本就自带这些数据）。"""
    if not BASELINE.is_dir():
        return
    restored = 0
    for src in BASELINE.rglob("*"):
        if not src.is_file():
            continue
        dst = ROOT / src.relative_to(BASELINE)
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        restored += 1
    if restored:
        print(f"[conftest] 已从 tests/fixtures/baseline 补齐 {restored} 个测试基线文件（不覆盖已有文件）")
