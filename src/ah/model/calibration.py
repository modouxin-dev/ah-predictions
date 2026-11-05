"""概率校准（模型 B = 校准后的 Dixon-Coles）。只用过去数据拟合（walk-forward 由调用方保证）。

- 1X2：三分类温度缩放 p_i ∝ p_i^(1/T)。
- 大小 2.5 / 亚盘：二分类温度缩放 q' = σ(logit(q)/T)。亚盘的 q = 所选方“领先”概率
  =(赢+赢半)/(赢+赢半+输半+输)（走盘剔除），与回测二元 log loss 口径一致。
- 亚盘 5 种结算概率重建：走盘概率不变，赢侧（赢、赢半）整体乘 α、输侧（输半、输）整体乘 β，
  使 α·a/(α·a+β·b)=q'、α·a+β·b=a+b。零概率项保持为零 → 整数盘半赢半输仍为 0、四分之一盘走盘仍为 0。
- T>1 = 原模型过度自信（压向 0.5）；T=1 = 不变。
- PAV 等渗回归只用于研究对比（不进实验盘 / 正式推荐）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

EPS = 1e-12
T_BOUNDS = (0.25, 4.0)


def temper_multiclass(p, T: float) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), EPS, None)
    q = np.power(p, 1.0 / T)
    return q / q.sum(axis=-1, keepdims=True)


def temper_binary(q, T: float) -> np.ndarray:
    q = np.clip(np.asarray(q, dtype=float), EPS, 1 - EPS)
    z = np.log(q / (1 - q)) / T
    return 1.0 / (1.0 + np.exp(-z))


def nll_multiclass(P, y, T: float) -> float:
    Q = temper_multiclass(P, T)
    return float(-np.mean(np.log(np.clip(Q[np.arange(len(y)), np.asarray(y)], EPS, None))))


def nll_binary(q, y, T: float) -> float:
    qq = temper_binary(q, T)
    y = np.asarray(y, dtype=float)
    return float(-np.mean(y * np.log(qq) + (1 - y) * np.log(1 - qq)))


def fit_temperature(nll, bounds=T_BOUNDS) -> float:
    from scipy.optimize import minimize_scalar

    r = minimize_scalar(
        lambda lt: nll(float(np.exp(lt))),
        bounds=(np.log(bounds[0]), np.log(bounds[1])),
        method="bounded",
        options={"xatol": 1e-4},
    )
    return float(np.exp(r.x))


def fit_t_multiclass(P, y) -> float:
    P, y = np.asarray(P, dtype=float), np.asarray(y, dtype=int)
    if len(y) == 0:
        return 1.0
    return fit_temperature(lambda T: nll_multiclass(P, y, T))


def fit_t_binary(q, y) -> float:
    q, y = np.asarray(q, dtype=float), np.asarray(y, dtype=float)
    if len(y) == 0:
        return 1.0
    return fit_temperature(lambda T: nll_binary(q, y, T))


def ahead_prob(probs: dict) -> float:
    """(赢+赢半)/(1−走)，走盘接近 1 时返回 nan。"""
    a = float(probs["win"] + probs["half_win"])
    b = float(probs["half_loss"] + probs["loss"])
    s = a + b
    return a / s if s > EPS else float("nan")


def recalibrate_ah_to_q(probs: dict, q_target: float) -> dict:
    """把亚盘 5 类概率的领先侧重标定到 q_target（走盘不变，零项保持为零）。"""
    a = probs["win"] + probs["half_win"]
    b = probs["half_loss"] + probs["loss"]
    push = float(probs["push"])
    if a <= EPS or b <= EPS:
        return dict(probs)
    q2 = float(np.clip(q_target, EPS, 1 - EPS))
    # α·a + β·b = a+b, α·a / (α·a+β·b) = q2  → α/β = q2·b / ((1-q2)·a)
    r = q2 * b / ((1 - q2) * a)
    beta = (a + b) / (r * a + b)
    alpha = r * beta
    out = {
        "win": float(probs["win"]) * alpha,
        "half_win": float(probs["half_win"]) * alpha,
        "push": push,
        "half_loss": float(probs["half_loss"]) * beta,
        "loss": float(probs["loss"]) * beta,
    }
    # 数值归一（push 固定后赢输侧和应为 a+b）
    side = out["win"] + out["half_win"] + out["half_loss"] + out["loss"]
    target_side = a + b
    if side > EPS and abs(side - target_side) > 1e-9:
        scale = target_side / side
        for k in ("win", "half_win", "half_loss", "loss"):
            out[k] *= scale
    return out


def recalibrate_ah(probs: dict, T: float) -> dict:
    """亚盘 5 种结算概率按二分类温度缩放重建（见模块说明）。"""
    if T == 1.0:
        return dict(probs)
    q = ahead_prob(probs)
    if not np.isfinite(q):
        return dict(probs)
    return recalibrate_ah_to_q(probs, float(temper_binary(q, T)))


@dataclass(frozen=True)
class TemperatureCalibrator:
    t_1x2: float = 1.0
    t_ah: float = 1.0
    t_ou: float = 1.0

    def p1x2(self, p) -> np.ndarray:
        return temper_multiclass(p, self.t_1x2)

    def p_over(self, q) -> float:
        return float(temper_binary(q, self.t_ou))

    def ah(self, probs: dict) -> dict:
        return recalibrate_ah(probs, self.t_ah)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- 研究对比：PAV 等渗回归
def pav_fit(x, y):
    """返回 (阈值 xs, 取值 vs)，单调不减。xs 为每个块右端点。"""
    o = np.argsort(np.asarray(x, float))
    xs = np.asarray(x, float)[o]
    ys = np.asarray(y, float)[o]
    vals: list[float] = []
    wts: list[float] = []
    right: list[float] = []
    for xi, yi in zip(xs, ys):
        vals.append(float(yi))
        wts.append(1.0)
        right.append(float(xi))
        while len(vals) > 1 and vals[-2] > vals[-1]:
            w = wts[-2] + wts[-1]
            v = (vals[-2] * wts[-2] + vals[-1] * wts[-1]) / w
            vals[-2:] = [v]
            wts[-2:] = [w]
            right[-2:] = [right[-1]]
    return np.array(right), np.array(vals)


def pav_predict(model, x, clip=(0.01, 0.99)) -> np.ndarray:
    xs, vs = model
    if len(xs) == 0:
        return np.full(np.shape(x), 0.5, dtype=float)
    idx = np.searchsorted(xs, np.asarray(x, float), side="left")
    idx = np.clip(idx, 0, len(vs) - 1)
    return np.clip(vs[idx], *clip)
