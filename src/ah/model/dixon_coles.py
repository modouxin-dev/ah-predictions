"""Dixon-Coles（时间衰减 + 主场优势 + rho 低比分修正），封装 penaltyblog（研究用，惰性导入）。

penaltyblog 不在 requirements.txt 中；研究环境用 ``.venv/bin/pip install penaltyblog``。
每个联赛单独拟合 -> 主场优势按联赛估计。可选与 Elo 基线矩阵对数线性融合（w_elo）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def fit(train: pd.DataFrame, ref_date, xi: float = 0.0018, window_days: int = 1095):
    """train 需含 date, home, away, hg, ag；只用 ref_date 之前、window_days 内的比赛。"""
    import penaltyblog as pb

    ref_date = pd.Timestamp(ref_date)
    t = train[(train["date"] < ref_date) & (train["date"] >= ref_date - pd.Timedelta(days=window_days))]
    w = pb.models.dixon_coles_weights(list(t["date"]), xi=xi, base_date=ref_date)
    m = pb.models.DixonColesGoalModel(t["hg"].astype(int).values, t["ag"].astype(int).values,
                                      t["home"].values, t["away"].values, weights=w)
    m.fit()
    m.n_train = t.groupby("home").size().add(t.groupby("away").size(), fill_value=0).to_dict()
    return m


def matrix(model, home: str, away: str, max_goals: int = 12) -> np.ndarray:
    g = model.predict(home, away, max_goals=max_goals)
    m = np.asarray(g.grid, dtype=float)
    return m / m.sum()


def params(model) -> dict:
    p = model.get_params()
    return {k: float(v) for k, v in p.items() if k in ("home_advantage", "rho")}
