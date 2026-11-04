"""亚冠（ACL Elite / ACL Two）比分第二来源核对：API-Football vs 官方（the-afc.com，日本球队另有 jleague.jp）。

football-data.org 不覆盖亚冠，所以亚冠比分只有 API-Football 一个来源；这里用官方页面做第二来源。
判定（每场）：
- 已验证：API-Football 已完场，且至少 1 条官方比分声明与之完全一致、没有任何不一致的声明。
- 冲突：任一官方声明与 API-Football 比分不同，或主客顺序相反；写明两边来源和比分，结算前需人工确认。
- 暂无：API-Football 未完场/无比分，或官方页面没有这场的比分（官网落地页只列最近有战报/集锦的比赛）。
比较口径：官方战报给的是全场最终比分；API-Football 用 goals（含加时，不含点球）。
亚盘按 90 分钟结算，若 API 标记有加时，额外给出 90 分钟比分并注明官方比分不可直接用于结算。
绝不推断或补全比分。
"""

from __future__ import annotations

from ..api_football import FINISHED
from ..match import parse_dt, team_similarity
from .common import BJT
from .crosscheck import CONFLICT, MISSING, VERIFIED

CLI_DOC = """比分核对入口（先知的一次性任务：赛后约 2.5 小时一次，次日早上再补一次）。

  python -m ah.scrapers.scores --match-id <API-Football fixture id> [--match-id ...] [--no-api] [--no-afc]
  python -m ah.scrapers.scores --date <YYYY-MM-DD(UTC)> [--date ...] [--no-api] [--no-afc]

需要的 id：API-Football fixture id（--match-id），或 UTC 日期（--date，核对当天全部 ACL Elite/ACL Two）。
日本球队比赛的 J.League 比赛路径从 config/sources.yaml scrapers.jleague.acl_match_ids 自动查。
官方来源：the-afc.com 落地页（每次最多 2 页，间隔 ≥15 秒，只提取对阵+比分，仅内部核对）+ jleague.jp（有映射时）。
非亚冠比赛不在本核对范围（记「暂无」）。
API-Football 额度：--match-id 每场最多 1 次（/fixtures?id=；本地已有完场结果则 0 次，完场结果永久缓存）；
  --date 每个日期 1 次（/fixtures?date= 强制刷新）。--no-api 只读本地缓存。Client 保留最后 10 次。
输出：--match-id -> data/snapshots/scores/<开赛北京日期>/score-<fixture id>.json；
      --date    -> data/snapshots/scores/<日期>/acl-score-crosscheck.json。
退出码：0 完成且无冲突（已验证/暂无）；1 有「冲突」；2 参数错误或找不到 fixture；
        3 来源故障（API-Football 报错/额度不足，或需要的 AFC 页面全部失败；结果仍会写出）。
停用 AFC：环境变量 AH_AFC_SCORES_DISABLED=1 或 sources.yaml scrapers.afc.scores.enabled: false（不发请求）。
"""

ACL_LEAGUES = {17: "acle", 18: "acl2"}
THRESHOLD = 0.75


def jleague_claim(rec: dict) -> dict | None:
    """J.League 比赛页头部比分（只取 post-game）转成与 afc.parse_scores 同格式的声明。"""
    if rec.get("status") != "post-game" or not rec.get("score"):
        return None
    sc = rec["score"]
    if sc.get("home") is None or sc.get("away") is None:
        return None
    return {"source": "jleague", "competition": rec.get("competition"), "kind": "match_page",
            "home": rec["home"]["name"], "away": rec["away"]["name"], "home_goals": int(sc["home"]),
            "away_goals": int(sc["away"]), "source_url": rec["source_url"], "fetched_at": rec.get("fetched_at"), "match_id": rec.get("match_id")}


def _api_side(f: dict) -> dict:
    st = f["fixture"]["status"]["short"]
    ft = (f.get("score") or {}).get("fulltime") or {}
    et = (f.get("score") or {}).get("extratime") or {}
    return {"status": st, "finished": st in FINISHED, "home": f["teams"]["home"]["name"],
            "away": f["teams"]["away"]["name"], "home_goals": f["goals"]["home"], "away_goals": f["goals"]["away"],
            "fulltime_90": {"home": ft.get("home"), "away": ft.get("away")},
            "had_extra_time": et.get("home") is not None,
            "source": f'API-Football /fixtures?id={f["fixture"]["id"]}'}


def _orient(f: dict, c: dict, aliases=None) -> str | None:
    h, a = f["teams"]["home"]["name"], f["teams"]["away"]["name"]
    if c.get("match_id") is not None:                         # J.League 记录已用开赛时间映射过 fixture
        return "same" if c["match_id"] == f["fixture"]["id"] else None
    if team_similarity(h, c["home"], aliases) >= THRESHOLD and team_similarity(a, c["away"], aliases) >= THRESHOLD:
        return "same"
    if team_similarity(h, c["away"], aliases) >= THRESHOLD and team_similarity(a, c["home"], aliases) >= THRESHOLD:
        return "reversed"
    return None


