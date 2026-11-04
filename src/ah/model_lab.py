"""模型实验盘（experiment track）：读取、宽容解析、按「模型 × 玩法」统计。

字段依据：docs/roadmap.md「模型实验盘」——每条记录带 track（official/experiment）、model_id、market、
盘口、水位和时间戳；model_version 沿用 model/v0（research/README.md）的写法。审计员 paper_trades_schema.md
（audit/paper-trades @1a1c42a）尚未定义 track/model_id/market，其余列名（clv、units/pnl、ah_outcome、
odds_format、close_odds/odds_close 等）按该文档对齐。所有列名别名集中在 COLUMNS / MARKETS / OUTCOMES。

实验盘不计入正式命中率；数据缺失时调用方显示「暂无」或省略，绝不补造。
"""
from __future__ import annotations

import csv
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

from . import DATA

LAB_FILE = DATA / "model_lab.csv"   # 约定路径（待审计员确认）；不存在即视为无数据
BJ = timezone(timedelta(hours=8))

# ---- 列名映射（唯一一处）：标准名 -> 可接受的别名（不区分大小写，按顺序取第一个非空）----
COLUMNS: dict[str, tuple[str, ...]] = {
    "date": ("date", "report_date", "日期"),
    "match_id": ("match_id", "fixture_id", "id"),
    "track": ("track", "轨道"),
    "model_id": ("model_id", "model", "model_name", "模型"),
    "model_version": ("model_version", "version"),
    "model_label": ("model_label", "baseline_label", "label"),
    "market": ("market", "bet_type", "玩法"),
    "league": ("league", "competition", "赛事"),
    "home": ("home", "home_team", "主队"),
    "away": ("away", "away_team", "客队"),
    "kickoff_utc": ("kickoff_utc", "kickoff"),
    "kickoff_bj": ("kickoff_bj",),
    "selection": ("selection", "pick", "pick_team", "pick_side", "side", "选项"),
    "line": ("line", "handicap", "total_line", "盘口"),
    "odds": ("odds", "odds_taken", "price", "水位"),
    "odds_format": ("odds_format",),
    "odds_close": ("odds_close", "close_odds"),
    "clv": ("clv", "clv_pct"),
    "status": ("status", "ledger_status"),
    "stake": ("stake",),
    "result": ("result", "score"),
    "outcome": ("outcome", "ah_outcome", "settlement"),
    "units": ("units", "pnl", "profit"),
    "model_p": ("model_p",),
    "brier": ("brier",),
    "note": ("note",),
    "generated_at": ("generated_at", "updated_at", "submitted_at"),
}
MARKETS = {"ah": "ah", "asian_handicap": "ah", "asian handicap": "ah", "亚盘": "ah", "handicap": "ah",
           "1x2": "1x2", "h2h": "1x2", "moneyline": "1x2", "胜平负": "1x2", "match_winner": "1x2",
           "ou": "ou", "o/u": "ou", "totals": "ou", "total": "ou", "over_under": "ou", "大小球": "ou", "goals": "ou"}
MARKET_CN = {"ah": "亚盘", "1x2": "1X2", "ou": "大小球"}
MARKET_ORDER = ("ah", "1x2", "ou")
OUTCOMES = {"win": "win", "w": "win", "赢": "win", "half_win": "half_win", "赢半": "half_win",
            "push": "push", "走": "push", "走水": "push", "half_loss": "half_loss", "输半": "half_loss",
            "loss": "loss", "lose": "loss", "l": "loss", "输": "loss", "void": "void", "取消": "void"}
SEL_1X2 = {"home": "主胜", "1": "主胜", "h": "主胜", "draw": "平", "x": "平", "d": "平", "away": "客胜", "2": "客胜", "a": "客胜"}
SEL_OU = {"over": "大", "o": "大", "大": "大", "under": "小", "u": "小", "小": "小"}


def _num(x) -> float | None:
    try:
        s = str(x).strip().rstrip("%")
        return None if s in ("", "None", "nan", "—", "-") else float(s)
    except (TypeError, ValueError):
        return None


def normalize(raw: dict) -> dict | None:
    """一行 CSV → 标准字段。不是实验盘（track=official）或缺模型/玩法时返回 None。"""
    low = {str(k).strip().lower(): ("" if v is None else str(v).strip()) for k, v in raw.items() if k is not None}
    r = {}
    for k, names in COLUMNS.items():
        r[k] = next((low[n.lower()] for n in names if low.get(n.lower())), "")
    track = r["track"].lower()
    if track and track not in ("experiment", "exp", "实验", "实验盘"):
        return None                                   # 正式推荐（official）不进实验场
    r["model_id"] = r["model_id"] or r["model_version"]
    r["market"] = MARKETS.get(r["market"].lower(), "")
    if not r["model_id"] or not r["market"]:
        return None
    for k in ("line", "odds", "odds_close", "clv", "units", "model_p", "brier", "stake"):
        r[k] = _num(r[k])
    if r["clv"] is not None and abs(r["clv"]) > 1.5:   # 写成百分数的 clv_pct（如 3.2）→ 小数
        r["clv"] /= 100
    r["outcome"] = OUTCOMES.get(r["outcome"].lower(), "")
    r["odds_format"] = (r["odds_format"] or "hk").lower()
    if not r["date"] and r["kickoff_bj"]:
        r["date"] = r["kickoff_bj"][:10]
    return r


