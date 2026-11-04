"""抓取当日赛程（titan007 即时比分数据）。

已验证可用(2026-11-04)：https://live.titan007.com/vbsxml/bfdata_ut.js?r=007<随机>
注意：必须带 ?r= 参数和 Referer，否则连接被拒；时间为北京时间(UTC+8)。
字段(按 ^ 分割，经验映射，可能变动)：0 id, 2 联赛, 5 主队, 8 客队, 11 开球(HH:MM 北京时间),
13 状态(0未开,1上半,2中场,3下半,-1完场,-14推迟...), 14/15 主客比分, 21/22 主客排名。
TODO: 字段映射需定期回归测试；完场比分回填 predictions.csv 供 settle 使用。
"""
from __future__ import annotations

import json
import random
import re
import sys

import requests

URL = "https://live.titan007.com/vbsxml/bfdata_ut.js?r=007{}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
           "Referer": "https://live.titan007.com/"}


def parse(js: str) -> list[dict]:
    out = []
    for m in re.finditer(r'A\[\d+\]="([^"]*)"', js):
        f = m.group(1).split("^")
        if len(f) < 23:
            continue
        out.append({"match_id": f"titan007-{f[0]}", "league": f[2], "home": f[5], "away": f[8],
                    "kickoff_bjt": f[11], "state": f[13], "home_goals": f[14], "away_goals": f[15],
                    "home_rank": f[21], "away_rank": f[22]})
    return out


def fetch() -> list[dict]:
    r = requests.get(URL.format(random.randint(1000, 9999)), headers=HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = "utf-8"
    return parse(r.text)


if __name__ == "__main__":
    json.dump(fetch(), sys.stdout, ensure_ascii=False, indent=1)
