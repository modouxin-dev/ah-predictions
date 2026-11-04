"""把 The Odds API 多公司亚盘共识写入日报每个推荐的 market_consensus 字段（报告显示“多家公司共识”）。

流程：pick.league → leagues.yaml 联赛 id → The Odds API sport key；同一联赛当天只调用一次
(fetch_odds.cross_check_odds_api)；用 match.team_similarity（规范化 + team_aliases.yaml + 模糊，阈值 0.75）
和开球时间(±15 分钟)把事件对上推荐。无 key / 额度不足 / 联赛不覆盖 / 未匹配 → 不写该字段（向后兼容）。
用法：PYTHONPATH=src python -m ah consensus YYYY-MM-DD   （之后再 render）
"""
from __future__ import annotations

import argparse
import json
import sys

import yaml

from . import CONFIG
from .match import TOLERANCE, norm, parse_dt, team_similarity

# leagues.yaml id -> The Odds API sport key（亚冠/卡塔尔不覆盖）
SPORT_BY_LEAGUE = {
    "j1": "soccer_japan_j_league", "k1": "soccer_korea_kleague1", "aleague": "soccer_australia_aleague",
    "csl": "soccer_china_superleague", "spl": "soccer_saudi_arabia_pro_league",
    "ucl": "soccer_uefa_champs_league", "uel": "soccer_uefa_europa_league", "epl": "soccer_epl",
    "laliga": "soccer_spain_la_liga", "seriea": "soccer_italy_serie_a", "bundesliga": "soccer_germany_bundesliga",
    "ligue1": "soccer_france_ligue_one", "championship": "soccer_efl_champ",
}
THRESHOLD = 0.75
SOURCE_URL = "https://the-odds-api.com/"
ODDSPAPI_URL = "https://oddspapi.io/"
FIVEDOLLAR_URL = "https://5dollarfootballapi.com/"  # 免费档公开展示需署名：Football data by 5DollarFootballAPI


def _leagues() -> list[dict]:
    return yaml.safe_load((CONFIG / "leagues.yaml").read_text(encoding="utf-8"))["leagues"]


def league_id(label: str, leagues: list[dict] | None = None) -> str | None:
    """报告里的联赛文本（如 'UEFA Champions League（欧冠 …）'）→ leagues.yaml id。最长名称优先，避免 J1/J2 混淆。"""
    n = norm(label)
    best, blen = None, 0
    for lg in leagues if leagues is not None else _leagues():
        for name in (lg.get("name"), lg.get("cn")):
            if name and norm(name) in n and len(norm(name)) > blen:
                best, blen = lg["id"], len(norm(name))
    return best


def find_event(events: dict, home: str, away: str, kickoff, aliases=None, threshold: float = THRESHOLD):
    ko = parse_dt(kickoff)
    best, bs = None, 0.0
    for ev in events.values():
        if ev.get("commence") and abs(parse_dt(ev["commence"]) - ko) > TOLERANCE:
            continue
        hs = team_similarity(ev.get("home") or "", home, aliases)
        as_ = team_similarity(ev.get("away") or "", away, aliases)
        if hs >= threshold and as_ >= threshold and (hs + as_) / 2 > bs:
            best, bs = ev, (hs + as_) / 2
    return best


def summarize(ev: dict, pick_side: str) -> dict | None:
    c = ev.get("consensus")
    if not c:
        return None
    sign = 1 if pick_side == "home" else -1
    pin = next((b for b in ev.get("books", []) if b["bookmaker"].lower() == "pinnacle"), None)
    return {
        "source": "The Odds API",
        "bookmakers": c["bookmakers"],
        "agree": c["agree"],
        "home_line": c["home_line"],
        "pick_line": sign * c["home_line"] + 0.0,
        "pick_water": c["home_hk"] if pick_side == "home" else c["away_hk"],
        "lines": c["lines"],
        "pinnacle": None if not pin else {
            "home_line": pin["home_line"], "pick_line": sign * pin["home_line"] + 0.0,
            "pick_water": pin["home_hk"] if pick_side == "home" else pin["away_hk"]},
    }


