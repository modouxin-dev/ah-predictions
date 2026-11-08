"""盘口快照（数据官）：给每天筛选过的每场比赛记录 开盘(open) / 报告时(report) / 收盘(close) 三个时点的
各公司亚盘主盘线和主客水位，写入 data/snapshots/odds/（旧路径 data/odds_snapshots/ 只读兼容），供回测审计员计算 CLV。格式说明见 docs/odds_snapshots.md。

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

验证状态（同一场、同一时点；先知 2026-11-04 审定）：
- 主线 is_main：完整盘口里去水后主队概率最接近 0.5 的线，优先用 Pinnacle 完整盘口；只给单线的来源/公司
  （odds_api、1xbet/coolbet/marathonbet 等）只核价——其线等于该主线才标 is_main，否则 false。
- 行级 verification：同一公司在 ≥2 个来源的主盘线相同、且该线两边赔率差都 ≤0.06 → verified；
  主盘线不同但同一条线两边赔率差都 ≤0.06 → conflict，status_detail 标 same_market（不算 verified）；
  同一主线但赔率差 >0.06 → conflict，标 same_line_price_diff；只有一个来源 → single。
  verification 只保留 verified / conflict / single / missing（取消 verified_ladder）。
- 场级 match_verification：每个来源取各公司主盘线的众数（并列取离 Pinnacle 主线更近，无则取绝对值较小）为共识。
  共同公司有 conflict → conflict；否则共识一致或共同公司全 verified → verified；
  共识不同且没有共同公司 → conflict；只有一个来源 → single；没有数据 → missing（写一行占位，绝不估算）。
- overround 列：1/odds_home+1/odds_away-1；超过阈值（默认 6%，config/sources.yaml）时 status_detail 标 high_overround。
- 签发门槛：推荐那条线本身必须是 verified；same_market 只算辅助证据。

用法（北京时间安排见 docs/odds_snapshots.md）：
  PYTHONPATH=src .venv/bin/python -m ah.odds_snapshots report open --date 2026-11-05
  PYTHONPATH=src .venv/bin/python -m ah.odds_snapshots close            # 每 5 分钟跑：开赛前 10 分钟内的场次
  PYTHONPATH=src .venv/bin/python -m ah.odds_snapshots close --backfill # 开赛后补跑：只用 OddsPapi 历史（免费）
"""
from __future__ import annotations
from ah.degrade import record as _degrade_fact

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

SNAP_DIR = DATA / "snapshots" / "odds"
LEGACY_SNAP_DIR = DATA / "odds_snapshots"     # 2026-11-04 之前的路径：新路径没有文件时从这里读（只读，写一律写新路径）
ODDS_API_CACHE = DATA / "cache" / "odds_api_snap"
PHASES = ("open", "report", "close")
STATUSES = ("verified", "conflict", "single", "missing")
SOURCES = ("oddspapi", "odds_api", "api_football", "infersports")  # 用户 2026-11-05 确认：InferSports 免 key 进默认（含收盘循环）
OPTIONAL_SOURCES = ()                                    # 无额外 opt-in 源
ALL_SOURCES = SOURCES + OPTIONAL_SOURCES
LIVE_SOURCES = ("odds_api", "api_football", "infersports")  # 只能取“当前”盘口的来源（infersports 另有 /opening 初盘）
# 只给单条线的来源 / 公司：只用来核价，is_main 仅当其线等于完整盘口主线时才为 1
SINGLE_LINE_SOURCES = frozenset({"odds_api"})
SINGLE_LINE_BOOKS = frozenset({"1xbet", "coolbet", "marathonbet", "betanything", "gtbets", "betonline", "matchbook"})
# 回测审计员约定的必备字段：match_id, captured_at, stage, bookmaker, ah_home_line, odds_home, odds_away, odds_format
FIELDS = ["match_id", "captured_at", "stage", "bookmaker", "ah_home_line", "odds_home", "odds_away", "odds_format",
          "source", "verification", "match_verification", "is_main", "home_hk", "away_hk", "overround", "as_of",
          "price_ts", "report_date", "fixture_id", "kickoff_utc", "league_id", "league", "home", "away",
          "status_detail", "source_ref"]
