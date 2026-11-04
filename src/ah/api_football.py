"""API-Football v3 轻量客户端（https://v3.football.api-sports.io）。

- Key 只从环境变量 API_FOOTBALL_KEY 读取（CI 中为 Secret），绝不写入文件/日志。
- 请求预算：读取响应头 x-ratelimit-requests-remaining，并在 data/cache/api_usage.json
  记录当日(UTC)已用次数；剩余 <= RESERVE(10) 时拒绝发起非必要请求（抛 BudgetExceeded）。
- 磁盘缓存：data/cache/<endpoint>/<hash>.json，按端点 TTL 过期（cache 目录除 leagues.json 外 gitignore）。
- 免费档限制（2026-11-04 实测，详见 README）：见 PLAN_NOTES。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import DATA

BASE_URL = "https://v3.football.api-sports.io"
CACHE_DIR = DATA / "cache"
USAGE_FILE = CACHE_DIR / "api_usage.json"
DAILY_LIMIT = 100
RESERVE = 10
MIN_INTERVAL_S = 6.5  # 免费档约 10 次/分钟

# 秒；None = 不缓存
TTL = {
    "status": None,
    "leagues": 30 * 86400,
    "fixtures": 6 * 3600,
    "fixtures/headtohead": 7 * 86400,
    "standings": 12 * 3600,
    "injuries": 3 * 3600,
    "fixtures/lineups": 0,          # 特殊：空结果不缓存，非空结果永久缓存（首发公布后不变）
    "teams/statistics": 12 * 3600,
    "odds": 1800,
    "result": 0,                    # 特殊：仅完场(FT/AET/PEN)结果永久缓存
}
FINISHED = {"FT", "AET", "PEN"}


class ApiFootballError(RuntimeError):
    pass


class BudgetExceeded(ApiFootballError):
    pass


class Client:
    def __init__(self, key: str | None = None, cache_dir: Path = CACHE_DIR, session=None,
                 reserve: int = RESERVE, min_interval: float = MIN_INTERVAL_S, now=time.time):
        self._key = key if key is not None else os.environ.get("API_FOOTBALL_KEY", "")
        self.cache_dir = Path(cache_dir)
        self.usage_file = self.cache_dir / "api_usage.json"
        self.session = session or requests.Session()
        self.reserve = reserve
        self.min_interval = min_interval
        self.now = now
        self._last_call = 0.0

    def __repr__(self) -> str:  # 不泄露 key
        return f"<api_football.Client cache={self.cache_dir}>"

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    # ---------- 预算 ----------
    def _today(self) -> str:
        return datetime.fromtimestamp(self.now(), tz=timezone.utc).strftime("%Y-%m-%d")

    def usage(self) -> dict:
        try:
            u = json.loads(self.usage_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            u = {}
        if u.get("date") != self._today():
            u = {"date": self._today(), "used": 0, "remaining": None, "log": []}
        return u

    def _save_usage(self, u: dict) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.usage_file.write_text(json.dumps(u, ensure_ascii=False, indent=1), encoding="utf-8")

    def remaining(self) -> int:
        u = self.usage()
        local = DAILY_LIMIT - u["used"]
        return min(local, u["remaining"]) if u["remaining"] is not None else local

    # ---------- 缓存 ----------
    def _cache_path(self, endpoint: str, params: dict) -> Path:
        key = json.dumps(params, sort_keys=True)
        h = hashlib.sha1(f"{endpoint}?{key}".encode()).hexdigest()[:16]
        return self.cache_dir / endpoint.replace("/", "_") / f"{h}.json"

    def _cache_get(self, path: Path, ttl):
        if ttl is None or not path.exists():
            return None
        d = json.loads(path.read_text(encoding="utf-8"))
        if ttl == 0 and d.get("permanent"):
            return d["data"]
        if ttl and self.now() - d.get("ts", 0) <= ttl:
            return d["data"]
        return None

    def _cache_put(self, path: Path, data, permanent: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": self.now(), "permanent": permanent, "data": data},
                                   ensure_ascii=False), encoding="utf-8")

    # ---------- 请求 ----------
    def get(self, endpoint: str, params: dict | None = None, ttl_key: str | None = None,
            force: bool = False, essential: bool = False) -> dict:
        """返回完整 JSON（含 response/errors/results）。命中缓存不消耗额度。
        essential=True（如赛后结算）可动用预留额度，但不会超过官方剩余。"""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        tk = ttl_key or endpoint
        ttl = TTL.get(tk, 3600)
        path = self._cache_path(tk, params)
        if not force:
            hit = self._cache_get(path, ttl)
            if hit is not None:
                return hit
        if not self.enabled:
            raise ApiFootballError("API_FOOTBALL_KEY 未设置")
        rem = self.remaining()
        floor = 0 if essential else self.reserve
        if endpoint != "status" and rem <= floor:
            raise BudgetExceeded(f"今日剩余 {rem} 次 <= 预留 {floor}，拒绝请求 {endpoint}")
        wait = self.min_interval - (self.now() - self._last_call)
        if wait > 0 and self.min_interval:
            time.sleep(wait)
        r = self.session.get(f"{BASE_URL}/{endpoint}", params=params,
                             headers={"x-apisports-key": self._key}, timeout=30)
        self._last_call = self.now()
        u = self.usage()
        if endpoint != "status":  # /status 不计入额度
            u["used"] += 1
        hdr = r.headers.get("x-ratelimit-requests-remaining")
        if hdr is not None and str(hdr).lstrip("-").isdigit():
            u["remaining"] = int(hdr)
        u["log"] = (u.get("log") or [])[-200:] + [
            {"t": datetime.fromtimestamp(self.now(), tz=timezone.utc).strftime("%H:%M:%S"),
             "endpoint": endpoint, "params": params, "status": r.status_code}]
        self._save_usage(u)
        if r.status_code != 200:
            raise ApiFootballError(f"HTTP {r.status_code} {endpoint}")
        data = r.json()
        errs = data.get("errors")
        if errs:  # API 以 200 + errors 返回计划限制等错误
            raise ApiFootballError(f"{endpoint} errors: {json.dumps(errs, ensure_ascii=False)}")
        if ttl == 0:
            if tk == "fixtures/lineups" and data.get("response"):
                self._cache_put(path, data, permanent=True)
            elif tk == "result" and _is_finished(data):
                self._cache_put(path, data, permanent=True)
        elif ttl:
            self._cache_put(path, data)
        return data

    # ---------- 业务函数 ----------
    def status(self) -> dict:
        return self.get("status")["response"]

    def leagues(self, **params) -> list:
        return self.get("leagues", params)["response"]

    def fixtures_by_date(self, date: str, league_ids=None, season: int | None = None,
                         tz: str = "UTC") -> list:
        """date=YYYY-MM-DD。league_ids 为 None 时一次请求取当天全部比赛再本地过滤（1 次请求）；
        免费档按 league+date 查询需要 season，故默认取全量再过滤更省额度。"""
        data = self.get("fixtures", {"date": date, "timezone": tz})["response"]
        if league_ids:
            ids = {int(i) for i in league_ids}
            data = [f for f in data if f["league"]["id"] in ids]
        return data

    def injuries(self, fixture_id: int) -> list:
        return self.get("injuries", {"fixture": fixture_id})["response"]

    def lineups(self, fixture_id: int) -> list:
        return self.get("fixtures/lineups", {"fixture": fixture_id})["response"]

    def h2h(self, team1: int, team2: int, last: int = 10) -> list:
        """免费档不支持 last 参数（"Free plans do not have access to the Last parameter."），
        故取全部后按日期倒序本地截取最近 last 场（只含已完场）。"""
        resp = self.get("fixtures/headtohead", {"h2h": f"{min(team1, team2)}-{max(team1, team2)}"})["response"]
        done = [f for f in resp if f["fixture"]["status"]["short"] in FINISHED]
        return sorted(done, key=lambda f: f["fixture"]["date"], reverse=True)[:last]

    def team_statistics(self, league: int, season: int, team: int) -> dict:
        return self.get("teams/statistics", {"league": league, "season": season, "team": team})["response"]

    def standings(self, league: int, season: int) -> list:
        return self.get("standings", {"league": league, "season": season})["response"]

    def fixture_result(self, fixture_id: int, essential: bool = True) -> dict | None:
        """返回 {status, home_goals, away_goals, home, away}；未完场返回 status 但 goals 可能为 None。"""
        d = self.get("fixtures", {"id": fixture_id}, ttl_key="result", essential=essential)["response"]
        if not d:
            return None
        return summarize_result(d[0])

    def odds(self, fixture_id: int, bet: int | None = None, bookmaker: int | None = None) -> list:
        return self.get("odds", {"fixture": fixture_id, "bet": bet, "bookmaker": bookmaker})["response"]


def _is_finished(data: dict) -> bool:
    resp = data.get("response") or []
    return bool(resp) and resp[0]["fixture"]["status"]["short"] in FINISHED


def summarize_result(f: dict) -> dict:
    st = f["fixture"]["status"]["short"]
    ft = (f.get("score") or {}).get("fulltime") or {}
    et = (f.get("score") or {}).get("extratime") or {}
    # 亚盘按 90 分钟（含补时）结算：优先 score.fulltime；AET/PEN 时 goals 含加时，不能用
    hg, ag = ft.get("home"), ft.get("away")
    if hg is None and st == "FT":
        hg, ag = f["goals"]["home"], f["goals"]["away"]
    return {"fixture_id": f["fixture"]["id"], "status": st, "finished": st in FINISHED,
            "home": f["teams"]["home"]["name"], "away": f["teams"]["away"]["name"],
            "home_goals": hg, "away_goals": ag, "had_extra_time": bool(et and et.get("home") is not None)}


def asian_handicap_from_odds(resp: list) -> list[dict]:
    """从 /odds 响应提取亚盘（bet name 'Asian Handicap'）：[{bookmaker, value, odd}]。"""
    out = []
    for item in resp or []:
        for bm in item.get("bookmakers", []):
            for bet in bm.get("bets", []):
                if bet.get("name", "").lower() == "asian handicap":
                    for v in bet.get("values", []):
                        out.append({"bookmaker": bm.get("name"), "value": v.get("value"), "odd": v.get("odd")})
    return out


def summarize_injuries(resp: list) -> dict:
    """{team_name: [{player, type, reason}]}"""
    out: dict = {}
    seen = set()
    for it in resp or []:  # 实测同一球员会重复出现，按 (球队, 球员id/名) 去重
        k = (it["team"]["id"], it["player"].get("id") or it["player"]["name"])
        if k in seen:
            continue
        seen.add(k)
        out.setdefault(it["team"]["name"], []).append(
            {"player": it["player"]["name"], "type": it["player"].get("type"), "reason": it["player"].get("reason")})
    return out


def summarize_h2h(resp: list, team_id: int) -> dict:
    """以 team_id 视角统计交锋 W/D/L 与进失球（仅完场）。"""
    w = d = l = gf = ga = 0
    for f in resp or []:
        if f["fixture"]["status"]["short"] not in FINISHED:
            continue
        home = f["teams"]["home"]["id"] == team_id
        hg, ag = f["goals"]["home"], f["goals"]["away"]
        if hg is None:
            continue
        mine, theirs = (hg, ag) if home else (ag, hg)
        gf += mine; ga += theirs
        if mine > theirs: w += 1
        elif mine == theirs: d += 1
        else: l += 1
    return {"played": w + d + l, "w": w, "d": d, "l": l, "gf": gf, "ga": ga}


def main_ah_line(resp: list, prefer=("Pinnacle", "SBO", "Dafabet", "Bet365")) -> dict | None:
    """从 /odds 响应取主盘（两边十进制赔率最接近的那条）。返回主队视角：
    {bookmaker, home_line, home_odds, away_odds, home_hk, away_hk}。数字=主队让球数（见 sources.yaml odds_convention）。"""
    books: dict = {}
    for v in asian_handicap_from_odds(resp):
        side, _, num = (v["value"] or "").partition(" ")
        try:
            line, odd = float(num), float(v["odd"])
        except ValueError:
            continue
        books.setdefault(v["bookmaker"], {}).setdefault(line, {})[side.lower()] = odd
    order = [b for b in prefer if b in books] + [b for b in books if b not in prefer]
    for b in order:
        pairs = [(abs(o["home"] - o["away"]), ln, o) for ln, o in books[b].items() if "home" in o and "away" in o]
        if not pairs:
            continue
        _, ln, o = min(pairs, key=lambda x: x[0])
        return {"bookmaker": b, "home_line": ln, "home_odds": o["home"], "away_odds": o["away"],
                "home_hk": round(o["home"] - 1, 2), "away_hk": round(o["away"] - 1, 2)}
    return None


def form_from_fixtures(fixtures: list, team_id: int, n: int = 5, before: str | None = None) -> str:
    """免费档拿不到当季 teams/statistics，改用本地累积的 /fixtures?date 结果计算近 n 场（新的在后），如 'WDLWW'。"""
    rows = []
    for f in fixtures or []:
        if f["fixture"]["status"]["short"] not in FINISHED:
            continue
        if before and f["fixture"]["date"] >= before:
            continue
        h, a = f["teams"]["home"]["id"], f["teams"]["away"]["id"]
        if team_id not in (h, a):
            continue
        hg, ag = f["goals"]["home"], f["goals"]["away"]
        mine, theirs = (hg, ag) if team_id == h else (ag, hg)
        rows.append((f["fixture"]["date"], "W" if mine > theirs else "D" if mine == theirs else "L"))
    rows = sorted(set(rows))[-n:]
    return "".join(r for _, r in rows)


def form_from_stats(stats: dict) -> str | None:
    """teams/statistics 的 form 串，如 'WWDLW'（最近在后）。"""
    return (stats or {}).get("form")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="API-Football 工具：status/usage/fixtures")
    ap.add_argument("cmd", choices=["status", "usage", "fixtures"])
    ap.add_argument("--date")
    ap.add_argument("--leagues", help="逗号分隔的 league id")
    a = ap.parse_args()
    c = Client()
    if a.cmd == "usage":
        print(json.dumps(c.usage(), ensure_ascii=False, indent=1))
    elif a.cmd == "status":
        s = c.status()
        print(json.dumps({"plan": s.get("subscription", {}).get("plan"),
                          "requests": s.get("requests")}, ensure_ascii=False))
    else:
        ids = [int(x) for x in a.leagues.split(",")] if a.leagues else None
        for f in c.fixtures_by_date(a.date, ids):
            print(f["fixture"]["id"], f["fixture"]["date"], f["league"]["name"], "|",
                  f["teams"]["home"]["name"], "vs", f["teams"]["away"]["name"])


if __name__ == "__main__":
    main()
