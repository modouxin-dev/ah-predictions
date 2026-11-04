"""football-data.co.uk 数据覆盖表 -> research/output/coverage.md"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

import fd_data as fd

OUT = Path(__file__).resolve().parent / "outputs"


def nn(df, c):
    return int(df[c].notna().sum()) if c in df else 0


def main():
    fd.download()
    rows = []
    for div, cn in fd.MAIN.items():
        df = fd.load_main(div)
        for s, g in df.groupby("season"):
            rows.append(dict(联赛=f"{cn} {div}", 赛季=s, 场次=len(g), Pin1X2开=nn(g, "PSH"), Pin1X2收=nn(g, "PSCH"),
                             AH盘口开=nn(g, "AHh"), PinAH开=nn(g, "PAHH"), AH盘口收=nn(g, "AHCh"), PinAH收=nn(g, "PCAHH")))
    for code, cn in fd.NEW.items():
        df = fd.load_new(code)
        for s, g in df.groupby("season"):
            rows.append(dict(联赛=f"{cn} {code}", 赛季=s, 场次=len(g), Pin1X2开=0, Pin1X2收=nn(g, "PSCH"),
                             AH盘口开=0, PinAH开=0, AH盘口收=0, PinAH收=0))
    t = pd.DataFrame(rows)
    OUT.mkdir(exist_ok=True)
    note = ("# football-data.co.uk 覆盖表（由 research/coverage.py 生成）\n\n"
            "- “开”= football-data 的赛前采集价（周五/周二采集，非真正开盘）；“收”= 收盘价。\n"
            "- AH 盘口列 AHh/AHCh 为主队视角让球；PinAH = Pinnacle 主/客亚盘赔率 PAHH/PCAHH。\n"
            "- 新联赛文件（new/*.csv）只有 Pinnacle 1X2 **收盘**价，无亚盘、无开盘。\n"
            "- 已实测不存在：new/KOR.csv（重定向到 NOR）、new/AUS.csv（300）、new/SAU.csv、new/QAT.csv（404）；"
            "即韩K、澳超、沙特、卡塔尔、亚冠在 football-data.co.uk 均无数据。\n\n")
    (OUT / "coverage.md").write_text(note + t.to_markdown(index=False) + "\n", encoding="utf-8")
    print(t.to_string(index=False))


if __name__ == "__main__":
    main()