ODDS_FORMAT = "decimal"
PAPI_BOOK_GROUPS = (("pinnacle", "sbobet", "singbet"), ("dafabet", "sabasports", "3et"), ("ps3838", "198bet"))  # 用户 2026-11-05 确认加 ps3838/198bet
LADDER_WINDOW = 0.5          # 主盘之外再存 ±0.5 以内的相邻盘口（is_main=0），便于同盘口算 CLV
OVERROUND_WARN = 0.06        # 抽水超过此值 → status_detail 标 high_overround（可在 sources.yaml 覆盖）
PAIR_SKEW_S = 2.0            # hist_ladder 主客两边报价允许的最大时间差（秒）；优先同一 createdAt
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


def read_path(date: str, kind: str, snap_dir: Path | None = None, legacy_dir: Path | None = None) -> Path:
    """读取用：新路径有文件就用新路径；否则旧路径（data/odds_snapshots/）有就用旧的；都没有返回新路径。
    snap_dir 显式给出时（测试）只有同时给 legacy_dir 才回退。"""
    new = paths(date, snap_dir)[kind]
    if new.exists():
        return new
    legacy = legacy_dir if legacy_dir is not None else (LEGACY_SNAP_DIR if snap_dir is None else None)
    if legacy is not None:
        old = paths(date, legacy)[kind]
        if old.exists():
            return old
    return new


def main_line(date: str, match_id: str, stage: str = "report", bookmaker: str = "pinnacle",
              snap_dir: Path | None = None, legacy_dir: Path | None = None) -> dict | None:
    """读取 data/snapshots/odds/ 中某场某时点的主线（is_main=1）；新路径没有时回退旧路径 data/odds_snapshots/。
    同公司多来源时优先 oddspapi。返回 {ah_home_line, odds_home, odds_away, source, verification, ...} 或 None。"""
    rows = load_rows(read_path(date, "csv", snap_dir, legacy_dir))
    cands = [r for r in rows if r.get("match_id") == match_id and r.get("stage") == stage
             and r.get("bookmaker") == bookmaker and str(r.get("is_main")) == "1" and r.get("source")]
    if not cands:
        return None
    cands.sort(key=lambda r: (0 if r["source"] == "oddspapi" else 1, r["source"]))
    r = cands[0]
    return {"match_id": match_id, "stage": stage, "bookmaker": bookmaker, "source": r["source"],
            "ah_home_line": float(r["ah_home_line"]), "odds_home": float(r["odds_home"]),
            "odds_away": float(r["odds_away"]), "odds_format": r.get("odds_format") or ODDS_FORMAT,
            "verification": r.get("verification"), "match_verification": r.get("match_verification"),
            "overround": _f(r.get("overround")), "captured_at": r.get("captured_at"),
            "price_ts": r.get("price_ts"), "status_detail": r.get("status_detail") or ""}




# ---------- 交叉核验打标（用户 2026-11-05 启用；只标 summary，不改主线） ----------

def load_cross_verification(path=None) -> dict:
    """读 config/sources.yaml 的 cross_verification_draft；缺省/异常返回 enabled=False。"""
    try:
        import yaml
        from . import CONFIG
        cfg = yaml.safe_load((path or (CONFIG / "sources.yaml")).read_text(encoding="utf-8")) or {}
        sec = cfg.get("cross_verification_draft") or {}
        return sec if isinstance(sec, dict) else {"enabled": False}
    except Exception:
        _degrade_fact("odds_snapshots", "config_read", None)
        return {"enabled": False}