def crosscheck_scores(api_fixtures: list, claims: list, leagues=ACL_LEAGUES, aliases=None,
                      afc_disabled: str | None = None) -> dict:
    """api_fixtures：API-Football fixture 原始对象（自动只取亚冠两项赛事）；claims：afc.parse_scores / jleague_claim 输出。
    返回 {"results": [...每场...], "unmatched_claims": [...], "summary": {已验证/冲突/暂无: n}}。"""
    results, used = [], set()
    for f in api_fixtures or []:
        lid = f["league"]["id"]
        if lid not in leagues:
            continue
        api = _api_side(f)
        ko = parse_dt(f["fixture"]["date"])
        row = {"match_id": f["fixture"]["id"], "league_id": lid, "competition": leagues[lid],
               "round": f["league"].get("round"), "kickoff_utc": ko.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "kickoff_bjt": ko.astimezone(BJT).isoformat(), "home": api["home"], "away": api["away"],
               "api_football": api, "official": [], "verdict": MISSING, "reason": ""}
        for i, c in enumerate(claims or []):
            if c.get("competition") not in (None, leagues[lid]) and c.get("source") == "afc":
                continue
            o = _orient(f, c, aliases)
            if o:
                used.add(i)
                row["official"].append({"source": c["source"], "orientation": o, "home": c["home"], "away": c["away"],
                                        "home_goals": c["home_goals"], "away_goals": c["away_goals"],
                                        "source_url": c["source_url"]})
        if not api["finished"] or api["home_goals"] is None:
            row["reason"] = f'API-Football 未完场（状态 {api["status"]}），不比较'
        elif not row["official"]:
            row["reason"] = ("官方页面没有这场比分（the-afc.com 落地页只列最近有战报/集锦的比赛；非日本球队无 jleague 来源）"
                             if not afc_disabled else f"AFC 来源已停用（{afc_disabled}），且无 jleague 来源")
        else:
            bad = []
            for o in row["official"]:
                if o["orientation"] == "reversed":
                    bad.append(f'{o["source"]} 主客顺序与 API-Football 相反：{o["home"]} {o["home_goals"]}-{o["away_goals"]} '
                               f'{o["away"]}（{o["source_url"]}）')
                elif (o["home_goals"], o["away_goals"]) != (api["home_goals"], api["away_goals"]):
                    bad.append(f'{o["source"]} {o["home_goals"]}-{o["away_goals"]}（{o["source_url"]}） vs '
                               f'API-Football {api["home_goals"]}-{api["away_goals"]}（{api["source"]}）')
            srcs = "、".join(sorted({o["source"] for o in row["official"]}))
            if bad:
                row["verdict"], row["reason"] = CONFLICT, "；".join(bad) + "。结算前需人工确认"
            else:
                row["verdict"] = VERIFIED
                row["reason"] = f'API-Football 与 {srcs} 比分一致 {api["home_goals"]}-{api["away_goals"]}'
            if api["had_extra_time"]:
                row["reason"] += (f'；有加时，亚盘按 90 分钟 {api["fulltime_90"]["home"]}-{api["fulltime_90"]["away"]}'
                                  f'（仅 API-Football 来源，官方比分含加时）')
        results.append(row)
    summary = {k: sum(r["verdict"] == k for r in results) for k in (VERIFIED, CONFLICT, MISSING)}
    unmatched = [c for i, c in enumerate(claims or []) if i not in used]
    return {"results": results, "unmatched_claims": unmatched, "summary": summary}


def _jleague_claims(fixtures: list, ttl_s: float = 0) -> tuple[list, list, int]:
    """有手工映射、且 API 已完场的日本球队比赛，抓 J.League 比赛页比分。返回 (claims, errors, 请求数)。"""
    from . import ScraperError, jleague
    from .common import acl_match_ids
    from .http import PoliteFetcher
    ids = acl_match_ids()
    todo = [f for f in fixtures if f["fixture"]["id"] in ids and f["fixture"]["status"]["short"] in FINISHED]
    if not todo:
        return [], [], 0
    jf = PoliteFetcher.from_config(jleague.SITE)
    claims, errs = [], []
    for f in todo:
        fid = f["fixture"]["id"]
        try:
            rec = jleague.scrape_match(ids[fid], fetcher=jf, api_fixtures=[f], ttl_s=ttl_s)
            rec["match_id"] = fid
            c = jleague_claim(rec)
            if c:
                claims.append(c)
        except ScraperError as e:
            errs.append(f"jleague {ids[fid]}: {type(e).__name__}: {e}")
    return claims, errs, jf.requests_made


