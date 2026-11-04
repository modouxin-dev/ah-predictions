"""盘口快照（数据官）：给每天筛选过的每场比赛记录 开盘(open) / 报告时(report) / 收盘(close) 三个时点的
各公司亚盘主盘线和主客水位，写入 data/odds_snapshots/，供回测审计员计算 CLV。格式说明见 docs/odds_snapshots.md。

筛选清单：reports/YYYY/MM/<date>.json 的 picks + watched + watchlist（按 match_id 去重；没有 match_id
或 kickoff_utc 的条目无法对上 API-Football，跳过并在 summary 里列出）。

来源（优先级）：
1. OddsPapi /historical-odds（免费、不计额度，每次最多 3 家）：Pinnacle + 亚洲公司，带每条价格的 createdAt，
   可同时得到开盘、任意时刻（报告时）和开赛前最后一次（收盘，开赛后补跑也准确）。比赛映射需要每联赛 1 次
   计费 /fixtures（缓存 12h，映射写入 <date>.map.json 后不再重复）。429 由 oddspapi.Client 指数退避。
2. The Odds API /sports/{sport}/odds（spreads、regions=eu，每联赛 1 credit，一次覆盖该联赛全部比赛），
   只能取“当前”盘口，所以只用于 report 和开赛前的 close；不覆盖亚冠/卡塔尔。缓存 10 分钟。
3. API-Football /odds?fixture（每场 1 次，日额度）：默认只给 The Odds API 不覆盖的联赛补第二来源（--apif auto），
   保留 --apif-floor（默认 30）次给首发/结算。

验证状态（同一场、同一时点）：
- 行级 verification：同一公司在 ≥2 个来源都有主盘且盘口线一致 → verified；主盘线不同但对方梯子里同一条线的
  两边赔率都在 ±0.06 内（同一市场，只是主盘选线不同）→ verified（status_detail 标 ladder_price_match）；
  否则 conflict；只有一个来源 → single。
- 场级 match_verification：每个来源取各公司主盘线的众数（并列取中位偏低）作为该源共识；≥2 个来源共识一致 → verified；
  共识线不同但共同公司全部行级 verified → verified；否则 conflict；只有一个来源 → single；没有数据 → missing（写一行占位，绝不估算）。

用法（北京时间安排见 docs/odds_snapshots.md）：
  PYTHONPATH=src .venv/bin/python -m ah.odds_snapshots report open --date 2026-11-05
  PYTHONPATH=src .venv/bin/python -m ah.odds_snapshots close            # 每 5 分钟跑：开赛前 10 分钟内的场次
  PYTHONPATH=src .venv/bin/python -m ah.odds_snapshots close --backfill # 开赛后补跑：只用 OddsPapi 历史（免费）
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import DATA, REPORTS
from .match import BJT, parse_dt

SNAP_DIR = DATA / "odds_snapshots"
ODDS_API_CACHE = DATA / "cache" / "odds_api_snap"
PHASES = ("open", "report", "close")
STATUSES = ("verified", "conflict", "single", "missing")
SOURCES = ("oddspapi", "odds_api", "api_football")
LIVE_SOURCES = ("odds_api", "api_football")       # 只能取“当前”盘口的来源
# 回测审计员约定的必备字段：match_id, captured_at, stage, bookmaker, ah_home_line, odds_home, odds_away, odds_format
FIELDS = ["match_id", "captured_at", "stage", "bookmaker", "ah_home_line", "odds_home", "odds_away", "odds_format",
          "source", "verification", "match_verification", "is_main", "home_hk", "away_hk", "as_of", "price_ts",
          "report_date", "fixture_id", "kickoff_utc", "league_id", "league", "home", "away", "status_detail",
          "source_ref"]
ODDS_FORMAT = "decimal"
PAPI_BOOK_GROUPS = (("pinnacle", "sbobet", "singbet"), ("dafabet", "sabasports", "3et"))
LADDER_WINDOW = 0.5          # 主盘之外再存 ±0.5 以内的相邻盘口（is_main=0），便于同盘口算 CLV
ODDS_API_TTL = 600
REPORT_LIVE_TOLERANCE = timedelta(minutes=30)
BOOK_ALIASES = {"sbo": "sbobet", "marathon": "marathonbet", "betonlineag": "betonline", "pinnaclesports": "pinnacle",
                "williamhill": "williamhill", "bet365": "bet365"}


# ---------- 小工具 ----------

def iso(dt: datetime | None) -> str:
    return "" if dt is None else dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    return BOOK_ALIASES.get(s, s)


def apif_id(match_id: str) -> int | None:
    m = re.fullmatch(r"apif-(\d+)", str(match_id or ""))
    return int(m.group(1)) if m else None


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def report_path(date: str) -> Path:
    return REPORTS / date[:4] / date[5:7] / f"{date}.json"


def paths(date: str, snap_dir: Path | None = None) -> dict:
    d = Path(snap_dir or SNAP_DIR)
    return {"csv": d / f"{date}.csv", "summary": d / f"{date}.summary.json", "map": d / f"{date}.map.json"}


def screened(report: dict, leagues=None) -> tuple[list[dict], list[str]]:
    """picks + watched + watchlist → [{match_id, fixture_id, kickoff_utc, league, league_id, home, away, list}]。"""
    from .consensus import league_id
    out, seen, skipped = [], set(), []
    for key in ("picks", "watched", "watchlist"):
        for it in report.get(key) or []:
            mid, ko = it.get("match_id"), it.get("kickoff_utc")
            if not mid or not ko:
                skipped.append(f"{key}: {it.get('home', '?')} vs {it.get('away', '?')}（无 match_id/kickoff_utc）")
                continue
            if mid in seen:
                continue
            seen.add(mid)
            out.append({"match_id": mid, "fixture_id": apif_id(mid), "kickoff_utc": iso(parse_dt(ko)),
                        "league": it.get("league", ""), "league_id": league_id(it.get("league", ""), leagues) or "",
                        "home": it.get("home", ""), "away": it.get("away", ""), "list": key})
    return out, skipped


def pick_main(ladder: list) -> tuple | None:
    """主盘 = 两边赔率最接近的线（与 oddspapi.normalize_ah / api_football.main_ah_line 一致）。"""
    return min(ladder, key=lambda x: (abs(x[1] - x[2]), abs(x[0]))) if ladder else None


def ladder_rows(ladder: list, window: float = LADDER_WINDOW) -> list[tuple[tuple, bool]]:
    m = pick_main(ladder)
    if not m:
        return []
    return [(x, x is m) for x in sorted(ladder) if abs(x[0] - m[0]) <= window + 1e-9]


# ---------- OddsPapi 历史 ----------

def _entries(outcome: dict) -> list[dict]:
    p = (outcome.get("players") or {}).get("0") or []
    if isinstance(p, dict):
        p = [p]
    return sorted((e for e in p if e.get("createdAt")), key=lambda e: e["createdAt"])


def hist_ladder(book: dict, ah_markets: dict, t: datetime | None = None) -> list[tuple]:
    """某公司在时刻 t（含）的亚盘梯子 [(主队让球数, 主赔, 客赔, 价格时间)]；t=None 为最新。最后一条 inactive 视为已关盘。"""
    out = []
    for mid, mk in (book.get("markets") or {}).items():
        meta = ah_markets.get(str(mid))
        if not meta:
            continue
        side = {}
        for oid, o in (mk.get("outcomes") or {}).items():
            s = meta["o"].get(str(oid))
            es = [e for e in _entries(o) if t is None or parse_dt(e["createdAt"]) <= t]
            if not es or es[-1].get("active") is False or not _f(es[-1].get("price")):
                continue
            side[s] = (float(es[-1]["price"]), parse_dt(es[-1]["createdAt"]))
        if "1" in side and "2" in side:
            out.append((float(meta["h"]), side["1"][0], side["2"][0], max(side["1"][1], side["2"][1])))
    return sorted(out)


def hist_open_ts(book: dict, ah_markets: dict) -> datetime | None:
    """开盘时刻：任一亚盘线两边首次都有有效报价的最早时间。"""
    best = None
    for mid, mk in (book.get("markets") or {}).items():
        meta = ah_markets.get(str(mid))
        if not meta:
            continue
        first = {}
        for oid, o in (mk.get("outcomes") or {}).items():
            es = [e for e in _entries(o) if e.get("active") is not False and _f(e.get("price"))]
            if es:
                first[meta["o"].get(str(oid))] = parse_dt(es[0]["createdAt"])
        if "1" in first and "2" in first:
            t = max(first["1"], first["2"])
            best = t if best is None or t < best else best
    return best


def phase_time(phase: str, ko: datetime, now: datetime, as_of: datetime) -> datetime:
    if phase == "close":
        return min(now, ko - timedelta(seconds=1))
    return min(as_of, ko - timedelta(seconds=1))


# ---------- 状态 ----------

def _consensus_line(lines: list[float]) -> float:
    """来源共识 = 各公司主盘线的众数；并列时取并列线的中位(偏低)，保证是真实存在的盘口线。"""
    c = Counter(lines)
    top = max(c.values())
    return statistics.median_low(sorted(x for x, n in c.items() if n == top))


PRICE_TOL = 0.06   # 跨来源同一盘口线的十进制赔率容差（抓取时间不同、四舍五入）


def _same_price(ladder: dict, book: str, sa: str, line: float, sb: str) -> bool:
    """来源 sa 的主盘线 line 是否在来源 sb 的同公司梯子里、且两边赔率都在容差内。"""
    a = ladder.get((book, sa), {}).get(line)
    b = ladder.get((book, sb), {}).get(line)
    return bool(a and b and abs(a[0] - b[0]) <= PRICE_TOL and abs(a[1] - b[1]) <= PRICE_TOL)


def assign_status(rows: list[dict]) -> dict:
    """原地写入 verification / match_verification，返回 {(match_id, stage): {verification, sources:{src:{line, books}}, books}}。"""
    groups = defaultdict(list)
    for r in rows:
        groups[(r["match_id"], r["stage"])].append(r)
    out = {}
    for key, rs in groups.items():
        real = [r for r in rs if r.get("source")]
        mains = [r for r in real if str(r.get("is_main")) == "1"]
        by_book = defaultdict(dict)
        for r in mains:
            by_book[r["bookmaker"]][r["source"]] = float(r["ah_home_line"])
        ladder = defaultdict(dict)    # (bookmaker, source) -> {line: (odds_home, odds_away)}（含相邻线）
        for r in real:
            if _f(r.get("ah_home_line")) is not None and _f(r.get("odds_home")) and _f(r.get("odds_away")):
                ladder[(r["bookmaker"], r["source"])][float(r["ah_home_line"])] = (float(r["odds_home"]), float(r["odds_away"]))
        book_status, via_ladder = {}, set()
        for b, srcs in by_book.items():
            if len(srcs) < 2:
                book_status[b] = "single"
            elif len(set(srcs.values())) == 1:
                book_status[b] = "verified"
            elif all(_same_price(ladder, b, sa, srcs[sa], sb) or _same_price(ladder, b, sb, srcs[sb], sa)
                     for sa in srcs for sb in srcs if sa < sb and srcs[sa] != srcs[sb]):
                book_status[b] = "verified"   # 主盘选线不同，但对方在同一条线上的价格一致
                via_ladder.add(b)
            else:
                book_status[b] = "conflict"
        by_src = defaultdict(list)
        for r in mains:
            by_src[r["source"]].append(float(r["ah_home_line"]))
        src_line = {s: _consensus_line(v) for s, v in by_src.items()}
        common = {b: st for b, st in book_status.items() if len(by_book[b]) >= 2}
        if not src_line:
            ms = "missing"
        elif len(src_line) == 1:
            ms = "single"
        elif Counter(src_line.values()).most_common(1)[0][1] >= 2:
            ms = "verified"
        else:   # 来源共识线不同：若共同公司都在同一盘口线上价格一致，视为同一市场 → verified
            ms = "verified" if common and all(st == "verified" for st in common.values()) else "conflict"
        for r in rs:
            r["match_verification"] = ms
            r["verification"] = book_status.get(r["bookmaker"], "single") if r.get("source") else "missing"
            if r.get("source") and r["bookmaker"] in via_ladder and "ladder_price_match" not in (r.get("status_detail") or ""):
                r["status_detail"] = ((r.get("status_detail") or "") + " ladder_price_match").strip()
        out[key] = {"verification": ms, "books": len(by_book),
                    "sources": {s: {"line": src_line[s], "books": len(by_src[s])} for s in sorted(src_line)},
                    "book_status": dict(Counter(book_status.values()))}
    return out


# ---------- 读写 ----------

def load_rows(p: Path) -> list[dict]:
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _sort_key(r):
    return (r["kickoff_utc"], r["match_id"], PHASES.index(r["stage"]) if r["stage"] in PHASES else 9,
            r.get("source") or "~", r.get("bookmaker") or "", _f(r.get("ah_home_line")) or 0.0)


def write_rows(p: Path, rows: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in sorted(rows, key=_sort_key):
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in FIELDS})
    tmp.replace(p)


def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _dump_json(p: Path, d) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


# ---------- 采集 ----------

class Snapshotter:
    def __init__(self, date: str, papi=None, oddsapi=None, apif=None, now=None, snap_dir: Path | None = None,
                 sources=SOURCES, apif_mode: str = "auto", apif_floor: int = 30, papi_groups=PAPI_BOOK_GROUPS,
                 ah_markets: dict | None = None, aliases=None, leagues=None, odds_api_cache: Path = ODDS_API_CACHE,
                 log=None):
        self.date, self.papi, self.oddsapi, self.apif = date, papi, oddsapi, apif
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.p = paths(date, snap_dir)
        self.sources, self.apif_mode, self.apif_floor = tuple(sources), apif_mode, apif_floor
        self.papi_groups = papi_groups
        if ah_markets is None:
            from .oddspapi import load_ah_markets
            ah_markets = load_ah_markets()
        self.ah_markets, self.aliases, self.leagues = ah_markets, aliases, leagues
        self.odds_api_cache = Path(odds_api_cache)
        self.log = log or (lambda m: print(m, file=sys.stderr))
        self.map = _load_json(self.p["map"], {})
        self.calls = Counter()
        self._oa_events: dict[str, list] = {}

    # --- 映射 ---
    def _map_oddspapi(self, matches: list[dict]) -> None:
        from .oddspapi import TOURNAMENTS, find_fixture
        todo = defaultdict(list)
        for m in matches:
            e = self.map.setdefault(m["match_id"], {})
            if "oddspapi" not in e and m["league_id"] in TOURNAMENTS:
                todo[m["league_id"]].append(m)
        for lid, ms in todo.items():
            kos = [parse_dt(m["kickoff_utc"]) for m in ms]
            try:
                before = self.papi.remaining()
                fx = self.papi.fixtures(TOURNAMENTS[lid], min(kos) - timedelta(hours=1), max(kos) + timedelta(hours=1))
                self.calls["oddspapi_billable"] += before - self.papi.remaining()
            except Exception as e:  # noqa: BLE001  含 BudgetExceeded
                self.log(f"[snap] oddspapi fixtures {lid}: {e}")
                for m in ms:
                    m.setdefault("_why", []).append(f"oddspapi:fixtures_error({type(e).__name__})")
                continue
            for m in ms:
                f = find_fixture(fx, m["home"], m["away"], m["kickoff_utc"], self.aliases)
                self.map[m["match_id"]]["oddspapi"] = f["fixtureId"] if f else None

    def _odds_api_events(self, sport: str) -> list:
        if sport in self._oa_events:
            return self._oa_events[sport]
        cp = self.odds_api_cache / f"{sport}.json"
        if cp.exists() and time.time() - cp.stat().st_mtime < ODDS_API_TTL:
            evs = json.loads(cp.read_text())
        else:
            before = (self.oddsapi.usage() or {}).get("used")
            evs = self.oddsapi.odds(sport, markets="spreads", regions="eu")
            after = (self.oddsapi.usage() or {}).get("used")
            self.calls["odds_api_credits"] += (after - before) if isinstance(before, int) and isinstance(after, int) else 1
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps(evs))
        self._oa_events[sport] = evs
        return evs

    # --- 各来源 → 行 ---
    def _rows_oddspapi(self, m, phases, now, as_of, fresh: bool) -> tuple[dict, list[str]]:
        """返回 {phase: [rows]}（成功抓取的时点，即使为空列表），及原因。"""
        why = []
        fid = self.map.get(m["match_id"], {}).get("oddspapi")
        if fid is None:
            from .oddspapi import TOURNAMENTS
            why.append("oddspapi:not_covered" if m["league_id"] not in TOURNAMENTS else "oddspapi:no_fixture_match")
            return {}, why
        books = {}
        ok = False
        for g in self.papi_groups:
            try:
                h = self.papi.historical_odds(fid, g, ttl=60 if fresh else None)
                self.calls["oddspapi_free"] += 1
                ok = True
            except Exception as e:  # noqa: BLE001
                why.append(f"oddspapi:historical_error({type(e).__name__}:{str(e)[:60]})")
                continue
            for b, v in (h.get("bookmakers") or {}).items():
                books[slug(b)] = v
        if not ok:
            return {}, why
        ko = parse_dt(m["kickoff_utc"])
        out = {}
        for ph in phases:
            rows = []
            for b, v in sorted(books.items()):
                if ph == "open":
                    t = hist_open_ts(v, self.ah_markets)
                    if t is None or t >= ko:
                        continue
                else:
                    t = phase_time(ph, ko, now, as_of)
                lad = hist_ladder(v, self.ah_markets, t)
                for (line, ho, ao, ts), main in ladder_rows(lad):
                    rows.append(self._row(m, ph, b, "oddspapi", main, line, ho, ao, t, ts, now, fid))
            out[ph] = rows
            if not rows:
                why.append(f"oddspapi:{ph}_no_ah_lines")
        return out, why

    def _rows_odds_api(self, m, phases, now) -> tuple[dict, list[str]]:
        from .consensus import SPORT_BY_LEAGUE, find_event
        from .odds_api import normalize_spreads
        sport = SPORT_BY_LEAGUE.get(m["league_id"])
        if not sport:
            return {}, ["odds_api:not_covered"]
        try:
            evs = self._odds_api_events(sport)
        except Exception as e:  # noqa: BLE001
            return {}, [f"odds_api:error({type(e).__name__})"]
        idx = {ev.get("id"): {"id": ev.get("id"), "home": ev.get("home_team"), "away": ev.get("away_team"),
                              "commence": ev.get("commence_time"), "raw": ev} for ev in evs}
        hit = find_event(idx, m["home"], m["away"], m["kickoff_utc"], self.aliases)
        if not hit:
            return {}, ["odds_api:no_event_match"]
        self.map.setdefault(m["match_id"], {})["odds_api"] = hit["id"]
        upd = {slug(b.get("title") or b.get("key")): b.get("last_update") for b in hit["raw"].get("bookmakers", [])}
        rows = []
        for b in normalize_spreads(hit["raw"]):
            bk = slug(b["bookmaker"])
            ts = parse_dt(upd[bk]) if upd.get(bk) else None
            rows.append(self._row(m, phases[0], bk, "odds_api", True, b["home_line"], b["home_odds"], b["away_odds"],
                                  now, ts, now, hit["id"]))
        return {phases[0]: rows}, ([] if rows else ["odds_api:no_spreads"])

    def _rows_apif(self, m, phases, now, fresh: bool) -> tuple[dict, list[str]]:
        from .api_football import asian_handicap_from_odds
        fid = m["fixture_id"]
        if fid is None:
            return {}, ["api_football:no_fixture_id"]
        try:
            if self.apif.remaining() <= self.apif_floor:
                return {}, [f"api_football:floor({self.apif.remaining()}<={self.apif_floor})"]
            before = self.apif.usage().get("used", 0)
            resp = self.apif.get("odds", {"fixture": fid}, force=fresh)["response"]
            self.calls["api_football"] += self.apif.usage().get("used", 0) - before
        except Exception as e:  # noqa: BLE001
            return {}, [f"api_football:error({type(e).__name__})"]
        upd = None
        for it in resp or []:
            upd = parse_dt(it["update"]) if it.get("update") else upd
        books = defaultdict(dict)
        for v in asian_handicap_from_odds(resp):
            side, _, num = (v["value"] or "").partition(" ")
            line, odd = _f(num), _f(v["odd"])
            if line is None or odd is None:
                continue
            books[slug(v["bookmaker"])].setdefault(line, {})[side.lower()] = odd
        rows = []
        for b, lines in sorted(books.items()):
            lad = [(ln, o["home"], o["away"], upd) for ln, o in lines.items() if "home" in o and "away" in o]
            for (line, ho, ao, ts), main in ladder_rows(lad):
                rows.append(self._row(m, phases[0], b, "api_football", main, line, ho, ao, now, ts, now, str(fid)))
        return {phases[0]: rows}, ([] if rows else ["api_football:no_ah"])

    def _row(self, m, phase, book, source, main, line, ho, ao, as_of, ts, now, ref) -> dict:
        return {"report_date": self.date, "match_id": m["match_id"], "fixture_id": m["fixture_id"] or "",
                "kickoff_utc": m["kickoff_utc"], "league_id": m["league_id"], "league": m["league"],
                "home": m["home"], "away": m["away"], "stage": phase, "bookmaker": book, "source": source,
                "is_main": "1" if main else "0", "ah_home_line": float(line) + 0.0, "odds_home": round(float(ho), 3),
                "odds_away": round(float(ao), 3), "odds_format": ODDS_FORMAT, "home_hk": round(float(ho) - 1, 3),
                "away_hk": round(float(ao) - 1, 3), "as_of": iso(as_of), "price_ts": iso(ts), "captured_at": iso(now),
                "status_detail": "",
                "source_ref": ref}

    # --- 主流程 ---
    def run(self, matches: list[dict], phases: list[str], as_of: datetime | None = None,
            backfill: bool = False) -> dict:
        now = self.now()
        as_of = as_of or now
        rows = load_rows(self.p["csv"])
        if "oddspapi" in self.sources and self.papi is not None and self.papi.enabled and matches:
            self._map_oddspapi(matches)
        replaced: dict[tuple, list] = {}     # (mid, phase, source) -> rows（成功抓取的才替换）
        reasons: dict[tuple, list] = defaultdict(list)
        for m in matches:
            ko = parse_dt(m["kickoff_utc"])
            reasons_m = list(m.pop("_why", []))
            # OddsPapi 历史：一次抓取，多时点复用
            if "oddspapi" in self.sources and self.papi is not None and self.papi.enabled:
                got, why = self._rows_oddspapi(m, phases, now, as_of, fresh=("close" in phases))
                reasons_m += why
                for ph, rs in got.items():
                    replaced[(m["match_id"], ph, "oddspapi")] = rs
            for ph in phases:
                live_ok = (ph == "report" and abs(now - as_of) <= REPORT_LIVE_TOLERANCE and now < ko) or \
                          (ph == "close" and now < ko and not backfill)
                if ph == "open":
                    reasons[(m["match_id"], ph)].append("live_sources:no_history")
                elif not live_ok:
                    reasons[(m["match_id"], ph)].append("live_sources:skipped(started_or_as_of_far)")
                if not live_ok:
                    continue
                have_oa = False
                if "odds_api" in self.sources and self.oddsapi is not None and self.oddsapi.enabled:
                    got, why = self._rows_odds_api(m, [ph], now)
                    reasons[(m["match_id"], ph)] += why
                    for p2, rs in got.items():
                        replaced[(m["match_id"], p2, "odds_api")] = rs
                        have_oa = have_oa or bool(rs)
                want_apif = self.apif_mode == "all" or (self.apif_mode == "auto" and not have_oa)
                if "api_football" in self.sources and self.apif is not None and self.apif.enabled and want_apif:
                    got, why = self._rows_apif(m, [ph], now, fresh=(ph == "close"))
                    reasons[(m["match_id"], ph)] += why
                    for p2, rs in got.items():
                        replaced[(m["match_id"], p2, "api_football")] = rs
            for ph in phases:
                reasons[(m["match_id"], ph)] = reasons_m + reasons[(m["match_id"], ph)]
        # 合并：被成功重抓的 (match, phase, source) 整体替换；处理过的 (match, phase) 的旧占位行删除
        done = {(m["match_id"], ph) for m in matches for ph in phases}
        keep = [r for r in rows if (r["match_id"], r["stage"], r.get("source")) not in replaced
                and not ((r["match_id"], r["stage"]) in done and not r.get("source"))]
        for rs in replaced.values():
            keep.extend(rs)
        present = {(r["match_id"], r["stage"]) for r in keep if r.get("source")}
        for m in matches:
            for ph in phases:
                if (m["match_id"], ph) not in present:
                    r = self._row(m, ph, "", "", False, 0, 1, 1, as_of if ph != "close" else now, None, now, "")
                    r.update({"is_main": "", "ah_home_line": "", "odds_home": "", "odds_away": "", "home_hk": "",
                              "away_hk": "", "status_detail": "; ".join(dict.fromkeys(reasons[(m["match_id"], ph)]))[:300]})
                    keep.append(r)
        status = assign_status(keep)
        write_rows(self.p["csv"], keep)
        _dump_json(self.p["map"], self.map)
        return {"verification": status, "rows": len(keep), "calls": dict(self.calls),
                "reasons": {f"{k[0]}|{k[1]}": v for k, v in reasons.items() if v}}


def write_summary(date: str, all_matches: list[dict], skipped: list[str], result: dict, phases, quota: dict,
                  snap_dir: Path | None = None, now: datetime | None = None) -> dict:
    p = paths(date, snap_dir)
    rows = load_rows(p["csv"])
    status = assign_status(rows)
    prev = _load_json(p["summary"], {})
    ms, counts = [], {ph: Counter() for ph in PHASES}
    for m in sorted(all_matches, key=lambda x: (x["kickoff_utc"], x["match_id"])):
        e = {k: m[k] for k in ("match_id", "kickoff_utc", "league_id", "home", "away", "list")}
        e["kickoff_bjt"] = parse_dt(m["kickoff_utc"]).astimezone(BJT).strftime("%Y-%m-%d %H:%M")
        e["phases"] = {}
        for ph in PHASES:
            s = status.get((m["match_id"], ph))
            e["phases"][ph] = s or {"verification": "pending"}
            counts[ph][(s or {}).get("verification", "pending")] += 1
        ms.append(e)
    books = sorted({r["bookmaker"] for r in rows if r.get("source")})
    runs = (prev.get("runs") or [])[-50:] + [{"at": iso(now or datetime.now(timezone.utc)), "phases": list(phases),
                                              "calls": result.get("calls", {}), "quota_after": quota}]
    d = {"report_date": date, "updated_at": iso(now or datetime.now(timezone.utc)), "csv": p["csv"].name,
         "matches_screened": len(all_matches), "skipped": skipped, "bookmakers": books,
         "counts": {ph: dict(c) for ph, c in counts.items()}, "matches": ms, "runs": runs}
    _dump_json(p["summary"], d)
    return d


def select(matches: list[dict], phases, now: datetime, lead_min: int, backfill: bool, ids=None,
           existing: list[dict] | None = None) -> list[dict]:
    if ids:
        return [m for m in matches if m["match_id"] in ids]
    if "close" not in phases:
        return matches
    out = []
    for m in matches:
        ko = parse_dt(m["kickoff_utc"])
        if backfill:
            if now - timedelta(hours=24) <= ko <= now:
                out.append(m)
        elif now < ko <= now + timedelta(minutes=lead_min):
            done = any(r["match_id"] == m["match_id"] and r["stage"] == "close" and r.get("source") in LIVE_SOURCES
                       and parse_dt(r["captured_at"]) >= ko - timedelta(minutes=lead_min) for r in existing or [])
            if not done:
                out.append(m)
    return out


def quota_snapshot(papi=None, oddsapi=None, apif=None) -> dict:
    q = {}
    if papi is not None and papi.enabled:
        u = papi.usage()
        q["oddspapi"] = {"used": u.get("used"), "remaining": papi.remaining(), "limit": u.get("limit"), "month": u.get("month")}
    if oddsapi is not None and oddsapi.enabled:
        u = oddsapi.usage() or {}
        q["odds_api"] = {"used": u.get("used"), "remaining": u.get("remaining")}
    if apif is not None and apif.enabled:
        q["api_football"] = {"used": apif.usage().get("used"), "remaining": apif.remaining()}
    return q


def _default_dates(now: datetime) -> list[str]:
    d = now.astimezone(BJT).date()
    return [str(d + timedelta(days=k)) for k in (-1, 0, 1) if report_path(str(d + timedelta(days=k))).exists()]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m ah.odds_snapshots", description=__doc__.splitlines()[0])
    ap.add_argument("phases", nargs="+", choices=PHASES)
    ap.add_argument("--date", action="append", help="报告日期 YYYY-MM-DD（可多次）；close 默认扫描北京时间昨天/今天/明天的报告")
    ap.add_argument("--match", action="append", help="只处理这些 match_id（apif-xxx）")
    ap.add_argument("--as-of", help="report 时点：ISO 时间或 'report'（=报告 generated_at）；默认现在")
    ap.add_argument("--sources", default=",".join(SOURCES))
    ap.add_argument("--apif", choices=("off", "auto", "all"), default="auto")
    ap.add_argument("--apif-floor", type=int, default=30)
    ap.add_argument("--lead", type=int, default=10, help="close：处理未来多少分钟内开赛的场次（默认 10）")
    ap.add_argument("--backfill", action="store_true", help="close：开赛后补跑，只用 OddsPapi 历史（免费）")
    ap.add_argument("--no-sync", action="store_true", help="不调用 OddsPapi /account 校准额度")
    a = ap.parse_args(argv)
    from . import source_status
    from .api_football import Client as ApiFootball
    from .match import load_aliases
    from .odds_api import OddsAPI
    from .oddspapi import Client as PAPI
    now = datetime.now(timezone.utc)
    sources = tuple(s.strip() for s in a.sources.split(",") if s.strip())
    if a.backfill:
        sources = ("oddspapi",)
    dates = a.date or ([str(now.astimezone(BJT).date())] if "close" not in a.phases else _default_dates(now))
    papi = PAPI() if "oddspapi" in sources else None
    oddsapi = OddsAPI(regions="eu") if "odds_api" in sources else None
    apif = ApiFootball() if "api_football" in sources and a.apif != "off" else None
    synced = False
    for date in dates:
        rp = report_path(date)
        if not rp.exists():
            print(f"[snap] {date}: 报告不存在 {rp}", file=sys.stderr)
            continue
        report = json.loads(rp.read_text(encoding="utf-8"))
        allm, skipped = screened(report)
        existing = load_rows(paths(date)["csv"])
        todo = select(allm, a.phases, now, a.lead, a.backfill, set(a.match or []), existing)
        if not todo:
            print(f"[snap] {date}: {','.join(a.phases)} 无需处理的场次", file=sys.stderr)
            continue
        if papi is not None and papi.enabled and not synced and not a.no_sync:
            try:
                papi.sync_usage()          # /account 不计数；响应不打印
                u = papi.usage()
                source_status.record("oddspapi", True, quota={"used": u.get("used"), "remaining": papi.remaining(),
                                                               "limit": u.get("limit"), "period": "月", "month": u.get("month")})
            except Exception as e:  # noqa: BLE001
                print(f"[snap] oddspapi sync_usage 失败: {type(e).__name__}", file=sys.stderr)
            synced = True
        as_of = None
        if a.as_of:
            as_of = parse_dt(report["generated_at"]) if a.as_of == "report" else parse_dt(a.as_of)
        snap = Snapshotter(date, papi=papi, oddsapi=oddsapi, apif=apif, sources=sources, apif_mode=a.apif,
                           apif_floor=a.apif_floor, aliases=load_aliases())
        res = snap.run(todo, list(a.phases), as_of=as_of, backfill=a.backfill)
        q = quota_snapshot(papi, oddsapi, apif)
        summ = write_summary(date, allm, skipped, res, a.phases, q, now=datetime.now(timezone.utc))
        if papi is not None and papi.backoffs:
            print(f"[snap] oddspapi 429 退避 {len(papi.backoffs)} 次", file=sys.stderr)
        print(json.dumps({"date": date, "matches": len(todo), "calls": res["calls"], "counts": summ["counts"],
                          "bookmakers": len(summ["bookmakers"]), "quota": q}, ensure_ascii=False))


if __name__ == "__main__":
    main()