def evaluate_cross_verification(rows: list[dict], mid: str, stage: str, cfg: dict,
                                now: datetime | None = None) -> dict:
    """在主线上数 source / bookmaker / verified / Pinnacle-or-亚洲，并按时效打标。不改动原 verification。

    口径（用户 2026-11-05 确认）：
    - 打标默认排除 suspended 的 api_football；双源/3家/verified 门槛不放宽。
    - 时效用 now（缺省=该阶段最新 captured_at），只看主线 Pinnacle 与 verified 公司，
      不要全场最旧一家一票否决。空阶段 pending，不算失败。
    """
    excluded = set(cfg.get("exclude_sources") or ["api_football"])
    mains = [r for r in rows if r.get("match_id") == mid and r.get("stage") == stage
             and r.get("source") and r.get("source") not in excluded and str(r.get("is_main")) == "1"]
    if not mains:
        return {
            "status": "pending", "passed": None, "reasons": [], "sources": [],
            "bookmakers_on_main": [], "verified_books": [], "pinnacle_or_asian": False,
            "stale_age_s": None, "freshness": "pending", "deep_line": None,
            "shared_book": False,
        }
    sources = sorted({r["source"] for r in mains})
    books = sorted({r["bookmaker"] for r in mains})
    verified = sorted({r["bookmaker"] for r in mains if r.get("verification") == "verified"})
    asian = sorted({"sbobet", "singbet", "crown", "sabasports"} & set(books))
    pin_ok = "pinnacle" in books
    # 双源但公司集合不相交：标 no_shared_book，不写「价格冲突」
    by_book = defaultdict(set)
    for r in mains:
        by_book[r["bookmaker"]].add(r["source"])
    shared_book = any(len(v) >= 2 for v in by_book.values())
    # 时效：只看主线 Pinnacle + verified 公司；没有这两类时再看全部主线
    watch = [r for r in mains if r["bookmaker"] == "pinnacle" or r.get("verification") == "verified"]
    if not watch:
        watch = mains
    anchor = now
    if anchor is None:
        caps = [parse_dt(r["captured_at"]) for r in mains if r.get("captured_at")]
        anchor = max(caps) if caps else None
    stale_age_s = None
    stale_detail = []
    for r in watch:
        pts = r.get("price_ts")
        if not pts or not anchor:
            continue
        try:
            age = (anchor - parse_dt(pts)).total_seconds()
            stale_age_s = age if stale_age_s is None else max(stale_age_s, age)
            stale_detail.append({"book": r["bookmaker"], "source": r["source"], "age_s": round(age)})
        except Exception:
            continue
    mins = cfg.get("min_sources") or {}
    minb = cfg.get("min_bookmakers_on_main_line") or {}
    minv = cfg.get("min_verified_books") or {}
    stale = (cfg.get("stale_s") or {}).get(stage) or {}
    need_src = int(mins.get(stage, 1 if stage == "open" else 2) or 0)
    need_books = int(minb.get(stage, 2 if stage == "open" else 3) or 0)
    need_verified = int(minv.get(stage, 0 if stage == "open" else 1) or 0)
    reasons = []
    if len(sources) < need_src:
        reasons.append(f"sources:{len(sources)}<{need_src}")
    if len(books) < need_books:
        reasons.append(f"books:{len(books)}<{need_books}")
    if len(verified) < need_verified:
        reasons.append(f"verified:{len(verified)}<{need_verified}")
    if stage != "open" and not (pin_ok or len(asian) >= 2):
        reasons.append("no_pinnacle_and_asian<2")
    if len(sources) >= 2 and not shared_book:
        reasons.append("no_shared_book")
    freshness = "ok"
    if stage != "open" and stale_age_s is not None:
        wd = stale.get("withdraw")
        dg = stale.get("downgrade")
        if wd is not None and stale_age_s > wd:
            freshness = "stale_withdraw"
            reasons.append(f"stale>{wd}s")
        elif dg is not None and stale_age_s > dg:
            freshness = "stale_downgrade"
            reasons.append(f"stale>{dg}s")
    passed = (not reasons) if stage != "open" else (len(sources) >= need_src and len(books) >= need_books)
    return {
        "status": "ok" if passed else "fail",
        "passed": passed,
        "reasons": reasons,
        "sources": sources,
        "bookmakers_on_main": books,
        "verified_books": verified,
        "pinnacle_or_asian": pin_ok or len(asian) >= 2,
        "stale_age_s": round(stale_age_s) if stale_age_s is not None else None,
        "stale_detail": stale_detail,
        "freshness": freshness,
        "deep_line": None,
        "shared_book": shared_book,
    }


