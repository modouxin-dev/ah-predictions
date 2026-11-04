"""亚盘“结算得分”口径（与回测审计员约定，2026-11-04）。

- 结算得分 y：赢 1，赢半 0.75，走 0.5，输半 0.25，输 0（走盘不剔除）。
- 预测 p_cover = 期望结算得分 = P赢 + 0.75·P赢半 + 0.5·P走 + 0.25·P输半。
- Brier = mean((p_cover − y)²)，只算已结算场次，按推荐方视角。
- 概率校验：整数盘 → P赢半 = P输半 = 0；四分之一盘 → P走 = 0；半球盘 → 走/赢半/输半 都为 0；五项和为 1（1e-9）。
"""
from __future__ import annotations

from decimal import Decimal

import numpy as np

SETTLED_SCORE = {"win": 1.0, "half_win": 0.75, "push": 0.5, "half_loss": 0.25, "loss": 0.0}
TOL = 1e-9


def settled_score(outcome: str) -> float:
    return SETTLED_SCORE[outcome]


def expected_score(probs: dict) -> float:
    """model_p / p_cover。"""
    return float(sum(SETTLED_SCORE[k] * float(probs[k]) for k in SETTLED_SCORE))


def brier_settled(p_cover, outcomes) -> float:
    p = np.asarray(p_cover, dtype=float)
    y = np.array([settled_score(o) for o in outcomes], dtype=float)
    if len(p) != len(y) or len(p) == 0:
        raise ValueError("p_cover 与 outcomes 长度须相同且非空")
    return float(np.mean((p - y) ** 2))


def line_kind(line: float) -> str:
    q = Decimal(str(line)) * 4
    if q % 1 != 0:
        raise ValueError(f"非法盘口 {line}")
    q = int(q) % 4
    return {0: "integer", 2: "half"}.get(q, "quarter")


def validate_probs(probs: dict, line: float, tol: float = TOL) -> None:
    """不满足口径时抛 ValueError。"""
    keys = ("win", "half_win", "push", "half_loss", "loss")
    vals = [float(probs[k]) for k in keys]
    if any(v < -tol for v in vals):
        raise ValueError(f"概率为负: {probs}")
    if abs(sum(vals) - 1.0) > tol:
        raise ValueError(f"五项概率和 {sum(vals)!r} ≠ 1")
    kind = line_kind(line)
    if kind == "integer" and (abs(probs["half_win"]) > tol or abs(probs["half_loss"]) > tol):
        raise ValueError(f"整数盘 {line} 不应有赢半/输半")
    if kind == "quarter" and abs(probs["push"]) > tol:
        raise ValueError(f"四分之一盘 {line} 不应有走盘")
    if kind == "half" and any(abs(probs[k]) > tol for k in ("push", "half_win", "half_loss")):
        raise ValueError(f"半球盘 {line} 不应有走盘/赢半/输半")
