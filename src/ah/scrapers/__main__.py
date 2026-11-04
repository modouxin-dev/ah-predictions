"""用法（北京时间；默认不消耗 API-Football 额度，只读其本地缓存）：

  python -m ah.scrapers jleague --match acle/2026/110401 [--match ...] [--map-date 2026-11-04] [--crosscheck] [--allow-api]
  python -m ah.scrapers jleague --discover [--comps j1,acle,acl2]
  python -m ah.scrapers probe afc|aleagues|spl

每场任务请用稳定入口（见 docs/scrapers.md）：
  python -m ah.scrapers.lineups --match-id <API-Football fixture id>
  python -m ah.scrapers.scores  --match-id <API-Football fixture id> | --date YYYY-MM-DD
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import DATA
from . import ScraperError, jleague, probes
from .crosscheck import crosscheck_lineup
from .http import PoliteFetcher

APIF_CACHE = DATA / "cache"


def _cached_fixtures(date: str) -> list | None:
    """读取 API-Football fixtures?date=<date> 的本地缓存（忽略 TTL：fixture id 不变）。"""
    from ..api_football import Client
    c = Client(key="")
    p = c._cache_path("fixtures", {"date": date, "timezone": "UTC"})
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))["data"]["response"]


def _lineups(fid: int, allow_api: bool) -> list | None:
    from ..api_football import Client
    c = Client()
    p = c._cache_path("fixtures/lineups", {"fixture": fid})
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))["data"]["response"]
    if not allow_api:
        return None
    return c.lineups(fid)   # 受 Client 预留额度保护；空结果不缓存


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ah.scrapers")
    sub = ap.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("jleague")
    j.add_argument("--match", action="append", default=[], help="<comp>/<year>/<id>，如 acle/2026/110401")
    j.add_argument("--discover", action="store_true", help="从英文日程页列出比赛路径")
    j.add_argument("--comps", default="")
    j.add_argument("--map-date", action="append", default=[], help="用 API-Football 该日(UTC)缓存赛程对 fixture id")
    j.add_argument("--crosscheck", action="store_true")
    j.add_argument("--allow-api", action="store_true", help="缓存缺失时允许调用 API-Football /fixtures/lineups")
    j.add_argument("--ttl", type=float, default=0)
    p = sub.add_parser("probe")
    p.add_argument("site", choices=["afc", "aleagues", "spl"])
    a = ap.parse_args(argv)

    if a.cmd == "probe":
        try:
            probes.probe(a.site)
        except ScraperError as e:
            print(f"{a.site}: {type(e).__name__}: {e}")
            return 2
        return 0

    fetcher = PoliteFetcher.from_config(jleague.SITE)
    if a.discover:
        html, _ = fetcher.fetch(f"{jleague.BASE}/en/match/", ttl_s=1800)
        comps = {c for c in a.comps.split(",") if c} or None
        for m in jleague.discover(html, comps):
            print(m)
    fixtures = None
    if a.map_date:
        fixtures = []
        for d in a.map_date:
            fixtures += _cached_fixtures(d) or []
    rc = 0
    for mid in a.match:
        try:
            rec = jleague.scrape_match(mid, fetcher=fetcher, api_fixtures=fixtures, ttl_s=a.ttl)
        except ScraperError as e:
            print(f"{mid}: {type(e).__name__}: {e}")
            rc = 2
            continue
        xi = rec["lineups"]["home"]["starting_xi"]
        print(f"{mid}: {rec['kickoff_bjt']} {rec['home']['name']} vs {rec['away']['name']} [{rec['status']}] "
              f"fixture={rec['match_id']} 首发={'有' if xi else '暂无'}")
        if a.crosscheck:
            api = _lineups(rec["match_id"], a.allow_api) if rec["match_id"] else None
            res = crosscheck_lineup(rec, api)
            out = Path(jleague.OUT_DIR) / rec["kickoff_bjt"][:10] / f"crosscheck-{rec['site_match_id'].replace('/', '-')}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            print(f"  核对: {res['verdict']} " + json.dumps({k: v['verdict'] for k, v in res['teams'].items()}, ensure_ascii=False)
                  + (f" ({res.get('reason')})" if res.get("reason") else ""))
    print(f"本次真实请求数: {fetcher.requests_made}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