def cross_verification_for_rows(rows: list[dict], mid: str, cfg: dict | None = None,
                                now: datetime | None = None, purpose: str = "auto") -> dict:
    """只打标，不自动拦截。purpose=auto：有 close 数据则赛前/收盘看 close，否则看当时 report。
    open 永不进 verdict；空阶段 pending，不进 withhold。旧 report 不得否决新 close。"""
    cfg = cfg if cfg is not None else load_cross_verification()
    excluded = set(cfg.get("exclude_sources") or ["api_football"])
    live = [r for r in rows if r.get("source") not in excluded]
    out = {"enabled": bool(cfg.get("enabled")), "advisory_only": True, "phases": {}}
    for ph in ("open", "report", "close"):
        v = evaluate_cross_verification(live, mid, ph, cfg, now=now)
        mains = [r for r in live if r.get("match_id") == mid and r.get("stage") == ph
                 and r.get("source") and str(r.get("is_main")) == "1"]
        line = None
        if mains:
            pin = [r for r in mains if r["bookmaker"] == "pinnacle"]
            line = float((pin or mains)[0]["ah_home_line"])
        deep = bool(line is not None and abs(line) >= 1.75)
        v["main_line"] = line
        v["deep_line"] = deep
        # 深盘只打标；是否拦截由先知签发侧决定，本函数不自动 withhold
        if deep and ph in ((cfg.get("deep_line") or {}).get("check_stages") or ["report", "close"]):
            v["reasons"] = list(v["reasons"]) + ["deep_line"]
        out["phases"][ph] = v
    has_close = bool(out["phases"]["close"]["sources"])
    if purpose == "close" or (purpose == "auto" and has_close):
        active = "close"
    else:
        active = "report"  # 早报/尚未有 close：看当时 report
    out["active_stage"] = active
    av = out["phases"][active]
    if av["status"] == "pending":
        out["verdict"] = "pending"
        out["advisory"] = "pending"
    else:
        out["verdict"] = "ok" if av["passed"] else "review"  # review=打标未过；不自动拦截
        out["advisory"] = out["verdict"]
    return out


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


def home_novig(oh: float, oa: float) -> float:
    """去水后主队隐含概率。"""
    ih, ia = 1.0 / float(oh), 1.0 / float(oa)
    return ih / (ih + ia)


def overround(oh: float, oa: float) -> float:
    return 1.0 / float(oh) + 1.0 / float(oa) - 1.0


def pick_main(ladder: list) -> tuple | None:
    """主盘 = 完整盘口里去水后主队概率最接近 0.5 的线；并列取 |line| 较小。"""
    return min(ladder, key=lambda x: (abs(home_novig(x[1], x[2]) - 0.5), abs(x[0]))) if ladder else None


def ladder_rows(ladder: list, window: float = LADDER_WINDOW) -> list[tuple[tuple, bool]]:
    m = pick_main(ladder)
    if not m:
        return []
    return [(x, abs(x[0] - m[0]) < 1e-9) for x in sorted(ladder) if abs(x[0] - m[0]) <= window + 1e-9]


def is_single_line(source: str, bookmaker: str) -> bool:
    return source in SINGLE_LINE_SOURCES or bookmaker in SINGLE_LINE_BOOKS


# ---------- OddsPapi 历史 ----------

def _entries(outcome: dict) -> list[dict]:
    p = (outcome.get("players") or {}).get("0") or []
    if isinstance(p, dict):
        p = [p]
    return sorted((e for e in p if e.get("createdAt")), key=lambda e: e["createdAt"])


def hist_ladder(book: dict, ah_markets: dict, t: datetime | None = None,
                max_skew_s: float = PAIR_SKEW_S) -> list[tuple]:
    """某公司在时刻 t（含）的亚盘梯子 [(主队让球数, 主赔, 客赔, 价格时间)]；t=None 为最新。
    主客两边只用同一时刻（相同 createdAt；否则 |Δt|≤max_skew_s）的配对，避免不同更新时刻拼出虚高抽水。
    最后一条 inactive 视为已关盘。"""
    out = []
    for mid, mk in (book.get("markets") or {}).items():
        meta = ah_markets.get(str(mid))
        if not meta:
            continue
        hist: dict[str, list] = {}
        closed = False
        for oid, o in (mk.get("outcomes") or {}).items():
            s = meta["o"].get(str(oid))
            es = [e for e in _entries(o) if t is None or parse_dt(e["createdAt"]) <= t]
            if not es or es[-1].get("active") is False:   # 最新一条 inactive → 整条线已关盘
                closed = True
                break
            hist[s] = [e for e in es if e.get("active") is not False and _f(e.get("price"))]
        if closed or "1" not in hist or "2" not in hist or not hist["1"] or not hist["2"]:
            continue
        by1 = {parse_dt(e["createdAt"]): e for e in hist["1"]}
        by2 = {parse_dt(e["createdAt"]): e for e in hist["2"]}
        common = sorted(set(by1) & set(by2), reverse=True)
        pair = None
        if common:
            ts = common[0]
            pair = (float(by1[ts]["price"]), float(by2[ts]["price"]), ts)
        elif max_skew_s > 0:
            # 无同一 createdAt：找最新的、时间差在阈值内的两边报价
            best = None
            for t1, e1 in by1.items():
                for t2, e2 in by2.items():
                    skew = abs((t1 - t2).total_seconds())
                    if skew <= max_skew_s:
                        latest = max(t1, t2)
                        if best is None or latest > best[0] or (latest == best[0] and skew < best[1]):
                            best = (latest, skew, float(e1["price"]), float(e2["price"]))
            if best:
                pair = (best[2], best[3], best[0])
        if pair:
            out.append((float(meta["h"]), pair[0], pair[1], pair[2]))
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

