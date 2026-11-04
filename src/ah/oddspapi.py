"""OddsPapi v4 客户端（https://api.oddspapi.io/v4）：亚洲公司亚盘共识（Pinnacle/SBOBet/Singbet/Dafabet 等 240+ 家），
重点补 The Odds API 不覆盖的亚冠精英/亚冠二/卡塔尔联赛。

- Key：ODDSPAPI_KEY（只从环境变量/secrets.get_secret 读），query 参数 apiKey；绝不打印/记录。
  注意 GET /v4/account 的响应里含 api_key 字段 —— account() 会删除它再返回。
- 免费档：250 次/月（每月 1 日重置），任何计费端点 1 次=1 请求（无论结果）；/v4/historical-odds 免费，
  /v4/account 不计数。各端点冷却约 1000ms。WebSocket 不可用。
- 用量：本地计数 data/cache/oddspapi_usage.json（按月），并可用 sync_usage() 通过 /account 校准；
  剩余 <= reserve(30) 时拒绝计费请求（BudgetExceeded）。
- 亚盘：markets 1060..1084 为 "Asian Handicap h"（fulltime），outcome '1'=主队、'2'=客队，h=主队让球数
  （负=主让；2026-11-04 Buriram-Kashima 实测 Pinnacle 主 +0.25 @2.07/1.80），映射快照见 config/oddspapi_ah_markets.json。
- 每联赛每天：/fixtures?tournamentId（1 次，缓存当天）+ 每个推荐 /odds?fixtureId（1 次，缓存 30 分钟）。
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import CONFIG, DATA
from .secrets import get_secret

BASE_URL = "https://api.oddspapi.io/v4"
CACHE_DIR = DATA / "cache" / "oddspapi"
USAGE_FILE = DATA / "cache" / "oddspapi_usage.json"
MONTHLY_LIMIT = 250
RESERVE = 30
COOLDOWN_S = 1.1        # 兼容旧引用；实际间隔见 MIN_INTERVAL_S / config/sources.yaml
# 同一 key 同一端点两次请求的最小间隔（秒）。官方文档（oddspapi.io/en/docs/errors「Rate limits」）：
# historical-odds/players 5s，fixtures/settlements 2s，account 等 1s，odds/fixture 0.5s；这里各加约 10% 余量。
# 2026-11-04 实测：按 1.1s 发 historical-odds，约 34 次请求触发 ~30 次 429，成功请求间隔稳定在 ~7.6s。
# 由 config/sources.yaml 的 odds_snapshots.sources.oddspapi.min_interval_seconds 配置（缺省用这里）。
MIN_INTERVAL_S = {"default": 1.1, "historical-odds": 5.5, "players": 5.5, "fixtures": 2.2, "settlements": 2.2,
                  "odds": 0.6, "fixture": 0.6}
BACKOFF_BASE_S = 2.0   # 429/5xx 指数退避：2s、4s、8s（尊重 Retry-After），上限 BACKOFF_MAX_S
BACKOFF_MAX_S = 60.0
RETRIES = 3
# leagues.yaml id -> OddsPapi tournamentId（GET /v4/tournaments?sportId=10，2026-11-04 实测）
TOURNAMENTS = {"acl_elite": 463, "acl_two": 668, "qsl": 825, "spl": 955, "j1": 196, "j2": 402,
               "k1": 410, "csl": 649, "aleague": 136, "ucl": 7, "championship": 18}
ASIAN_BOOKS = ("pinnacle", "sbobet", "singbet", "dafabet", "3et", "sabasports", "ps3838", "198bet")
TTL = {"fixtures": 12 * 3600, "odds": 1800, "historical-odds": 1800}


class OddsPapiError(RuntimeError):
    pass


class BudgetExceeded(OddsPapiError):
    pass


def load_min_intervals(path: Path | None = None) -> dict:
    """读 config/sources.yaml → odds_snapshots.sources.oddspapi.min_interval_seconds（或 odds_snapshots.oddspapi.…）（数字=所有端点，或 {endpoint: 秒, default: 秒}）。
    文件/字段缺失或格式不对时用 MIN_INTERVAL_S。"""
    out = dict(MIN_INTERVAL_S)
    try:
        import yaml
        cfg = yaml.safe_load((path or CONFIG / "sources.yaml").read_text(encoding="utf-8")) or {}
        sec = cfg.get("odds_snapshots") or {}
        v = (((sec.get("sources") or {}).get("oddspapi") or {}).get("min_interval_seconds")
             if ((sec.get("sources") or {}).get("oddspapi") or {}).get("min_interval_seconds") is not None
             else (sec.get("oddspapi") or {}).get("min_interval_seconds"))
    except Exception:  # noqa: BLE001  配置坏了不影响采集，退回默认
        return out
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
        return {k: float(v) for k in out} | {"default": float(v)}
    if isinstance(v, dict):
        for k, x in v.items():
            if isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0:
                out[str(k).strip("/")] = float(x)
    return out


def load_ah_markets(path: Path | None = None) -> dict:
    return json.loads((path or CONFIG / "oddspapi_ah_markets.json").read_text(encoding="utf-8"))


class Client:
    def __init__(self, key: str | None = None, session=None, cache_dir: Path = CACHE_DIR,
                 usage_file: Path = USAGE_FILE, reserve: int = RESERVE, limit: int = MONTHLY_LIMIT,
                 sleep=time.sleep, now=None, retries: int = RETRIES, min_interval=None, clock=time.time):
        self._key = key if key is not None else get_secret("ODDSPAPI_KEY")
        self.s = session or requests.Session()
        self.cache_dir, self.usage_file = Path(cache_dir), Path(usage_file)
        self.reserve, self.limit, self.sleep = reserve, limit, sleep
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._last = 0.0
        self._last_by: dict[str, float] = {}
        self.clock = clock
        if min_interval is None:
            min_interval = load_min_intervals()
        elif isinstance(min_interval, (int, float)):       # 一个数 = 所有端点同一间隔
            min_interval = {k: float(min_interval) for k in MIN_INTERVAL_S}
        self.min_interval = {**MIN_INTERVAL_S, **min_interval}
        self.retries = retries
        self.backoffs: list = []   # [(path, status, sleep_s)]，便于日志/汇报

    def __repr__(self) -> str:
        return f"OddsPapi(enabled={self.enabled})"

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    def _month(self) -> str:
        return self.now().strftime("%Y-%m")

    def usage(self) -> dict:
        try:
            u = json.loads(self.usage_file.read_text())
        except (OSError, ValueError):
            u = {}
        if u.get("month") != self._month():
            u = {"month": self._month(), "used": 0, "limit": self.limit}
        return u

    def remaining(self) -> int:
        u = self.usage()
        return u.get("limit", self.limit) - u.get("used", 0)

    def _save(self, u: dict) -> None:
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)
        self.usage_file.write_text(json.dumps(u))

    def _cache(self, path, params):
        return self.cache_dir / (hashlib.sha1(json.dumps([path, sorted(params.items())]).encode()).hexdigest()[:16] + ".json")

    def get(self, path: str, params: dict | None = None, ttl: int | None = None, billable: bool = True):
        if not self.enabled:
            raise OddsPapiError("ODDSPAPI_KEY 未配置")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        cp = self._cache(path, params)
        if ttl and cp.exists() and time.time() - cp.stat().st_mtime < ttl:
            return json.loads(cp.read_text())
        if billable and self.remaining() <= self.reserve:
            raise BudgetExceeded(f"OddsPapi 本月剩余 {self.remaining()} <= 保留 {self.reserve}")
        r = self._request(path, params, retry_5xx=not billable)
        if billable and r.status_code not in (401, 403, 429):  # 处理完成的请求无论成功与否都计数
            u = self.usage()
            u["used"] += 1
            self._save(u)
        from . import source_status
        u = self.usage()
        source_status.record("oddspapi", r.status_code == 200, f"{path} HTTP {r.status_code}",
                             {"used": u.get("used"), "remaining": self.remaining(), "limit": u.get("limit", self.limit),
                              "period": "月", "month": u.get("month")})
        if r.status_code != 200:
            raise OddsPapiError(f"OddsPapi {path} HTTP {r.status_code}")  # 不含 URL（含 key）
        data = r.json()
        if ttl:
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps(data))
        return data

    def _request(self, path: str, params: dict, retry_5xx: bool = False):
        """带冷却与指数退避：429（及免费端点的 5xx）最多重试 self.retries 次（2s、4s、8s…，尊重 Retry-After，上限 60s）。
        429 不计入额度（OddsPapi 未处理该请求）；计费端点的 5xx 会计数，所以不重试以保护月额度。不换 IP、不绕限。"""
        ep = path.strip("/").split("/")[0]
        gap = self.min_interval.get(ep, self.min_interval.get("default", COOLDOWN_S))
        for attempt in range(self.retries + 1):
            wait = gap - (self.clock() - self._last_by.get(ep, 0.0))
            if wait > 0:
                self.sleep(wait)
            r = self.s.get(f"{BASE_URL}/{path.lstrip('/')}", params={**params, "apiKey": self._key}, timeout=60)
            self._last = self._last_by[ep] = self.clock()
            retry = r.status_code == 429 or (retry_5xx and r.status_code >= 500)
            if not retry or attempt == self.retries:
                return r
            delay = BACKOFF_BASE_S * (2 ** attempt)
            ra = (getattr(r, "headers", None) or {}).get("Retry-After")
            try:
                delay = max(delay, float(ra)) if ra is not None else delay
            except ValueError:
                pass
            try:   # 官方 429 体：{"error": {"code": "RATE_LIMITED", "retryMs": 420}}
                ms = ((r.json() or {}).get("error") or {}).get("retryMs")
                if isinstance(ms, (int, float)):
                    delay = max(delay, ms / 1000.0)
            except Exception:  # noqa: BLE001
                pass
            self.backoffs.append((path, r.status_code, min(delay, BACKOFF_MAX_S)))
            self.sleep(min(delay, BACKOFF_MAX_S))
        return r

    def account(self) -> dict:
        a = self.get("account", billable=False)
        a.pop("api_key", None)
        return a

    def sync_usage(self) -> dict:
        """用 /account（不计数）校准本地用量。"""
        subs = [s for s in self.account().get("subscriptions", []) if s.get("is_active")]
        if subs:
            u = self.usage()
            u.update({"used": int(subs[0].get("request_count") or 0), "limit": int(subs[0].get("request_limit") or self.limit)})
            self._save(u)
        return self.usage()

    def fixtures(self, tournament_id: int, start: datetime, end: datetime) -> list:
        return self.get("fixtures", {"tournamentId": tournament_id, "from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                     "to": end.strftime("%Y-%m-%dT%H:%M:%SZ")}, ttl=TTL["fixtures"]) or []

    def odds(self, fixture_id: str, bookmakers=ASIAN_BOOKS) -> dict:
        return self.get("odds", {"fixtureId": fixture_id, "oddsFormat": "decimal",
                                 "bookmakers": ",".join(bookmakers) if bookmakers else None}, ttl=TTL["odds"])

    def historical_odds(self, fixture_id: str, bookmakers=("pinnacle",), ttl: int | None = None) -> dict:
        """免费端点（最多 3 家），可取初盘/变盘历史（每条价格带 createdAt）。ttl 默认 30 分钟；收盘快照传短 ttl。"""
        return self.get("historical-odds", {"fixtureId": fixture_id, "bookmakers": ",".join(bookmakers[:3])},
                        ttl=TTL["historical-odds"] if ttl is None else ttl, billable=False)


# ---------- 纯函数 ----------

def ah_ladder(book: dict, ah_markets: dict) -> list[tuple[float, float, float]]:
    """某公司全部可下注(active)的亚盘线 [(主队让球数, 主赔, 客赔)]。"""
    out = []
    for mid, mk in (book.get("markets") or {}).items():
        meta = ah_markets.get(str(mid))
        if not meta or mk.get("marketActive") is False:
            continue
        pr = {}
        for oid, o in (mk.get("outcomes") or {}).items():
            p = (o.get("players") or {}).get("0") or {}
            if p.get("active") and p.get("price"):
                pr[meta["o"].get(str(oid))] = float(p["price"])
        if "1" in pr and "2" in pr:
            out.append((float(meta["h"]), pr["1"], pr["2"]))
    return sorted(out)


def normalize_ah(odds: dict, ah_markets: dict | None = None) -> list[dict]:
    """每家公司取主盘（两边赔率最接近的线），输出与 odds_api.normalize_spreads 相同结构。"""
    ah_markets = ah_markets if ah_markets is not None else load_ah_markets()
    out = []
    for slug, book in (odds.get("bookmakerOdds") or {}).items():
        lad = ah_ladder(book, ah_markets)
        if not lad:
            continue
        h, ho, ao = min(lad, key=lambda x: abs(x[1] - x[2]))
        out.append({"bookmaker": slug, "home_line": h, "ah_home_line": -h + 0.0, "home_odds": ho, "away_odds": ao,
                    "home_hk": round(ho - 1, 3), "away_hk": round(ao - 1, 3)})
    return out


def find_fixture(fixtures: list, home: str, away: str, kickoff, aliases=None, threshold: float = 0.75):
    from .match import TOLERANCE, parse_dt, team_similarity
    ko = parse_dt(kickoff)
    best, bs = None, 0.0
    for f in fixtures:
        if abs(parse_dt(f["startTime"]) - ko) > TOLERANCE:
            continue
        hs = team_similarity(f.get("participant1Name") or "", home, aliases)
        as_ = team_similarity(f.get("participant2Name") or "", away, aliases)
        if hs >= threshold and as_ >= threshold and hs + as_ > bs:
            best, bs = f, hs + as_
    return best


def events_for(c: Client, league_key: str, picks: list, aliases=None) -> dict[str, dict]:
    """给 consensus.attach 用：同一联赛 1 次 /fixtures + 每个匹配的推荐 1 次 /odds。
    返回与 fetch_odds.cross_check_odds_api 同结构 {"oddspapi-<id>": {home, away, commence, consensus, books}}。"""
    from .match import parse_dt
    from .odds_api import consensus
    tid = TOURNAMENTS.get(league_key)
    if not tid or not picks:
        return {}
    kos = [parse_dt(p["kickoff_utc"]) for p in picks]
    fx = c.fixtures(tid, min(kos) - timedelta(hours=1), max(kos) + timedelta(hours=1))
    out = {}
    for p in picks:
        f = find_fixture(fx, p["home"], p["away"], p["kickoff_utc"], aliases)
        if not f or f["fixtureId"] in {k.split("-", 1)[1] for k in out}:
            continue
        books = normalize_ah(c.odds(f["fixtureId"]))
        out[f"oddspapi-{f['fixtureId']}"] = {"home": f.get("participant1Name"), "away": f.get("participant2Name"),
                                             "commence": f["startTime"], "consensus": consensus(books), "books": books}
    return out
