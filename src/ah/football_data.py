"""football-data.org v4 客户端（https://api.football-data.org/v4），补 API-Football 免费档在欧洲联赛上的空缺。

- Key：FOOTBALL_DATA_KEY（secrets.get_secret），请求头 X-Auth-Token；绝不打印/记录。
- 免费档：10 次/分钟；覆盖 PL ELC CL FL1 BL1 SA PD PPL DED BSA CLI EC WC（无亚洲联赛）。
- 限速：读 X-Requests-Available-Minute / X-RequestCounter-Reset；剩余为 0 时等待重置；429 退避重试。
- 用途：当季积分榜、球队近期赛果/状态、交锋、按日期赛程/比分（欧洲比赛结算第二比分源）。
- 磁盘缓存 data/cache/football_data/<hash>.json（gitignore）。
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

BASE_URL = "https://api.football-data.org/v4"
CACHE_DIR = DATA / "cache" / "football_data"
# leagues.yaml id -> competition code
CODE_BY_LEAGUE = {"epl": "PL", "championship": "ELC", "ucl": "CL", "ligue1": "FL1",
                  "bundesliga": "BL1", "seriea": "SA", "laliga": "PD"}
TTL = {"standings": 6 * 3600, "teams": 3 * 3600, "head2head": 7 * 86400, "matches": 600}


class FootballDataError(RuntimeError):
    pass


class Client:
    def __init__(self, key: str | None = None, session=None, cache_dir: Path = CACHE_DIR,
                 sleep=time.sleep, max_retries: int = 3):
        self._key = key if key is not None else get_secret("FOOTBALL_DATA_KEY")
        self.s = session or requests.Session()
        self.cache_dir = Path(cache_dir)
        self.sleep = sleep
        self.max_retries = max_retries
        self.available: int | None = None
        self.reset_s: float = 60

    def __repr__(self) -> str:
        return f"FootballData(enabled={self.enabled})"

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    def _cache(self, path, params):
        h = hashlib.sha1(json.dumps([path, sorted((params or {}).items())]).encode()).hexdigest()[:16]
        return self.cache_dir / f"{h}.json"

    def get(self, path: str, params: dict | None = None, ttl: int | None = None) -> dict:
        if not self.enabled:
            raise FootballDataError("FOOTBALL_DATA_KEY 未配置")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        cp = self._cache(path, params)
        if ttl and cp.exists() and time.time() - cp.stat().st_mtime < ttl:
            return json.loads(cp.read_text())
        for attempt in range(self.max_retries + 1):
            if self.available is not None and self.available <= 0:
                self.sleep(self.reset_s + 1)
                self.available = None
            r = self.s.get(f"{BASE_URL}/{path.lstrip('/')}", params=params,
                           headers={"X-Auth-Token": self._key}, timeout=20)
            h = r.headers or {}
            if h.get("X-Requests-Available-Minute") is not None:
                self.available = int(h["X-Requests-Available-Minute"])
            if h.get("X-RequestCounter-Reset") is not None:
                self.reset_s = float(h["X-RequestCounter-Reset"])
            if r.status_code == 429:
                self.sleep(max(self.reset_s, 6 * (attempt + 1)))
                self.available = None
                continue
            from . import source_status
            q = {"remaining": self.available, "limit": 10, "period": "分钟"}
            if r.status_code != 200:
                source_status.record("football_data", False, f"{path} HTTP {r.status_code}", q)
                raise FootballDataError(f"football-data {path} HTTP {r.status_code}")
            source_status.record("football_data", True, quota=q)
            data = r.json()
            if ttl:
                cp.parent.mkdir(parents=True, exist_ok=True)
                cp.write_text(json.dumps(data))
            return data
        raise FootballDataError(f"football-data {path} 429 重试耗尽")

    def standings(self, code: str) -> dict:
        return self.get(f"competitions/{code}/standings", ttl=TTL["standings"])

    def team_matches(self, team_id: int, limit: int = 8) -> list:
        return self.get(f"teams/{team_id}/matches", {"status": "FINISHED", "limit": limit},
                        ttl=TTL["teams"]).get("matches", [])

    def head2head(self, match_id: int, limit: int = 10) -> dict:
        return self.get(f"matches/{match_id}/head2head", {"limit": limit}, ttl=TTL["head2head"])

    def matches(self, date_from: str, date_to: str | None = None, competitions: str | None = None) -> list:
        return self.get("matches", {"dateFrom": date_from, "dateTo": date_to or date_from,
                                    "competitions": competitions}, ttl=TTL["matches"]).get("matches", [])


# ---------- 纯函数（可离线测试） ----------

def find_match(matches: list, home: str, away: str, kickoff, aliases=None, threshold: float = 0.75) -> dict | None:
    from .match import TOLERANCE, parse_dt, team_similarity
    ko = parse_dt(kickoff)
    best, bs = None, 0.0
    for m in matches:
        if abs(parse_dt(m["utcDate"]) - ko) > TOLERANCE:
            continue
        hs = max(team_similarity(m["homeTeam"].get(k) or "", home, aliases) for k in ("name", "shortName"))
        as_ = max(team_similarity(m["awayTeam"].get(k) or "", away, aliases) for k in ("name", "shortName"))
        if hs >= threshold and as_ >= threshold and hs + as_ > bs:
            best, bs = m, hs + as_
    return best


def fulltime(m: dict) -> tuple[int, int] | None:
    """90 分钟比分（亚盘结算口径）。加时/点球的比赛用 regularTime。"""
    if m.get("status") != "FINISHED":
        return None
    sc = m.get("score") or {}
    ft = sc.get("regularTime") if sc.get("duration") in ("EXTRA_TIME", "PENALTY_SHOOTOUT") else sc.get("fullTime")
    if not ft or ft.get("home") is None:
        return None
    return int(ft["home"]), int(ft["away"])


def form(matches: list, team_id: int, n: int = 5) -> str:
    """最近 n 场 W/D/L（最新在前）。"""
    out = []
    for m in sorted(matches, key=lambda x: x["utcDate"], reverse=True):
        r = fulltime(m)
        if not r:
            continue
        mine, opp = r if m["homeTeam"]["id"] == team_id else r[::-1]
        out.append("W" if mine > opp else "D" if mine == opp else "L")
        if len(out) == n:
            break
    return "".join(out)


def table_row(standings: dict, team_id: int) -> dict | None:
    for st in standings.get("standings", []):
        if st.get("type") != "TOTAL":
            continue
        for row in st.get("table", []):
            if row["team"]["id"] == team_id:
                return {"group": st.get("group"), "position": row["position"], "points": row["points"],
                        "played": row["playedGames"], "gd": row["goalDifference"]}
    return None


def h2h_summary(h2h: dict, team_id: int) -> dict:
    w = d = l = 0
    for m in h2h.get("matches", []):
        r = fulltime(m)
        if not r:
            continue
        mine, opp = r if m["homeTeam"]["id"] == team_id else r[::-1]
        w, d, l = w + (mine > opp), d + (mine == opp), l + (mine < opp)
    return {"played": w + d + l, "w": w, "d": d, "l": l}


def fundamentals(c: Client, code: str, home: str, away: str, kickoff, pick_side: str = "home",
                 aliases=None) -> dict:
    """欧洲比赛基本面：积分榜位置、近 5 场状态、交锋。任一步失败记录在 errors。"""
    from .match import parse_dt
    out: dict = {"source": "football-data.org", "errors": {}}
    ko = parse_dt(kickoff)
    try:
        day = c.matches((ko - timedelta(days=1)).strftime("%Y-%m-%d"), (ko + timedelta(days=1)).strftime("%Y-%m-%d"), code)
        m = find_match(day, home, away, ko, aliases)
    except FootballDataError as e:
        out["errors"]["matches"] = str(e)
        return out
    if not m:
        out["errors"]["matches"] = "未匹配到比赛"
        return out
    hid, aid = m["homeTeam"]["id"], m["awayTeam"]["id"]
    pick_id = hid if pick_side == "home" else aid
    out.update({"fd_match_id": m["id"], "status": m.get("status")})
    for name, fn in (("standings", lambda: c.standings(code)),
                     ("form_home", lambda: c.team_matches(hid)),
                     ("form_away", lambda: c.team_matches(aid)),
                     ("h2h", lambda: c.head2head(m["id"]))):
        try:
            v = fn()
        except FootballDataError as e:
            out["errors"][name] = str(e)
            continue
        if name == "standings":
            out["standings"] = {"home": table_row(v, hid), "away": table_row(v, aid)}
        elif name == "form_home":
            out.setdefault("form", {})["home"] = form(v, hid)
        elif name == "form_away":
            out.setdefault("form", {})["away"] = form(v, aid)
        else:
            out["h2h"] = h2h_summary(v, pick_id)
    return out


def score_for(c: Client, code: str, home: str, away: str, kickoff, aliases=None) -> tuple[int, int] | None:
    """第二比分源：返回 (主, 客) 90 分钟比分；未完场/未匹配返回 None。"""
    from .match import parse_dt
    ko = parse_dt(kickoff)
    m = find_match(c.matches(ko.strftime("%Y-%m-%d"), (ko + timedelta(days=1)).strftime("%Y-%m-%d"), code),
                   home, away, ko, aliases)
    return fulltime(m) if m else None