def _consensus_line(lines: list[float], pinnacle_main: float | None = None) -> float:
    """来源共识 = 各公司主盘线的众数；并列时取离 Pinnacle 主线更近的，没有 Pinnacle 则取绝对值较小的。"""
    c = Counter(lines)
    top = max(c.values())
    modes = sorted(x for x, n in c.items() if n == top)
    if len(modes) == 1:
        return modes[0]
    if pinnacle_main is not None:
        return min(modes, key=lambda x: (abs(x - pinnacle_main), abs(x), x))
    return min(modes, key=lambda x: (abs(x), x))


PRICE_TOL = 0.06   # 跨来源同一盘口线的十进制赔率容差（抓取时间不同、四舍五入）


def _same_price(ladder: dict, book: str, sa: str, line: float, sb: str) -> bool:
    """同一公司、同一条线 line 在来源 sa 和 sb 里都有，且两边赔率都在容差内。"""
    a = ladder.get((book, sa), {}).get(line)
    b = ladder.get((book, sb), {}).get(line)
    return bool(a and b and abs(a[0] - b[0]) <= PRICE_TOL and abs(a[1] - b[1]) <= PRICE_TOL)


def _shared_line_match(ladder: dict, book: str, sa: str, sb: str) -> float | None:
    """两个来源在同一公司上有没有赔率一致的共同线；有则返回其中一条线。"""
    la, lb = ladder.get((book, sa), {}), ladder.get((book, sb), {})
    for line in set(la) & set(lb):
        if abs(la[line][0] - lb[line][0]) <= PRICE_TOL and abs(la[line][1] - lb[line][1]) <= PRICE_TOL:
            return line
    return None


def _book_status(ladder: dict, book: str, srcs: dict) -> tuple[str, str]:
    """srcs: {source: main_line}。返回 (status, detail_tag)。
    verified = 同一主线且赔率一致；主线不同但同线赔率一致 → conflict + same_market；否则 conflict。"""
    if len(srcs) < 2:
        return "single", ""
    pairs = [(a, b) for a in srcs for b in srcs if a < b]
    if all(srcs[a] == srcs[b] and _same_price(ladder, book, a, srcs[a], b) for a, b in pairs):
        return "verified", ""
    if all(srcs[a] == srcs[b] for a, b in pairs):
        return "conflict", "same_line_price_diff"
    if all(_same_price(ladder, book, a, srcs[a], b) or _same_price(ladder, book, b, srcs[b], a)
           or _shared_line_match(ladder, book, a, b) is not None for a, b in pairs):
        return "conflict", "same_market"
    return "conflict", ""


def reconcile_is_main(rows: list[dict]) -> None:
    """按完整盘口重算 is_main；单线来源/公司仅当其线等于（优先 Pinnacle 的）主线时标 1。原地修改。"""
    groups = defaultdict(list)
    for r in rows:
        if r.get("source") and _f(r.get("ah_home_line")) is not None and _f(r.get("odds_home")) and _f(r.get("odds_away")):
            groups[(r["match_id"], r["stage"])].append(r)
    for rs in groups.values():
        by_bs: dict[tuple, list] = defaultdict(list)
        for r in rs:
            by_bs[(r["bookmaker"], r["source"])].append(r)
        preferred = None
        for (book, src), brs in by_bs.items():
            if is_single_line(src, book):
                for r in brs:
                    r["is_main"] = "0"
                continue
            lad = [(float(r["ah_home_line"]), float(r["odds_home"]), float(r["odds_away"])) for r in brs]
            m = pick_main(lad)
            if not m:
                continue
            for r in brs:
                r["is_main"] = "1" if abs(float(r["ah_home_line"]) - m[0]) < 1e-9 else "0"
            if book == "pinnacle" and (preferred is None or src == "oddspapi"):
                preferred = m[0]
        if preferred is None:
            for r in rs:
                if r.get("bookmaker") == "pinnacle" and r.get("is_main") == "1":
                    preferred = float(r["ah_home_line"])
                    break
        if preferred is None:
            mains = [float(r["ah_home_line"]) for r in rs if r.get("is_main") == "1"]
            if mains:
                preferred = _consensus_line(mains)
        if preferred is None:
            continue
        for r in rs:
            if is_single_line(r["source"], r["bookmaker"]):
                r["is_main"] = "1" if abs(float(r["ah_home_line"]) - preferred) < 1e-9 else "0"


