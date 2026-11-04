"""每场首发抓取 + 核对入口（给先知的一次性任务用，开赛前 60 / 40 分钟各跑一次）。

  python -m ah.scrapers.lineups --match-id <API-Football fixture id> [--match-id ...]
                                [--jleague-id comp/year/id] [--no-api] [--official-ttl 秒]

需要的 id：API-Football fixture id（必填）。J.League 比赛路径（如 acle/2026/110402）可选：不给时查
config/sources.yaml 的 scrapers.jleague.acl_match_ids 映射；映射里没有 = 无官网来源。
官网来源：只有 jleague.jp（日本球队的 J 联赛/亚冠比赛）。A-Leagues、AFC 比赛中心、沙特联（条款禁止，已停用）
都没有官网来源 -> 只有 API-Football 时判 unverified。
API-Football 额度：每场最多 2 次 —— fixture 详情（本地赛程缓存命中则 0 次）+ /fixtures/lineups（非空结果永久缓存，
再跑 0 次；未公布时返回空、不缓存，下次再调 1 次）。Client 保留最后 10 次，低于即拒绝。
输出：data/snapshots/lineups/<开赛北京日期>/lineups-<fixture id>.json（核对结果，schema/lineup_crosscheck.schema.json）；
有官网记录时另写 jleague-<comp>-<year>-<id>.json。stdout 每场一行摘要。
退出码：0 完成（已验证 / unverified / 暂无）；1 有「冲突」；2 参数错误或找不到 fixture；
        3 来源故障（官网结构变化/不可用、API-Football 报错或额度不足；能写的结果仍会写出）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import DATA
from ..match import parse_dt
from . import ScraperError, jleague
from .apif import Api
from .common import BJT, acl_match_ids, validate_crosscheck
from .crosscheck import CONFLICT, crosscheck_lineup, single_source_lineup
from .http import PoliteFetcher

OUT_DIR = DATA / "snapshots" / "lineups"
NO_OFFICIAL = {
    188: "A-Leagues 官网比赛页靠 JS/Opta 组件渲染，无可合规抓取的首发",
    307: "沙特联官网条款禁止机器人/爬虫，已停用",
    17: "AFC 比赛中心是单页应用；该场没有 J.League 比赛页映射",
    18: "AFC 比赛中心是单页应用；该场没有 J.League 比赛页映射",
}


def run_one(fid: int, api: Api, jleague_id: str | None = None, official_ttl: float = 0,
            out_dir: Path = OUT_DIR, fetcher: PoliteFetcher | None = None) -> tuple[dict | None, int, str]:
    """返回 (核对结果, 退出码, 摘要行)。"""
    try:
        fx, origin = api.fixture(fid)
    except Exception as e:  # noqa: BLE001
        return None, 3, f"{fid}: API-Football fixture 查询失败 {type(e).__name__}: {e}"
    if not fx:
        return None, 2, f"{fid}: 本地缓存和 API-Football 都找不到这个 fixture（{origin}）"
    rc = 0
    try:
        api_xi, xi_origin = api.lineups(fid)
    except Exception as e:  # noqa: BLE001
        api_xi, xi_origin, rc = None, f"error: {type(e).__name__}: {e}", 3
    jid = jleague_id or acl_match_ids().get(fid)
    rec, note = None, None
    if jid:
        try:
            rec = jleague.scrape_match(jid, fetcher=fetcher or PoliteFetcher.from_config(jleague.SITE),
                                       api_fixtures=[fx], ttl_s=official_ttl,
                                       out_dir=out_dir)
            if rec["match_id"] != fid:          # 手工映射优先，但记下自动匹配的分歧
                note = f"J.League 页面自动匹配结果为 {rec['match_id']}，按手工映射使用 {fid}"
                rec["match_id"] = fid
        except ScraperError as e:
            rc, note = 3, f"J.League {jid} 抓取失败：{type(e).__name__}: {e}"
    if rec:
        res = crosscheck_lineup(rec, api_xi)
    else:
        reason = note or NO_OFFICIAL.get(fx["league"]["id"], "该赛事没有官网爬虫来源")
        res = single_source_lineup(fx, api_xi, reason)
    res.update({"league_id": fx["league"]["id"], "league": fx["league"].get("name"),
                "api_football_origin": {"fixture": origin, "lineups": xi_origin}})
    if note:
        res["note"] = note
    validate_crosscheck(res)
    if res["verdict"] == CONFLICT and rc == 0:
        rc = 1
    day = parse_dt(fx["fixture"]["date"]).astimezone(BJT).strftime("%Y-%m-%d")
    out = Path(out_dir) / day / f"lineups-{fid}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    line = (f"{fid} {res['kickoff_bjt'][:16]} {res['home']} vs {res['away']} -> {res['verdict']}"
            f"{'（' + res['reason'] + '）' if res.get('reason') else ''} | {out}")
    return res, rc, line


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ah.scrapers.lineups", description="首发抓取 + 核对（见模块说明）")
    ap.add_argument("--match-id", type=int, action="append", required=True, help="API-Football fixture id，可重复")
    ap.add_argument("--jleague-id", help="J.League 比赛路径 comp/year/id，覆盖映射（只在单个 --match-id 时可用）")
    ap.add_argument("--no-api", action="store_true", help="只用本地缓存，不消耗 API-Football 额度")
    ap.add_argument("--official-ttl", type=float, default=0, help="官网页面缓存秒数（默认 0=每次重新请求）")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if a.jleague_id and len(a.match_id) > 1:
        print("--jleague-id 只能配合单个 --match-id", file=sys.stderr)
        return 2
    api = Api(allow_api=not a.no_api)
    fetcher = PoliteFetcher.from_config(jleague.SITE)
    rc = 0
    for fid in a.match_id:
        _, r, line = run_one(fid, api, a.jleague_id, a.official_ttl, fetcher=fetcher)
        print(line)
        rc = max(rc, r)
    print(f"API-Football 本次消耗: {api.calls} 次 | 官网真实请求: jleague={fetcher.requests_made}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