def run(fixtures: list, *, use_afc: bool = True, ttl_s: float = 0, fetcher=None) -> tuple[dict, int]:
    from . import afc
    acl = [f for f in fixtures if f["league"]["id"] in ACL_LEAGUES]
    finished = [f for f in acl if f["fixture"]["status"]["short"] in FINISHED]
    comps = sorted({ACL_LEAGUES[f["league"]["id"]] for f in finished})
    rc, afc_off, pages = 0, None, {}
    claims, errs, jl_reqs = _jleague_claims(finished, ttl_s)
    afc_reqs = 0
    if not use_afc:
        afc_off = "命令行 --no-afc"
    elif comps:
        got = afc.fetch_scores(comps, fetcher=fetcher)
        pages = got["pages"]
        if not got["enabled"]:
            afc_off = got["disabled_reason"]
        else:
            claims += got["claims"]
            afc_reqs = sum(1 for p in pages.values() if p.get("ok") and not p.get("from_cache")) + \
                sum(1 for p in pages.values() if not p.get("ok"))
            if pages and not any(p["ok"] for p in pages.values()):
                rc = 3
    res = crosscheck_scores(acl, claims, afc_disabled=afc_off)
    for f in fixtures:
        if f["league"]["id"] not in ACL_LEAGUES:
            res["results"].append({"match_id": f["fixture"]["id"], "league_id": f["league"]["id"],
                                   "home": f["teams"]["home"]["name"], "away": f["teams"]["away"]["name"],
                                   "verdict": MISSING, "reason": "非亚冠比赛，不在本核对范围"})
            res["summary"][MISSING] += 1
    res.update({"afc_pages": pages, "afc_disabled": afc_off, "errors": errs,
                "official_requests": {"afc_pages_max": afc_reqs, "jleague": jl_reqs}})
    if res["summary"][CONFLICT]:
        rc = max(rc, 1)
    if errs:
        rc = max(rc, 3)
    return res, rc


def main(argv=None) -> int:
    import argparse
    import json
    import sys
    from datetime import datetime
    from .. import DATA
    from .apif import Api
    ap = argparse.ArgumentParser(prog="python -m ah.scrapers.scores", description=CLI_DOC,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--match-id", type=int, action="append", help="API-Football fixture id，可重复")
    g.add_argument("--date", action="append", help="UTC 日期 YYYY-MM-DD，可重复")
    ap.add_argument("--no-api", action="store_true", help="只读本地缓存，不消耗 API-Football 额度")
    ap.add_argument("--no-afc", action="store_true", help="不请求 the-afc.com")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    api = Api(allow_api=not a.no_api)
    out_root = DATA / "snapshots" / "scores"
    rc = 0
    groups = []        # (输出文件, fixtures)
    try:
        if a.match_id:
            for fid in a.match_id:
                f, origin = api.fixture(fid, need_final=True)
                if not f:
                    print(f"{fid}: 本地缓存和 API-Football 都找不到这个 fixture（{origin}）")
                    rc = max(rc, 2)
                    continue
                day = parse_dt(f["fixture"]["date"]).astimezone(BJT).strftime("%Y-%m-%d")
                groups.append((out_root / day / f"score-{fid}.json", [f]))
        else:
            for d in a.date:
                try:
                    datetime.strptime(d, "%Y-%m-%d")
                except ValueError:
                    print(f"日期格式错误：{d}")
                    return 2
                fx, origin = api.fixtures_by_date(d)
                if origin == "none":
                    print(f"{d}: 本地没有该日赛程缓存（--no-api）")
                    rc = max(rc, 2)
                groups.append((out_root / d / "acl-score-crosscheck.json",
                               [f for f in fx if f["league"]["id"] in ACL_LEAGUES]))
    except Exception as e:  # noqa: BLE001 —— API 报错/额度不足
        print(f"API-Football: {type(e).__name__}: {e}")
        return 3
    # 多个分组共用一次 AFC 抓取：先合并跑，再按分组拆分写出
    allfx = [f for _, fx in groups for f in fx]
    res, r = run(allfx, use_afc=not a.no_afc)
    rc = max(rc, r)
    by_id = {x["match_id"]: x for x in res["results"]}
    now = datetime.now(BJT).isoformat(timespec="seconds")
    for path, fx in groups:
        rows = [by_id[f["fixture"]["id"]] for f in fx if f["fixture"]["id"] in by_id]
        part = {"generated_at_bjt": now, "results": rows,
                "summary": {k: sum(x["verdict"] == k for x in rows) for k in (VERIFIED, CONFLICT, MISSING)},
                "afc_pages": res["afc_pages"], "afc_disabled": res["afc_disabled"], "errors": res["errors"],
                "tos_note": "internal-crosscheck-only：只含对阵与比分，不展示、不转载"}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(part, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        for x in rows:
            st = (x.get("api_football") or {}).get("status", "-")
            print(f"{x['match_id']} {x.get('kickoff_bjt', '')[:16]} {x['home']} vs {x['away']} [{st}] "
                  f"{x['verdict']}：{x['reason']}")
        print(f"-> {path}")
    print("汇总:", json.dumps(res["summary"], ensure_ascii=False),
          f"| API-Football 本次消耗: {api.calls} 次 | 官网请求: {json.dumps(res['official_requests'])}")
    for e in res["errors"]:
        print("错误:", e)
    return rc


if __name__ == "__main__":
    import sys
    sys.exit(main())