def assign_status(rows: list[dict], overround_warn: float = OVERROUND_WARN) -> dict:
    """原地写入 verification / match_verification / overround 标记，返回场级摘要。"""
    reconcile_is_main(rows)
    groups = defaultdict(list)
    for r in rows:
        groups[(r["match_id"], r["stage"])].append(r)
    out = {}
    for key, rs in groups.items():
        real = [r for r in rs if r.get("source")]
        for r in real:
            oh, oa = _f(r.get("odds_home")), _f(r.get("odds_away"))
            if oh and oa:
                r["overround"] = round(overround(oh, oa), 4)
            elif "overround" not in r or r.get("overround") in (None, ""):
                r["overround"] = ""
        mains = [r for r in real if str(r.get("is_main")) == "1"]
        by_book = defaultdict(dict)
        for r in mains:
            by_book[r["bookmaker"]][r["source"]] = float(r["ah_home_line"])
        ladder = defaultdict(dict)
        for r in real:
            if _f(r.get("ah_home_line")) is not None and _f(r.get("odds_home")) and _f(r.get("odds_away")):
                ladder[(r["bookmaker"], r["source"])][float(r["ah_home_line"])] = (
                    float(r["odds_home"]), float(r["odds_away"]))
        books = sorted({r["bookmaker"] for r in real})
        book_status, book_detail = {}, {}
        for b in books:
            srcs_b = sorted({r["source"] for r in real if r["bookmaker"] == b})
            mains_b = by_book.get(b, {})
            if len(mains_b) >= 2:
                st, det = _book_status(ladder, b, mains_b)
            elif len(srcs_b) >= 2:
                # 只有一侧有 is_main（另一侧是单线且线≠主线）：同线价一致 → same_market
                if any(_shared_line_match(ladder, b, a, c) is not None
                       for i, a in enumerate(srcs_b) for c in srcs_b[i + 1:]):
                    st, det = "conflict", "same_market"
                else:
                    st, det = "single", ""
            else:
                st, det = "single", ""
            book_status[b], book_detail[b] = st, det
        pin_mains = [float(r["ah_home_line"]) for r in mains if r["bookmaker"] == "pinnacle"]
        pin_main = pin_mains[0] if pin_mains else None
        by_src = defaultdict(list)
        for r in mains:
            by_src[r["source"]].append(float(r["ah_home_line"]))
        src_line = {s: _consensus_line(v, pin_main) for s, v in by_src.items()}
        multi = {b: st for b, st in book_status.items() if st != "single"}
        if not real:
            ms = "missing"
        elif not src_line and not multi:
            ms = "missing"
        elif "conflict" in book_status.values():
            ms = "conflict"
        elif len(src_line) <= 1 and not multi:
            ms = "single"
        elif len(src_line) >= 2 and Counter(src_line.values()).most_common(1)[0][1] >= 2:
            sts = set(multi.values()) or {"verified"}
            ms = "verified" if sts <= {"verified"} else "conflict"
        else:
            # 来源共识线不同：共同公司全 verified 才 verified；无共同公司也标 conflict
            sts = set(multi.values())
            ms = "verified" if sts == {"verified"} else "conflict"
        for r in rs:
            r["match_verification"] = ms
            r["verification"] = book_status.get(r["bookmaker"], "single") if r.get("source") else "missing"
            drop = {"ladder_price_match", "verified_ladder", "same_line_price_diff", "same_market", "high_overround"}
            detail = " ".join(x for x in (r.get("status_detail") or "").split() if x not in drop)
            tag = book_detail.get(r.get("bookmaker"), "")
            if r.get("source") and tag:
                detail = (detail + " " + tag).strip()
            ov = _f(r.get("overround"))
            if r.get("source") and ov is not None and ov > overround_warn:
                detail = (detail + " high_overround").strip()
            r["status_detail"] = detail
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
                 log=None, legacy_dir: Path | None = None, infs=None):
        self.date, self.papi, self.oddsapi, self.apif = date, papi, oddsapi, apif
        self.infs = infs
        self.snap_dir, self.legacy_dir = snap_dir, legacy_dir
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
        self.map = _load_json(read_path(date, "map", snap_dir, legacy_dir), {})
        self.calls = Counter()
        self._oa_events: dict[str, list] = {}
        self._infs_events: dict[str, dict] = {}

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
                # Requests exceptions can include a URL with apiKey; never persist it.
                why.append(f"oddspapi:historical_error({type(e).__name__})")
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

    def _infs_event(self, m) -> tuple[str | None, list[str]]:
        """match → InferSports event id（按开球 UTC 日期拉一次 events，队名+开球时间匹配；映射写入 map.json）。"""
        e = self.map.setdefault(m["match_id"], {})
        if e.get("infersports"):
            return e["infersports"], []
        from .consensus import find_event
        from .infersports import event_index
        day = parse_dt(m["kickoff_utc"]).strftime("%Y-%m-%d")
        try:
            if day not in self._infs_events:
                before = self.infs.usage().get("used", 0)
                self._infs_events[day] = event_index(self.infs.events(day))
                self.calls["infersports"] += self.infs.usage().get("used", 0) - before
        except Exception as ex:  # noqa: BLE001  含 BudgetExceeded
            _degrade_fact("odds_snapshots", "infersports_events", ex)
            return None, [f"infersports:events_error({type(ex).__name__})"]
        hit = find_event(self._infs_events[day], m["home"], m["away"], m["kickoff_utc"], self.aliases)
        if not hit:
            return None, ["infersports:no_event_match"]
        e["infersports"] = hit["id"]
        return hit["id"], []

    def _rows_infs(self, m, phase, now, ko) -> tuple[dict, list[str]]:
        """InferSports：open 用 /opening（每家 1 条初盘，opened_at 须早于开球）；report/close 用当前 /odds 全梯度。"""
        from .infersports import ah_ladders, ah_opening
        eid, why = self._infs_event(m)
        if not eid:
            return {}, why
        rows = []
        try:
            before = self.infs.usage().get("used", 0)
            if phase == "open":
                op_ = ah_opening(self.infs.opening(eid))
                for b, (line, ho, ao, ts) in sorted(op_.items()):
                    t = parse_dt(ts) if ts else None
                    if t is None or t >= ko:
                        continue
                    rows.append(self._row(m, phase, slug(b), "infersports", True, line, ho, ao, t, t, now, eid))
            else:
                lads = ah_ladders(self.infs.odds(eid, ttl=0 if phase == "close" else None))
                for b, lad in sorted(lads.items()):
                    lad = [(ln, ho, ao, parse_dt(ts) if ts else None) for ln, ho, ao, ts in lad]
                    for (line, ho, ao, ts), main in ladder_rows(lad):
                        rows.append(self._row(m, phase, slug(b), "infersports", main, line, ho, ao, now, ts, now, eid))
            self.calls["infersports"] += self.infs.usage().get("used", 0) - before
        except Exception as ex:  # noqa: BLE001
            return {}, [f"infersports:error({type(ex).__name__})"]
        return {phase: rows}, ([] if rows else [f"infersports:{phase}_no_ah_lines"])

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
        oh, oa = round(float(ho), 3), round(float(ao), 3)
        # 单线来源/公司：先标 0，assign_status→reconcile_is_main 再按 Pinnacle 主线决定
        if source and is_single_line(source, book):
            main = False
        return {"report_date": self.date, "match_id": m["match_id"], "fixture_id": m["fixture_id"] or "",
                "kickoff_utc": m["kickoff_utc"], "league_id": m["league_id"], "league": m["league"],
                "home": m["home"], "away": m["away"], "stage": phase, "bookmaker": book, "source": source,
                "is_main": "1" if main else "0", "ah_home_line": float(line) + 0.0, "odds_home": oh,
                "odds_away": oa, "odds_format": ODDS_FORMAT, "home_hk": round(oh - 1, 3),
                "away_hk": round(oa - 1, 3), "overround": round(overround(oh, oa), 4),
                "as_of": iso(as_of), "price_ts": iso(ts), "captured_at": iso(now),
                "status_detail": "",
                "source_ref": ref}

    # --- 主流程 ---
    def run(self, matches: list[dict], phases: list[str], as_of: datetime | None = None,
            backfill: bool = False) -> dict:
        now = self.now()
        as_of = as_of or now
        rows = load_rows(read_path(self.date, "csv", self.snap_dir, self.legacy_dir))
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
                use_infs = "infersports" in self.sources and self.infs is not None and self.infs.enabled
                if use_infs and (ph == "open" or live_ok):
                    got, why = self._rows_infs(m, ph, now, ko)
                    reasons[(m["match_id"], ph)] += why
                    for p2, rs in got.items():
                        replaced[(m["match_id"], p2, "infersports")] = rs
                if ph == "open":
                    if not use_infs:
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
                              "away_hk": "", "overround": "",
                              "status_detail": "; ".join(dict.fromkeys(reasons[(m["match_id"], ph)]))[:300]})
                    keep.append(r)
        status = assign_status(keep)
        write_rows(self.p["csv"], keep)
        _dump_json(self.p["map"], self.map)
        return {"verification": status, "rows": len(keep), "calls": dict(self.calls),
                "reasons": {f"{k[0]}|{k[1]}": v for k, v in reasons.items() if v}}