def load(path: Path | str | None = None) -> list[dict]:
    p = Path(path) if path else LAB_FILE
    if not p.exists():
        return []
    with p.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in (normalize(x) for x in csv.DictReader(f)) if r]


def for_date(rows: list[dict], d: str) -> list[dict]:
    return [r for r in rows if r["date"] == d]


def by_market(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """[(market, rows)]，按 亚盘/1X2/大小球 排序，空组省略。"""
    return [(m, [r for r in rows if r["market"] == m]) for m in MARKET_ORDER if any(r["market"] == m for r in rows)]


def short_team(name: str) -> str:
    """「波尔图 Porto」→「波尔图」；纯外文名原样返回。"""
    n = str(name or "").strip()
    head = n.split(" ")[0]
    return head if head and any("\u4e00" <= c <= "\u9fff" for c in head) else n


def selection_text(r: dict) -> str:
    """选项（不含盘口）：亚盘=球队；1X2=主胜/平/客胜；大小球=大/小。"""
    s = r["selection"]
    if r["market"] == "1x2":
        return SEL_1X2.get(s.lower(), s or "暂无")
    if r["market"] == "ou":
        return SEL_OU.get(s.lower(), s or "暂无")
    if s.lower() in ("home", "away"):
        return short_team(r[s.lower()])
    return short_team(s) or "暂无"


def line_text(r: dict) -> str:
    x = r["line"]
    if r["market"] == "1x2" or x is None:
        return ""
    if r["market"] == "ou":
        return f"{x:g}"
    return "0" if x == 0 else f"{x:+g}"


def odds_text(r: dict) -> str:
    return "" if r["odds"] is None else f"{r['odds']:.2f}"


def decimal_odds(r: dict) -> float | None:
    if r["odds"] is None:
        return None
    return r["odds"] if r["odds_format"] == "decimal" else 1 + r["odds"]


def _units(r: dict) -> float | None:
    """结算单位：优先用记录里的 units；否则按结果和赔率算（1 单位平注）。"""
    if r["units"] is not None:
        return r["units"]
    o, d = r["outcome"], decimal_odds(r)
    if not o or (d is None and o in ("win", "half_win")):
        return None
    return {"win": d - 1 if d else None, "half_win": (d - 1) / 2 if d else None, "push": 0.0, "void": 0.0,
            "half_loss": -0.5, "loss": -1.0}[o]


def leaderboard(rows: list[dict], start: str | None = None, end: str | None = None) -> list[dict]:
    """按「模型 × 玩法」统计（只算已结算）。命中率 = (赢 + 0.5×赢半) / (场数 − 走 − 取消)；ROI = 盈亏 / 投注单位。"""
    agg: dict[tuple[str, str], dict] = {}
    for r in rows:
        if (start and r["date"] < start) or (end and r["date"] > end):
            continue
        k = (r["model_id"], r["market"])
        a = agg.setdefault(k, {"model_id": k[0], "market": k[1], "label": r["model_label"], "picks": 0, "settled": 0,
                               "win": 0, "half_win": 0, "push": 0, "half_loss": 0, "loss": 0, "void": 0,
                               "units": 0.0, "staked": 0.0, "clv": [], "brier": []})
        a["picks"] += 1
        if r["clv"] is not None:
            a["clv"].append(r["clv"])
        if r["brier"] is not None:
            a["brier"].append(r["brier"])
        o, u = r["outcome"], _units(r)
        if not o:
            continue
        a["settled"] += 1
        a[o] += 1
        if u is not None:
            a["units"] += u
            if o != "void":
                a["staked"] += 1.0 if r["stake"] is None else r["stake"]
    out = []
    for a in agg.values():
        dec = a["settled"] - a["push"] - a["void"]
        a["decided"] = dec
        a["hit_rate"] = (a["win"] + 0.5 * a["half_win"]) / dec if dec else None
        a["roi"] = a["units"] / a["staked"] if a["staked"] else None
        a["clv_n"] = len(a["clv"])
        a["clv"] = sum(a["clv"]) / len(a["clv"]) if a["clv"] else None
        a["brier"] = sum(a["brier"]) / len(a["brier"]) if a["brier"] else None
        out.append(a)
    key = lambda a: (a["roi"] is None, -(a["roi"] or 0), -(a["hit_rate"] or 0), -a["settled"], a["model_id"])  # noqa: E731
    return sorted(out, key=key)


def week_range(end: str | None = None) -> tuple[str, str]:
    e = _date.fromisoformat(end) if end else datetime.now(BJ).date()
    return (e - timedelta(days=6)).isoformat(), e.isoformat()


def pct(x: float | None, signed: bool = False) -> str:
    if x is None:
        return "暂无"
    return f"{x:+.1%}" if signed else f"{x:.1%}"


def model_code(model_id: str) -> str:
    """紧凑代号：「A-market」→「A」（前缀 ≤3 个字符时）；否则取前 8 个字符。"""
    head = str(model_id).split("-")[0].split("_")[0]
    return head if 0 < len(head) <= 3 and head != model_id else str(model_id)[:8]


def legend(rows: list[dict]) -> str:
    """代号图例：「A 市场模型 · B 进球模型」；只列代号与原名不同的模型，没有就返回空串。"""
    seen: dict[str, str] = {}
    for r in rows:
        c = model_code(r["model_id"])
        if c != r["model_id"] and c not in seen:
            seen[c] = r["model_label"] or r["model_id"]
    return " · ".join(f"{c} {v}" for c, v in sorted(seen.items()))
