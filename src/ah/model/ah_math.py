"""亚盘数学（纯 numpy，无外部模型依赖）。

约定
----
- 比分矩阵 ``M[i, j] = P(主队 i 球, 客队 j 球)``。
- ``line`` 为 **所选一方** 的让球数（与 settle.py / 日报一致）：负=让球，正=受让。
  主队视角 line=-0.75 即主让半一；客队对应 line=+0.75。
- 赔率一律为 **十进制**（decimal）。港盘水位 hk 用 ``hk_to_decimal`` 转换（decimal = 1 + hk）。
- 四分之一盘拆成相邻两条线各半注：-0.75 = 半注 -0.5 + 半注 -1.0。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import numpy as np

OUTCOMES = ("win", "half_win", "push", "half_loss", "loss")


def hk_to_decimal(hk: float) -> float:
    return 1.0 + float(hk)


def decimal_to_hk(dec: float) -> float:
    return float(dec) - 1.0


def check_line(line: float) -> None:
    if (Decimal(str(line)) * 4) % 1 != 0:
        raise ValueError(f"非法盘口 {line}，必须是 0.25 的倍数")


def split_line(line: float) -> tuple[float, ...]:
    """整/半盘返回 (line,)；四分之一盘返回相邻两条线。"""
    check_line(line)
    ln = Decimal(str(line))
    if (ln * 2) % 1 == 0:
        return (float(ln),)
    return (float(ln - Decimal("0.25")), float(ln + Decimal("0.25")))


def settle_margin(margin: int, line: float) -> str:
    """所选一方净胜球 margin + line 的结算结果（与 ah.settle.settle 等价，便于交叉测试）。"""
    s = 0.0
    parts = split_line(line)
    for p in parts:
        v = margin + p
        s += 1.0 if v > 1e-9 else (-1.0 if v < -1e-9 else 0.0)
    s /= len(parts)
    return {1.0: "win", 0.5: "half_win", 0.0: "push", -0.5: "half_loss", -1.0: "loss"}[s]


def margin_distribution(matrix: np.ndarray) -> dict[int, float]:
    """主队净胜球分布 {d: P(主-客 = d)}。"""
    m = np.asarray(matrix, dtype=float)
    n_h, n_a = m.shape
    out: dict[int, float] = {}
    for d in range(-(n_a - 1), n_h):
        out[d] = float(np.trace(m, offset=-d))
    return out


def ah_probs(matrix: np.ndarray, line: float, side: str = "home") -> dict[str, float]:
    """从比分矩阵计算所选一方在 line 下的 5 种结算概率。"""
    if side not in ("home", "away"):
        raise ValueError("side must be home/away")
    md = margin_distribution(matrix)
    tot = sum(md.values())
    res = dict.fromkeys(OUTCOMES, 0.0)
    for d, p in md.items():
        margin = d if side == "home" else -d
        res[settle_margin(margin, line)] += p / tot
    return res


def win_lose_share(probs: dict[str, float]) -> tuple[float, float]:
    """有效赢注份额 W 与输注份额 L（半赢/半输按半注计）。"""
    w = probs["win"] + probs["half_win"] / 2
    l_ = probs["loss"] + probs["half_loss"] / 2
    return w, l_


def ev(probs: dict[str, float], odds: float) -> float:
    """每 1 单位本金的期望收益（十进制赔率）。EV = W·(o−1) − L。"""
    w, l_ = win_lose_share(probs)
    return w * (odds - 1.0) - l_


def fair_odds(probs: dict[str, float]) -> float:
    """令 EV=0 的十进制赔率：o* = 1 + L/W = (W+L)/W。"""
    w, l_ = win_lose_share(probs)
    if w <= 0:
        return float("inf")
    return (w + l_) / w


def effective_prob(probs: dict[str, float]) -> float:
    """去掉走盘部分后的“有效赢率” W/(W+L) = 1/fair_odds；主客两方之和恰为 1。"""
    w, l_ = win_lose_share(probs)
    return w / (w + l_) if (w + l_) > 0 else 0.5


@dataclass
class FairLine:
    line: float          # 主队视角
    home_fair_odds: float
    away_fair_odds: float
    home_probs: dict
    away_probs: dict


def fair_line(matrix: np.ndarray, lo: float = -5.0, hi: float = 5.0) -> FairLine:
    """主队视角下主客公平赔率最接近（有效赢率最接近 50/50）的盘口。"""
    best = None
    k = int(round(lo * 4))
    while k <= int(round(hi * 4)):
        ln = k / 4
        hp = ah_probs(matrix, ln, "home")
        ap = ah_probs(matrix, -ln, "away")
        gap = abs(effective_prob(hp) - 0.5)
        if best is None or gap < best[0] - 1e-12:
            best = (gap, FairLine(ln, fair_odds(hp), fair_odds(ap), hp, ap))
        k += 1
    return best[1]


def probs_1x2(matrix: np.ndarray) -> tuple[float, float, float]:
    m = np.asarray(matrix, dtype=float)
    m = m / m.sum()
    return float(np.tril(m, -1).sum()), float(np.trace(m)), float(np.triu(m, 1).sum())


def poisson_matrix(mu_h: float, mu_a: float, max_goals: int = 12, rho: float = 0.0) -> np.ndarray:
    """独立 Poisson 比分矩阵（可选 Dixon-Coles rho 低比分修正）。"""
    from scipy.stats import poisson

    g = np.arange(max_goals + 1)
    m = np.outer(poisson.pmf(g, mu_h), poisson.pmf(g, mu_a))
    if rho:
        m[0, 0] *= 1 - mu_h * mu_a * rho
        m[0, 1] *= 1 + mu_h * rho
        m[1, 0] *= 1 + mu_a * rho
        m[1, 1] *= 1 - rho
    return m / m.sum()


# ---------------------------------------------------------------- 去抽水
def devig_multiplicative(odds) -> np.ndarray:
    inv = 1.0 / np.asarray(odds, dtype=float)
    return inv / inv.sum()


def devig_power(odds, tol: float = 1e-12) -> np.ndarray:
    """p_i = (1/o_i)^k，求 k 使 Σp_i = 1（对热门压缩较少，修正冷门偏差）。"""
    from scipy.optimize import brentq

    inv = 1.0 / np.asarray(odds, dtype=float)
    if abs(inv.sum() - 1) < tol:
        return inv / inv.sum()
    k = brentq(lambda k: (inv ** k).sum() - 1.0, 1e-6, 50.0, xtol=tol)
    p = inv ** k
    return p / p.sum()


def devig_shin(odds, tol: float = 1e-12) -> np.ndarray:
    """Shin (1993) 内幕交易模型去抽水，求 z。"""
    from scipy.optimize import brentq

    inv = 1.0 / np.asarray(odds, dtype=float)
    s = inv.sum()
    if abs(s - 1) < tol:
        return inv / s

    def probs(z):
        return (np.sqrt(z * z + 4 * (1 - z) * inv * inv / s) - z) / (2 * (1 - z))

    z = brentq(lambda z: probs(z).sum() - 1.0, 0.0, 0.999, xtol=tol)
    p = probs(z)
    return p / p.sum()


DEVIG = {"multiplicative": devig_multiplicative, "power": devig_power, "shin": devig_shin}


def devig(odds, method: str = "multiplicative") -> np.ndarray:
    return DEVIG[method](odds)


# ---------------------------------------------------------------- 市场隐含比分矩阵（换盘用）
def implied_matrix_from_ah(home_line: float, p_home_eff: float, total: float = 2.6,
                           max_goals: int = 12) -> np.ndarray:
    """给定主队视角盘口与无水后主队有效赢率，在固定总进球期望下解出 Poisson 让球深度，
    返回市场隐含比分矩阵（用于把收盘价换算到不同盘口）。"""
    from scipy.optimize import brentq

    def f(sup):
        mh, ma = (total + sup) / 2, (total - sup) / 2
        return effective_prob(ah_probs(poisson_matrix(mh, ma, max_goals), home_line, "home")) - p_home_eff

    lo, hi = -total + 0.02, total - 0.02
    if f(lo) > 0:
        sup = lo
    elif f(hi) < 0:
        sup = hi
    else:
        sup = brentq(f, lo, hi, xtol=1e-10)
    return poisson_matrix((total + sup) / 2, (total - sup) / 2, max_goals)


def implied_matrix(home_line: float, p_home_eff: float, p_over25: float | None = None,
                   total: float = 2.6, max_goals: int = 12) -> np.ndarray:
    """市场隐含比分矩阵：用亚盘无水有效赢率定让球深度；若给出大 2.5 球无水概率则同时反解总进球。"""
    if p_over25 is None:
        return implied_matrix_from_ah(home_line, p_home_eff, total, max_goals)
    from scipy.optimize import brentq

    def over(t):
        m = implied_matrix_from_ah(home_line, p_home_eff, t, max_goals)
        return 1.0 - float(sum(m[i, j] for i in range(3) for j in range(3 - i))) - p_over25

    lo, hi = 1.0, 5.0
    if over(lo) > 0:
        t = lo
    elif over(hi) < 0:
        t = hi
    else:
        t = brentq(over, lo, hi, xtol=1e-6)
    return implied_matrix_from_ah(home_line, p_home_eff, t, max_goals)