def write_summary(date: str, all_matches: list[dict], skipped: list[str], result: dict, phases, quota: dict,
                  snap_dir: Path | None = None, now: datetime | None = None, legacy_dir: Path | None = None) -> dict:
    p = paths(date, snap_dir)
    rows = load_rows(read_path(date, "csv", snap_dir, legacy_dir))
    status = assign_status(rows)
    prev = _load_json(read_path(date, "summary", snap_dir, legacy_dir), {})
    ms, counts = [], {ph: Counter() for ph in PHASES}
    for m in sorted(all_matches, key=lambda x: (x["kickoff_utc"], x["match_id"])):
        e = {k: m[k] for k in ("match_id", "kickoff_utc", "league_id", "home", "away", "list")}
        e["kickoff_bjt"] = parse_dt(m["kickoff_utc"]).astimezone(BJT).strftime("%Y-%m-%d %H:%M")
        e["phases"] = {}
        for ph in PHASES:
            s = status.get((m["match_id"], ph))
            e["phases"][ph] = s or {"verification": "pending"}
            counts[ph][(s or {}).get("verification", "pending")] += 1
        xcfg = load_cross_verification()
        if xcfg.get("enabled"):
            xv = cross_verification_for_rows(rows, m["match_id"], xcfg)
            e["cross_verification"] = xv
            e["deep_line"] = any(v.get("deep_line") for v in xv["phases"].values())
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


