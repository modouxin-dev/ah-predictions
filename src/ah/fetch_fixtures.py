"""抓取赛程：API-Football 优先（按日期一次请求，过滤 config/leagues.yaml 的 apif 联赛），
失败/无 Key/额度不足时回退 titan007 即时比分数据。

用法：python -m ah.fetch_fixtures [--date YYYY-MM-DD] [--source auto|api|titan] [--all-leagues]

titan007 说明：

已验证可用(2026-11-04)：https://live.titan007.com/vbsxml/bfdata_ut.js?r=007<随机>
注意：必须带 ?r= 参数和 Referer，否则连接被拒；时间为北京时间(UTC+8)。
字段(按 ^ 分割，经验映射，可能变动)：0 id, 2 联赛, 5 主队, 8 客队, 11 开球(HH:MM 北京时间),
13 状态(0未开,1上半,2中场,3下半,-1完场,-14推迟...), 14/15 主客比分, 21/22 主客排名。
字段映射需定期回归测试。完场比分回填已由 settle.autofill_results（API-Football）完成。
"""
from __future__ import annotations

import json
import random
import re
import sys

import requests
import yaml

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
    """titan007 当日赛程（备用源）。"""
    from . import source_status
    try:
        r = requests.get(URL.format(random.randint(1000, 9999)), headers=HEADERS, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        source_status.record("titan007", False, f"赛程 {type(e).__name__}: {e}")
        raise
    r.encoding = "utf-8"
    out = parse(r.text)
    source_status.record("titan007", bool(out), None if out else "赛程：返回内容无法解析")
    return out


def load_leagues() -> list[dict]:
    from . import CONFIG
    return yaml.safe_load((CONFIG / "leagues.yaml").read_text(encoding="utf-8"))["leagues"]


def normalize_api(f: dict, by_apif: dict | None = None) -> dict:
    lg = (by_apif or {}).get(f["league"]["id"], {})
    st = f["fixture"]["status"]["short"]
    return {"match_id": f"apif-{f['fixture']['id']}", "source": "api_football",
            "api_fixture_id": f["fixture"]["id"], "league_id": f["league"]["id"],
            "league": lg.get("cn") or f["league"]["name"], "league_key": lg.get("id"), "tier": lg.get("tier"),
            "round": f["league"].get("round"), "season": f["league"].get("season"),
            "home": f["teams"]["home"]["name"], "home_id": f["teams"]["home"]["id"],
            "away": f["teams"]["away"]["name"], "away_id": f["teams"]["away"]["id"],
            "kickoff_utc": f["fixture"]["date"].replace("+00:00", "Z"), "status": st,
            "home_goals": f["goals"]["home"], "away_goals": f["goals"]["away"],
            "venue": (f["fixture"].get("venue") or {}).get("name")}


def fetch_api(date: str, client=None, all_leagues: bool = False) -> list[dict]:
    from .api_football import Client
    c = client or Client()
    leagues = load_leagues()
    by_apif = {l["apif"]: l for l in leagues if l.get("apif")}
    raw = c.fixtures_by_date(date, None if all_leagues else list(by_apif))
    out = [normalize_api(f, by_apif) for f in raw]
    out.sort(key=lambda x: (x["tier"] or 9, x["kickoff_utc"]))
    return out


def fetch_any(date: str | None = None, source: str = "auto", client=None, all_leagues: bool = False,
              titan_fetch=None) -> tuple[str, list[dict]]:
    """返回 (实际使用的源, 比赛列表)。auto：API-Football 失败则回退 titan007。"""
    from datetime import datetime, timezone
    from .api_football import ApiFootballError
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if source in ("auto", "api"):
        try:
            return "api_football", fetch_api(date, client, all_leagues)
        except (ApiFootballError, requests.RequestException) as e:
            if source == "api":
                raise
            print(f"[fetch_fixtures] API-Football 不可用，回退 titan007：{e}", file=sys.stderr)
    return "titan007", (titan_fetch or fetch)()


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--source", default="auto", choices=["auto", "api", "titan"])
    ap.add_argument("--all-leagues", action="store_true")
    a = ap.parse_args()
    src, rows = fetch_any(a.date, a.source, all_leagues=a.all_leagues)
    json.dump({"source": src, "fixtures": rows}, sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
