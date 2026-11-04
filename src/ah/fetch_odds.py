"""抓取亚盘/欧赔/大小球（titan007 goal3.xml，默认公司）。

已验证可用(2026-11-04)：https://live.titan007.com/vbsxml/goal3.xml?r=007<随机>
<m> 字段(逗号分隔，经验映射)：0 比赛id, 2 亚盘(正=主让, 负=主受让), 3 主水, 4 客水,
6/7/8 欧赔 主/平/客, 10 大小球盘口, 11 大球水, 12 小球水, 倒数第2 初盘亚盘, 倒数第1 初盘大小。
水位为港盘(HK)。
TODO: 多公司盘口及变盘历史（vip.titan007.com / zq.titan007.com 从本机不可达）；
      1x2d.titan007.com/<id>.js 可取多家欧赔及变化，可作为补充（未解析）。
TODO: 备用源 oddsportal（页面可达但数据由 JS 动态加载，需浏览器/逆向）。
"""
from __future__ import annotations

import json
import random
import re
import sys

import requests

URL = "https://live.titan007.com/vbsxml/goal3.xml?r=007{}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
           "Referer": "https://live.titan007.com/"}


def _f(x: str):
    try:
        return float(x)
    except ValueError:
        return None


def parse(xml: str) -> dict[str, dict]:
    out = {}
    for m in re.findall(r"<m>([^<]*)</m>", xml):
        x = m.split(",")
        if len(x) < 15:
            continue
        out[f"titan007-{x[0]}"] = {
            "ah_home_line": _f(x[2]), "ah_home_water": _f(x[3]), "ah_away_water": _f(x[4]),
            "eu_home": _f(x[6]), "eu_draw": _f(x[7]), "eu_away": _f(x[8]),
            "ou_line": _f(x[10]), "ou_over": _f(x[11]), "ou_under": _f(x[12]),
            "ah_home_line_open": _f(x[-2]), "ou_line_open": _f(x[-1]),
        }
    return out


def fetch() -> dict[str, dict]:
    from . import source_status
    try:
        r = requests.get(URL.format(random.randint(1000, 9999)), headers=HEADERS, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        source_status.record("titan007", False, f"盘口 {type(e).__name__}: {e}")
        raise
    r.encoding = "utf-8"
    out = parse(r.text)
    source_status.record("titan007", bool(out), None if out else "盘口：返回内容无法解析")
    return out


def cross_check_odds_api(sport: str, client=None) -> dict[str, dict]:
    """The Odds API 多公司亚盘交叉核对（优先级：API-Football 主 → The Odds API → titan007 备用）。
    返回 {"oddsapi-<event_id>": {home, away, commence, consensus, books}}；失败/无 key/预算不足返回 {}。"""
    from . import odds_api
    try:
        c = client or odds_api.OddsAPI()
        if not c.enabled:
            return {}
        events = c.odds(sport, markets="spreads")
    except Exception as e:  # noqa: BLE001  优雅降级
        print(f"[odds_api] skip: {e}", file=sys.stderr)
        return {}
    out = {}
    for ev in events:
        books = odds_api.normalize_spreads(ev)
        out[f"oddsapi-{ev.get('id')}"] = {"home": ev.get("home_team"), "away": ev.get("away_team"),
                                          "commence": ev.get("commence_time"),
                                          "consensus": odds_api.consensus(books), "books": books}
    return out


if __name__ == "__main__":
    json.dump(fetch(), sys.stdout, ensure_ascii=False, indent=1)