def quota_snapshot(papi=None, oddsapi=None, apif=None, infs=None) -> dict:
    q = {}
    if infs is not None and infs.enabled:
        u = infs.usage()
        q["infersports"] = {"used": u.get("used"), "remaining": infs.remaining(), "limit": u.get("limit"),
                            "tier": u.get("tier"), "day_utc": u.get("day")}
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
    ap.add_argument("--sources", default=",".join(SOURCES),
                    help=f"逗号分隔，可选 {','.join(ALL_SOURCES)}；infersports 免 key 已进默认（200 次/天/IP）")
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
    infs = None
    if "infersports" in sources:
        from .infersports import Client as InferSports
        infs = InferSports()
    synced = False
    for date in dates:
        rp = report_path(date)
        if not rp.exists():
            print(f"[snap] {date}: 报告不存在 {rp}", file=sys.stderr)
            continue
        report = json.loads(rp.read_text(encoding="utf-8"))
        allm, skipped = screened(report)
        existing = load_rows(read_path(date, "csv"))
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
                           apif_floor=a.apif_floor, aliases=load_aliases(), infs=infs)
        res = snap.run(todo, list(a.phases), as_of=as_of, backfill=a.backfill)
        q = quota_snapshot(papi, oddsapi, apif, infs)
        summ = write_summary(date, allm, skipped, res, a.phases, q, now=datetime.now(timezone.utc))
        if papi is not None and papi.backoffs:
            print(f"[snap] oddspapi 429 退避 {len(papi.backoffs)} 次", file=sys.stderr)
        print(json.dumps({"date": date, "matches": len(todo), "calls": res["calls"], "counts": summ["counts"],
                          "bookmakers": len(summ["bookmakers"]), "quota": q}, ensure_ascii=False))


if __name__ == "__main__":
    main()