def _bet365(fd5, lid, p, aliases):
    """五大联赛：5DollarFootballAPI Bet365 初盘→即时盘（推荐方视角）；失败返回 None。"""
    from . import fivedollar
    if lid not in fivedollar.LEAGUE_IDS or fd5 is None or not fd5.enabled:
        return None
    try:
        return fivedollar.bet365_movement(fd5, lid, p["home"], p["away"], p["kickoff_utc"], p["pick_side"], aliases)
    except Exception as e:  # noqa: BLE001
        print(f"[fivedollar] skip {p.get('match_id')}: {e}", file=sys.stderr)
        return None


def attach(report: dict, fetch=None, aliases=None, leagues=None, fd5=None, papi=None) -> dict:
    """原地更新 report['picks'][*]['market_consensus']。返回 {sport: 事件数 或 'error'} 便于日志。"""
    if fetch is None:
        from .fetch_odds import cross_check_odds_api as fetch
    cache: dict[str, dict] = {}
    log = {}
    for p in report.get("picks", []):
        lid = league_id(p.get("league", ""), leagues)
        mv = _bet365(fd5, lid, p, aliases)
        if mv:
            p["bet365_movement"] = mv
            if FIVEDOLLAR_URL not in p["sources"]:
                p["sources"].append(FIVEDOLLAR_URL)
        sport = SPORT_BY_LEAGUE.get(lid or "")
        if not sport:
            continue
        if sport not in cache:                # 每联赛最多一次付费调用
            cache[sport] = fetch(sport) or {}
            log[sport] = len(cache[sport])
        ev = find_event(cache[sport], p["home"], p["away"], p["kickoff_utc"], aliases)
        s = summarize(ev, p["pick_side"]) if ev else None
        if s:
            p["market_consensus"] = s
            if SOURCE_URL not in p["sources"]:
                p["sources"].append(SOURCE_URL)
    _attach_oddspapi(report, papi, aliases, leagues, log)
    return log


def _attach_oddspapi(report, papi, aliases, leagues, log) -> None:
    """补充源：The Odds API 没给出共识的推荐（亚冠/卡塔尔等）用 OddsPapi 亚洲公司共识；每联赛 1 次 /fixtures。"""
    from . import oddspapi
    if papi is None or not papi.enabled:
        return
    groups: dict[str, list] = {}
    for p in report.get("picks", []):
        lid = league_id(p.get("league", ""), leagues)
        if not p.get("market_consensus") and lid in oddspapi.TOURNAMENTS:
            groups.setdefault(lid, []).append(p)
    for lid, ps in groups.items():
        try:
            evs = oddspapi.events_for(papi, lid, ps, aliases)
        except Exception as e:  # noqa: BLE001  优雅降级（含 BudgetExceeded）
            print(f"[oddspapi] skip {lid}: {e}", file=sys.stderr)
            log[f"oddspapi:{lid}"] = "error"
            continue
        log[f"oddspapi:{lid}"] = len(evs)
        for p in ps:
            ev = find_event(evs, p["home"], p["away"], p["kickoff_utc"], aliases)
            s = summarize(ev, p["pick_side"]) if ev else None
            if s:
                s["source"] = "OddsPapi"
                p["market_consensus"] = s
                if ODDSPAPI_URL not in p["sources"]:
                    p["sources"].append(ODDSPAPI_URL)


def main() -> None:
    from .render_report import report_paths
    ap = argparse.ArgumentParser()
    ap.add_argument("date")
    a = ap.parse_args()
    path, _ = report_paths(a.date)
    r = json.loads(path.read_text(encoding="utf-8"))
    from .fivedollar import Client as FD5
    from .oddspapi import Client as PAPI
    log = attach(r, fd5=FD5(), papi=PAPI())
    path.write_text(json.dumps(r, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    n = sum(1 for p in r["picks"] if p.get("market_consensus"))
    print(f"consensus: calls={log} picks_with_consensus={n}/{len(r['picks'])}", file=sys.stderr)


if __name__ == "__main__":
    main()
