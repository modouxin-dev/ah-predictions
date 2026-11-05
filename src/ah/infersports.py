"""InferSports v1 客户端（https://api.infersports.dev/v1）：亚洲公司亚盘全梯度 + 初盘（只读赔率 API）。

- 来源条款：infersports.dev/terms、/aup（2026-06-08）允许在所属档位内调用 API；禁止绕过限速（多 key/多账号）、
  整包转售原始赔率；需"按档位新鲜度缓存"。robots.txt 只禁 /dashboard /login /signup /verify。
- 档位：**免 key（keyless）不用注册**：60 次/分钟、每 IP 200 次/天、90 秒新鲜度、6 家亚洲公司
  （crown、sbobet、macau、m8bet、nova88、hkjc），不含 Pinnacle。免费 key 需注册（5000 次/天）——由用户决定；
  若设置环境变量 INFERSPORTS_KEY（或 ah-secrets.env）则自动带 Bearer 头，否则走免 key。绝不打印/记录 key。
- 红线：同一站点请求间隔 ≥10 秒（跨进程，用量文件记录上次请求时间）；本地按 UTC 日计数，剩余 <= reserve 拒绝请求；
  429 尊重 Retry-After；X-RateLimit-Scope: daily 直接停（不重试）。
- 缓存 data/cache/infersports/<hash>.json：events 30 分钟、odds 90 秒（=免费档新鲜度）、opening 6 小时。
- 约定（2026-11-05 实测 Machida Zelvia vs Shanghai Shenhua）：asian_handicap.line = 主队让球数（负=主让），
  与本项目 ah_home_line 同号；prices 为十进制；status=suspended 的报价价格为 0，必须丢弃。
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import DATA
from .secrets import get_secret

BASE_URL = "https://api.infersports.dev/v1"
CACHE_DIR = DATA / "cache" / "infersports"
USAGE_FILE = DATA / "cache" / "infersports_usage.json"
DAILY_LIMIT = {"keyless": 200, "key": 5000}
RESERVE = 20
MIN_INTERVAL_S = 10.0
TTL = {"events": 1800, "odds": 90, "opening": 6 * 3600, "leagues": 86400}
BOOKS_FREE = ("crown", "sbobet", "macau", "m8bet", "nova88", "hkjc")
# leagues.yaml id -> InferSports 联赛名（GET /v1/leagues 2026-11-05 实测；联赛列表随赛程动态变化，
# 亚冠精英当天无赛程时不在列表里 → 暂无）。匹配比赛不依赖此表（按日期全量 events + 队名/开球时间匹配）。
LEAGUE_NAMES = {"acl_two": "AFCTwo", "j1": "Jap1", "j2": "Jap2", "k1": "KoreaK1", "aleague": "AUSLA",
                "spl": "SaudiPL", "qsl": "QatarSL", "ucl": "UCL", "uel": "UEL", "epl": "ENPL",
                "laliga": "SPA", "seriea": "ITA", "bundesliga": "GER", "ligue1": "FRA", "championship": "EFLC"}


class InferSportsError(RuntimeError):
    pass


class BudgetExceeded(InferSportsError):
    pass


def _utc_day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).strftime("%Y-%m-%d")


class Client:
    def __init__(self, key: str | None = None, session=None, cache_dir: Path = CACHE_DIR,
                 usage_file: Path = USAGE_FILE, reserve: int = RESERVE, min_interval: float = MIN_INTERVAL_S,
                 sleep=time.sleep, clock=time.time, max_retries: int = 1, enabled: bool = True):
        self._key = key if key is not None else (get_secret("INFERSPORTS_KEY") or "")
        self.s = session or requests.Session()
        self.cache_dir, self.usage_file = Path(cache_dir), Path(usage_file)
        self.reserve, self.min_interval = reserve, float(min_interval)
        self.sleep, self.clock, self.max_retries = sleep, clock, max_retries
        self.enabled = enabled
        self.backoffs: list[float] = []

    def __repr__(self) -> str:   # 不暴露 key
        return f"InferSports(tier={self.tier}, enabled={self.enabled})"

    @property
    def tier(self) -> str:
        return "key" if self._key else "keyless"

    # --- 用量 ---
    def usage(self) -> dict:
        try:
            u = json.loads(self.usage_file.read_text())
        except (OSError, ValueError):
            u = {}
        day = _utc_day(self.clock())
        if u.get("day") != day:
            u = {"day": day, "used": 0, "last_request_at": u.get("last_request_at", 0.0)}
        u["limit"] = DAILY_LIMIT[self.tier]
        u["tier"] = self.tier
        return u

    def remaining(self) -> int:
        u = self.usage()
        return u["limit"] - u["used"]

    def _save(self, u: dict) -> None:
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)
        self.usage_file.write_text(json.dumps(u))

    def _cache(self, path, params):
        key = json.dumps([path, sorted(params.items())])
        return self.cache_dir / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".json")

    # --- 请求 ---
    def get(self, path: str, params: dict | None = None, ttl: int | None = None):
        if not self.enabled:
            raise InferSportsError("infersports 已停用")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        cp = self._cache(path, params)
        if ttl and cp.exists():
            try:
                c = json.loads(cp.read_text())
                if self.clock() - float(c.get("cached_at", 0)) < ttl:
                    return c["data"]
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass
        from . import source_status
        for attempt in range(self.max_retries + 1):
            u = self.usage()
            if u["limit"] - u["used"] <= self.reserve:
                raise BudgetExceeded(f"InferSports 今日剩余 {u['limit'] - u['used']} <= 保留 {self.reserve}")
            wait = self.min_interval - (self.clock() - float(u.get("last_request_at") or 0))
            if wait > 0:
                self.sleep(wait)
            headers = {"Accept": "application/json", "User-Agent": "ah-data/1.0 (internal research)"}
            if self._key:
                headers["Authorization"] = f"Bearer {self._key}"
            r = self.s.get(f"{BASE_URL}/{path.lstrip('/')}", params=params, headers=headers, timeout=30)
            u = self.usage()
            u["used"] += 1
            u["last_request_at"] = self.clock()
            h = {k.lower(): v for k, v in (r.headers or {}).items()}
            if h.get("x-ratelimit-remaining") is not None:
                u["minute_remaining"] = h.get("x-ratelimit-remaining")
            if h.get("ratelimit-remaining") is not None:      # 每日预算头（服务端计数，优先采信）
                try:
                    u["used"] = max(u["used"], u["limit"] - int(h["ratelimit-remaining"]))
                except ValueError:
                    pass
            self._save(u)
            q = {"used": u["used"], "remaining": u["limit"] - u["used"], "limit": u["limit"], "period": "日(UTC)"}
            if r.status_code == 429:
                if (h.get("x-ratelimit-scope") or "").lower() == "daily":
                    u["used"] = u["limit"]
                    self._save(u)
                    source_status.record("infersports", False, f"{path} 429 daily", q)
                    raise BudgetExceeded("InferSports 每日额度已用完（429 daily）")
                if attempt == self.max_retries:
                    break
                ra = min(float(h.get("retry-after") or 30), 120.0)
                self.backoffs.append(ra)
                self.sleep(ra)
                continue
            if r.status_code != 200:
                source_status.record("infersports", False, f"{path} HTTP {r.status_code}", q)
                raise InferSportsError(f"InferSports {path} HTTP {r.status_code}")
            source_status.record("infersports", True, quota=q)
            data = r.json()
            if ttl:
                cp.parent.mkdir(parents=True, exist_ok=True)
                cp.write_text(json.dumps({"cached_at": self.clock(), "data": data}))
            return data
        raise InferSportsError(f"InferSports {path} 429 重试耗尽")

    def events(self, date: str) -> list:
        """某 UTC 日期的全部足球赛程（分页，limit ≤200）。"""
        out, cursor = [], None
        for _ in range(5):
            d = self.get("events", {"sport": "football", "date": date, "limit": 200, "cursor": cursor},
                         ttl=TTL["events"])
            out += d.get("data") or []
            cursor = (d.get("page") or {}).get("next_cursor")
            if not cursor:
                break
        return out

    def odds(self, event_id: str, ttl: int | None = None) -> dict:
        return self.get(f"events/{event_id}/odds", {"markets": "asian_handicap", "period": "full_time"},
                        ttl=TTL["odds"] if ttl is None else ttl)

    def opening(self, event_id: str) -> dict:
        return self.get(f"events/{event_id}/opening", {"markets": "asian_handicap"}, ttl=TTL["opening"])


# ---------- 纯函数 ----------

def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 1.0 else None


def ah_ladders(odds_block: dict) -> dict[str, list[tuple]]:
    """/events/{id}/odds → {bookmaker: [(line, odds_home, odds_away, as_of)]}；只取全场亚盘、status=open、价格>1。"""
    out: dict[str, list] = {}
    for q in (odds_block or {}).get("odds") or []:
        if q.get("market_type") != "asian_handicap" or q.get("period", "full_time") != "full_time":
            continue
        if (q.get("status") or "open") != "open":
            continue
        p = q.get("prices") or {}
        ho, ao, line = _f(p.get("home")), _f(p.get("away")), q.get("line")
        if ho is None or ao is None or line is None:
            continue
        out.setdefault(str(q.get("bookmaker") or "").lower(), []).append((float(line), ho, ao, q.get("as_of")))
    return out


def ah_opening(opening_block: dict) -> dict[str, tuple]:
    """/events/{id}/opening → {bookmaker: (line, odds_home, odds_away, opened_at)}（全场亚盘，每家 1 条初盘）。"""
    out = {}
    for q in (opening_block or {}).get("opening") or []:
        if q.get("market_type") != "asian_handicap" or q.get("period", "full_time") != "full_time":
            continue
        p = q.get("prices") or {}
        ho, ao, line = _f(p.get("home")), _f(p.get("away")), q.get("line")
        if ho is None or ao is None or line is None:
            continue
        out[str(q.get("bookmaker") or "").lower()] = (float(line), ho, ao, q.get("opened_at"))
    return out


def event_index(events: list) -> dict:
    """转成 consensus.find_event 需要的 {id: {id, home, away, commence, raw}}。"""
    return {e.get("id"): {"id": e.get("id"), "home": (e.get("home_team") or {}).get("name"),
                          "away": (e.get("away_team") or {}).get("name"), "commence": e.get("scheduled_at"),
                          "raw": e} for e in events or [] if e.get("id")}
