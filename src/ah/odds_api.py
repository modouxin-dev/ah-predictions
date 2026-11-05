"""The Odds API v4 客户端（https://api.the-odds-api.com/v4），多公司亚盘/大小/1X2 交叉核对。

- Key：ODDS_API_KEY（secrets.get_secret：环境变量 → ah-secrets.env），只作 query 参数 apiKey，绝不打印/记录。
- 额度：免费 500 credits/月；odds 调用花费 = markets 数 × regions 数；/sports、/events 免费。
- 用量：响应头 x-requests-remaining / x-requests-used 写入 data/cache/odds_api_usage.json；
  剩余 < reserve(默认 20) 时拒绝付费调用（BudgetExceeded）。
- spreads 的 point 是该队的让球数（主队 -0.25 = 主让平半）；亚盘四分之一盘以 ±0.25/0.75 出现。
"""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import requests

from . import DATA
from .secrets import get_secret

BASE_URL = "https://api.the-odds-api.com/v4"
USAGE_FILE = DATA / "cache" / "odds_api_usage.json"
RESERVE = 20

SPORT_KEYS = {
    "J1": "soccer_japan_j_league", "K1": "soccer_korea_kleague1", "A-League": "soccer_australia_aleague",
    "CSL": "soccer_china_superleague", "Saudi": "soccer_saudi_arabia_pro_league", "UCL": "soccer_uefa_champs_league",
    "UEL": "soccer_uefa_europa_league", "EPL": "soccer_epl", "La Liga": "soccer_spain_la_liga",
    "Serie A": "soccer_italy_serie_a", "Bundesliga": "soccer_germany_bundesliga",
    "Ligue 1": "soccer_france_ligue_one", "Championship": "soccer_efl_champ",
}


class BudgetExceeded(RuntimeError):
    pass


class OddsAPI:
    def __init__(self, key: str | None = None, session=None, usage_file: Path = USAGE_FILE,
                 reserve: int = RESERVE, regions: str = "eu,uk"):
        self._key = key if key is not None else get_secret("ODDS_API_KEY")
        self.s = session or requests.Session()
        self.usage_file = Path(usage_file)
        self.reserve = reserve
        self.regions = regions

    def __repr__(self) -> str:
        return f"OddsAPI(enabled={self.enabled})"

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    def usage(self) -> dict:
        try:
            return json.loads(self.usage_file.read_text())
        except (OSError, ValueError):
            return {}

    def _record(self, headers, path: str, cost: int) -> None:
        u = self.usage()
        rem, used = headers.get("x-requests-remaining"), headers.get("x-requests-used")
        if rem is not None:
            u["remaining"] = int(float(rem))
        if used is not None:
            u["used"] = int(float(used))
        u["last"] = {"path": path, "cost": cost, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)
        self.usage_file.write_text(json.dumps(u, ensure_ascii=False, indent=1))

    def get(self, path: str, params: dict | None = None, cost: int = 0):
        if not self.enabled:
            raise RuntimeError("ODDS_API_KEY 未配置")
        if cost:
            rem = self.usage().get("remaining")
            if rem is not None and rem - cost < self.reserve:
                raise BudgetExceeded(f"The Odds API 剩余 {rem} < 保留 {self.reserve}+{cost}")
        p = {k: v for k, v in (params or {}).items() if v is not None}
        p["apiKey"] = self._key
        r = self.s.get(f"{BASE_URL}/{path.lstrip('/')}", params=p, timeout=20)
        self._record(r.headers, path, cost)
        from . import source_status
        u = self.usage()
        q = {"used": u.get("used"), "remaining": u.get("remaining"), "limit": 500, "period": "月"}
        source_status.record("odds_api", r.status_code == 200, f"{path} HTTP {r.status_code}", q)
        if r.status_code != 200:
            raise RuntimeError(f"The Odds API {path} HTTP {r.status_code}")  # 不含 URL（含 key）
        return r.json()

    def sports(self) -> list:
        return self.get("sports", {"all": "true"})

    def events(self, sport: str) -> list:
        return self.get(f"sports/{sport}/events")

    def odds(self, sport: str, markets: str = "spreads", regions: str | None = None) -> list:
        regions = regions or self.regions
        cost = len(markets.split(",")) * len(regions.split(","))
        return self.get(f"sports/{sport}/odds", {"regions": regions, "markets": markets,
                                                 "oddsFormat": "decimal"}, cost=cost)

    def event_odds(self, sport: str, event_id: str, markets: str = "alternate_spreads",
                   regions: str | None = None) -> dict:
        regions = regions or self.regions
        cost = len(markets.split(",")) * len(regions.split(","))
        return self.get(f"sports/{sport}/events/{event_id}/odds", {"regions": regions, "markets": markets,
                                                                   "oddsFormat": "decimal"}, cost=cost)


def normalize_spreads(event: dict) -> list[dict]:
    """单场 → [{bookmaker, home_line, home_odds, away_odds, home_hk, away_hk}]（主队视角，与 api_football.main_ah_line 同结构）。
    home_line = 主队 point（与 API-Football 'Home -0.5' 同号：负=主让）；ah_home_line = -point（titan007 约定：正=主让）。"""
    home, away = event.get("home_team"), event.get("away_team")
    out = []
    for b in event.get("bookmakers", []):
        for m in b.get("markets", []):
            if m.get("key") not in ("spreads",):
                continue
            o = {x.get("name"): x for x in m.get("outcomes", [])}
            h, a = o.get(home), o.get(away)
            if not h or not a or h.get("point") is None:
                continue
            out.append({"bookmaker": b.get("title") or b.get("key"), "home_line": float(h["point"]), "ah_home_line": -float(h["point"]) + 0.0,
                        "home_odds": float(h["price"]), "away_odds": float(a["price"]),
                        "home_hk": round(float(h["price"]) - 1, 3), "away_hk": round(float(a["price"]) - 1, 3)})
    return out


def consensus(lines: list[dict], pinnacle_main: float | None = None) -> dict | None:
    """共识盘口：取众数；并列时取离 Pinnacle 主线更近的，没有 Pinnacle 则取绝对值较小的。"""
    if not lines:
        return None
    from collections import Counter
    hl = [x["home_line"] for x in lines]
    c = Counter(hl)
    top = max(c.values())
    modes = sorted(x for x, n in c.items() if n == top)
    if len(modes) == 1:
        chosen = modes[0]
    elif pinnacle_main is not None:
        chosen = min(modes, key=lambda x: (abs(x - pinnacle_main), abs(x), x))
    else:
        chosen = min(modes, key=lambda x: (abs(x), x))
    at = [x for x in lines if x["home_line"] == chosen] or lines
    return {"home_line": chosen, "bookmakers": len(lines), "lines": sorted(set(hl)),
            "home_hk": round(statistics.median(x["home_hk"] for x in at), 3),
            "away_hk": round(statistics.median(x["away_hk"] for x in at), 3),
            "agree": sum(1 for x in hl if x == chosen)}


def is_quarter(line: float) -> bool:
    return abs(line * 4) % 2 == 1
