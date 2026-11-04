"""模型概率与 Pinnacle 无水市场概率融合 + 亚盘 edge。

- logit_blend: 二元概率（亚盘有效赢率）logit 线性融合 p = σ(w·logit(p_模型) + (1−w)·logit(p_市场))。
- loglinear_1x2: 1X2 对数线性融合后归一化。
- pool_matrices: 比分矩阵对数线性融合（M ∝ M_模型^w · M_市场^(1−w)），可直接在任何亚盘上结算。
- edge: 融合（或模型）比分矩阵下，所选一方在 line、十进制赔率 odds 时的每单位 EV（含四分之一盘）。
"""
from __future__ import annotations

import numpy as np

from . import ah_math as am

EPS = 1e-12


def _logit(p):
    p = np.clip(np.asarray(p, dtype=float), 1e-9, 1 - 1e-9)
    return np.log(p / (1 - p))


def logit_blend(p_model, p_market, w: float):
    z = w * _logit(p_model) + (1 - w) * _logit(p_market)
    return 1 / (1 + np.exp(-z))


def loglinear_1x2(p_model, p_market, w: float) -> np.ndarray:
    lp = w * np.log(np.clip(p_model, EPS, 1)) + (1 - w) * np.log(np.clip(p_market, EPS, 1))
    p = np.exp(lp - lp.max())
    return p / p.sum()


def pool_matrices(m_model: np.ndarray, m_market: np.ndarray, w: float) -> np.ndarray:
    n = min(m_model.shape[0], m_market.shape[0])
    a, b = m_model[:n, :n], m_market[:n, :n]
    lp = w * np.log(np.clip(a, EPS, 1)) + (1 - w) * np.log(np.clip(b, EPS, 1))
    m = np.exp(lp - lp.max())
    return m / m.sum()


def edge(matrix: np.ndarray, side: str, line: float, odds: float) -> float:
    """edge = 每单位本金 EV（四分之一盘按半注拆分；走盘退本金）。"""
    return am.ev(am.ah_probs(matrix, line, side), odds)
