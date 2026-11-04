"""5DollarFootballAPI v1 客户端（https://api.5dollarfootballapi.com/v1）：Bet365 亚盘初盘→即时盘变化（五大联赛）。

- Key：FIVEDOLLAR_KEY（secrets.get_secret），请求头 Authorization: Bearer；绝不打印/记录。
- 免费档：60 次/小时（另有 20 次/分钟上限），仅五大联赛，Bet365 全部盘口（opening/closing/inplay），
  近 3 个月历史。响应头 X-RateLimit-Limit/Remaining/Reset(unix 秒)；429 带 Retry-After → 退避重试。
- 用量记录 data/cache/fivedollar_usage.json；剩余 <= reserve 时拒绝请求（BudgetExceeded）。
- 缓存 data/cache/fivedollar/<hash>.json。
- 约定（2026-11-04 实测）：asian_handicap.line 为主队让球数（负=主让，Brentford-Everton 主 -0.5 @1.825/2.025），
  与 API-Football 'Home -0.5' 同号；closing = 开赛前最新(即时)盘。
- 免费档 fixture 列表不带 odds；单场 GET /fixtures/{id} 内联各盘口线，/fixtures/{id}/odds?market=asian 带水位。
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import DATA
from .secrets import get_secret

BASE_URL = "https://api.5dollarfootballapi.com/v1"
CACHE_DIR = DATA / "cache" / "fivedollar"
USAGE_FILE = DATA / "cache" / "fivedollar_usage.json"
RESERVE = 5
# leagues.yaml id -> 5DollarFootballAPI league id（GET /leagues 2026-11-04 实测，免费档仅这 5 个）
LEAGUE_IDS = {"epl": 4160026622, "ligue1": 3614399544, "bundesliga": 686337048,
              "seriea": 3405541143, "laliga": 4212821298}
TTL = {"leagues": 7 * 86400, "fixtures": 1800, "fixture": 600, "odds": 600, "standings": 6 * 3600}


class FiveDollarError(RuntimeError):
    pass


class BudgetExceeded(FiveDollarError):
    pass


class Client:
    def __init__(self, key: str | None = None, session=None, cache_dir: Path = CACHE_DIR,
                 usage_file: Path = USAGE_FILE, reserve: int = RESERVE, sleep=time.sleep, max_retries: int = 2):
        self._key = key if key is not None else get_secret("FIVEDOLLAR_KEY")
        self.s = session or requests.Session()
        self.cache_dir, self.usage_file = Path(cache_dir), Path(usage_file)
        self.reserve, self.sleep, self.max_retries = reserve, sleep, max_retries

    def __repr__(self) -> str:
        return f"FiveDollar(enabled={self.enabled})"

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    def usage(self) -> dict:
        try:
            return json.loads(self.usage_file.read_text())
        except (OSError, ValueError):
            return {}

    def _record(self, h) -> None:
        if h.get("X-RateLimit-Remaining") is None:
            return
        u = {"limit": int(h.get("X-RateLimit-Limit") or 0), "remaining": int(h["X-RateLimit-Remaining"]),
             "reset": int(float(h.get("X-RateLimit-Reset") or 0))}
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)
        self.usage_file.write_text(json.dumps(u))

    def _cache(self, path, params):
        return self.cache_dir / (hashlib.sha1(json.dumps([path, sorted(params.items())]).encode()).hexdigest()[:16] + ".json")

    def get(self, path: str, params: dict | None = None, ttl: int | None = None):
        if not self.enabled:
            raise FiveDollarError("FIVEDOLLAR_KEY 未配置")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        cp = self._cache(path, params)
        if ttl and cp.exists() and time.time() - cp.stat().st_mtime < ttl:
            return json.loads(cp.read_text())
        u = self.usage()
        if u.get("remaining") is not None and u["remaining"] <= self.reserve and time.time() < u.get("reset", 0):
            raise BudgetExceeded(f"5DollarFootballAPI 剩余 {u['remaining']} <= 保留 {self.reserve}")
        for attempt in range(self.max_retries + 1):
            r = self.s.get(f"{BASE_URL}/{path.lstrip('/')}", params=params,
                           headers={"Authorization": f"Bearer {self._key}"}, timeout=20)
            h = {k.title(): v for k, v in (r.headers or {}).items()}
            h = {k.replace("Ratelimit", "RateLimit"): v for k, v in h.items()}
            self._record(h)
            if r.status_code == 429:
                if attempt == self.max_retries:
                    break
                self.sleep(min(float(h.get("Retry-After") or 30), 120))
                continue
            if r.status_code != 200:
                raise FiveDollarError(f"5DollarFootballAPI {path} HTTP {r.status_code}")
            data = r.json().get("data")
            if ttl:
                cp.parent.mkdir(parents=True, exist_ok=True)
                cp.write_text(json.dumps(data))
            return data
        raise FiveDollarError(f"5DollarFootballAPI {path} 429 重试耗尽")

    def fixtures(self, start: datetime, hours: int = 24, league: int | None = None) -> list:
        st = int(start.timestamp())
        return self.get("fixtures", {"start_time": st, "end_time": st + hours * 3600, "league": league,
                                     "per_page": 100}, ttl=TTL["fixtures"]) or []

    def fixture(self, fid: int) -> dict:
        return self.get(f"fixtures/{fid}", ttl=TTL["fixture"])

    def odds(self, fid: int, market: str = "asian") -> dict:
        return self.get(f"fixtures/{fid}/odds", {"market": market}, ttl=TTL["odds"])

    def standings(self, league: int) -> dict:
        return self.get("standings", {"league": league}, ttl=TTL["standings"])


# ---------- 纯函数 ----------

def normalize_ah(odds: dict) -> dict | None:
    """/fixtures/{id}/odds → {bookmaker, home_line, ah_home_line, home_odds, away_odds, home_hk, away_hk,
    open: {同结构}}（与 odds_api.normalize_spreads 同字段；home_line 负=主让）。"""
    for b in (odds or {}).get("bookmakers", []):
        ah = (b.get("odds") or {}).get("asian_handicap") or {}

        def one(x):
            if not x or x.get("line") is None:
                return None
            return {"home_line": float(x["line"]), "ah_home_line": -float(x["line"]) + 0.0,
                    "home_odds": x.get("home"), "away_odds": x.get("away"),
                    "home_hk": None if x.get("home") is None else round(x["home"] - 1, 3),
                    "away_hk": None if x.get("away") is None else round(x["away"] - 1, 3)}
        cur, op = one(ah.get("closing")), one(ah.get("opening"))
        if cur or op:
            out = dict(cur or op)
            out.update({"bookmaker": b.get("name") or b.get("slug"), "open": op})
            return out
    return None


def movement(norm: dict, pick_side: str) -> dict | None:
    """推荐方视角的 Bet365 初盘→即时盘。pick_line 负=推荐方让球；toward_pick>0 表示盘口朝推荐方加深（升盘）。"""
    if not norm:
        return None
    sign = 1 if pick_side == "home" else -1
    op = norm.get("open") or norm
    o, c = sign * op["home_line"] + 0.0, sign * norm["home_line"] + 0.0
    wk = "home_hk" if pick_side == "home" else "away_hk"
    return {"bookmaker": norm["bookmaker"], "open_pick_line": o, "pick_line": c,
            "open_pick_water": op.get(wk), "pick_water": norm.get(wk), "toward_pick": round(o - c, 2)}


def find_fixture(fixtures: list, home: str, away: str, kickoff, aliases=None, threshold: float = 0.75):
    from .match import TOLERANCE, parse_dt, team_similarity
    ko = parse_dt(kickoff)
    best, bs = None, 0.0
    for f in fixtures:
        if abs(parse_dt(f["kickoff_utc"]) - ko) > TOLERANCE:
            continue
        hs = team_similarity(f["teams"]["home"]["name"], home, aliases)
        as_ = team_similarity(f["teams"]["away"]["name"], away, aliases)
        if hs >= threshold and as_ >= threshold and hs + as_ > bs:
            best, bs = f, hs + as_
    return best


def bet365_movement(c: Client, league_key: str, home: str, away: str, kickoff, pick_side: str,
                    aliases=None) -> dict | None:
    """两次请求：当日赛程(按联赛，6h 窗口) + 单场亚盘水位。"""
    from .match import parse_dt
    lid = LEAGUE_IDS.get(league_key)
    if not lid:
        return None
    ko = parse_dt(kickoff)
    f = find_fixture(c.fixtures(ko - timedelta(hours=3), hours=6, league=lid), home, away, ko, aliases)
    if not f:
        return None
    m = movement(normalize_ah(c.odds(f["id"])), pick_side)
    if m:
        m["fixture_id"] = f["id"]
    return m
