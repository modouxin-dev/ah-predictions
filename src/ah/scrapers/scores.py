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
            "away_goals": int(sc["away"]), "text": f'{rec["home"]["site_name"]} {sc["home"]}-{sc["away"]} '
                                                   f'{rec["away"]["site_name"]}',
            "source_url": rec["source_url"], "fetched_at": rec.get("fetched_at"), "match_id": rec.get("match_id")}


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


def crosscheck_scores(api_fixtures: list, claims: list, leagues=ACL_LEAGUES, aliases=None) -> dict:
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
                                        "text": c.get("text"), "source_url": c["source_url"]})
        if not api["finished"] or api["home_goals"] is None:
            row["reason"] = f'API-Football 未完场（状态 {api["status"]}），不比较'
        elif not row["official"]:
            row["reason"] = "官方页面没有这场比分（the-afc.com 落地页只列最近有战报/集锦的比赛；非日本球队无 jleague 来源）"
        else:
            bad = []
            for o in row["official"]:
                if o["orientation"] == "reversed":
                    bad.append(f'{o["source"]} 主客顺序与 API-Football 相反：{o["text"]}（{o["source_url"]}）')
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
