"""评分框架：把赛程+盘口+基本面特征转为候选推荐，只输出超过阈值的场次。

设计：每个因子给 -2..+2 分（正=支持所选一方），加权求和 -> score；
score >= settings.min_score_to_publish 才输出；按 score 映射信心等级。
基本面特征：injuries / h2h / form 可由 enrich.enrich()（API-Football）自动给出参考值，
通过 apply_enrichment() 填入（不覆盖研究代理已手工给出的值）；xG、战意等仍需人工研究。
铁律：没有真实盘口(line)的比赛一律不输出。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from . import CONFIG

WEIGHTS = {
    "line_movement": 1.5,     # 初盘->即时 朝所选一方升盘
    "form": 1.2,              # 近期状态(近5-8场)
    "home_away": 0.8,         # 主客场战绩
    "injuries": 1.2,          # 伤停对比（正=对手伤停更重）
    "xg": 1.0,                # 进攻/防守质量
    "motivation": 0.8,        # 排名/战意
    "h2h": 0.3,
    "line_value": 1.0,        # 盘口深度惩罚：深盘/已大幅升盘 → 负分
}


@dataclass
class Candidate:
    match_id: str
    league: str
    home: str
    away: str
    kickoff_utc: str
    pick_side: str
    line: float | None
    odds: float | None
    line_open: float | None = None
    features: dict = field(default_factory=dict)  # 因子名 -> -2..+2
    reasons: list = field(default_factory=list)
    risks: list = field(default_factory=list)
    sources: list = field(default_factory=list)


def load_settings() -> dict:
    return yaml.safe_load((CONFIG / "settings.yaml").read_text(encoding="utf-8"))


def line_movement_score(line_open: float | None, line_now: float | None) -> float:
    """所选一方视角：line 越负=让越多。升盘(更负)给正分，每 0.25 记 1 分，封顶 ±2。"""
    if line_open is None or line_now is None:
        return 0.0
    return max(-2.0, min(2.0, (line_open - line_now) / 0.25))


def score(c: Candidate) -> float:
    f = dict(c.features)
    f.setdefault("line_movement", line_movement_score(c.line_open, c.line))
    return round(sum(WEIGHTS.get(k, 0) * max(-2, min(2, v)) for k, v in f.items()), 2)


def confidence(s: float, cfg: dict) -> str | None:
    th = cfg["confidence_thresholds"]
    if s >= th["high"]:
        return "high"
    if s >= th["medium"]:
        return "medium"
    if s >= th["low"]:
        return "low"
    return None


def select(cands: list[Candidate], cfg: dict | None = None) -> list[dict]:
    cfg = cfg or load_settings()
    order = {"high": 0, "medium": 1, "low": 2}
    picks = []
    for c in cands:
        if c.line is None or not c.sources:
            continue
        s = score(c)
        conf = confidence(s, cfg)
        if conf is None or order[conf] > order[cfg["min_confidence_to_publish"]]:
            continue
        picks.append({"match_id": c.match_id, "league": c.league, "home": c.home, "away": c.away,
                      "kickoff_utc": c.kickoff_utc, "pick_side": c.pick_side, "line": c.line,
                      "line_open": c.line_open, "odds": c.odds, "confidence": conf, "score": s,
                      "reasons": c.reasons, "risks": c.risks, "sources": c.sources})
    picks.sort(key=lambda p: -p["score"])
    return picks[: cfg["max_picks_per_day"]]


def apply_enrichment(c: Candidate, e: dict) -> Candidate:
    """把 enrich.enrich() 的 suggested_features 填进候选（已有的手工值优先），并记录数据源。"""
    for k, v in (e.get("suggested_features") or {}).items():
        c.features.setdefault(k, v)
    if e.get("api_fixture_id"):
        src = f"API-Football fixture {e['api_fixture_id']}"
        if src not in c.sources:
            c.sources.append(src)
    return c
