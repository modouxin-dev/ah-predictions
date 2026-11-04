"""基线（“当前版本”）：Elo（带净胜球倍数）-> 让球深度 -> 独立 Poisson 比分矩阵。纯 numpy。

- 赛前 Elo 差 diff = R_主 + hfa − R_客；
- 用训练期（只含过去比赛）的 (diff, 主-客净胜球) 线性回归得 supremacy = a + b·diff，总进球 = 训练期均值；
- mu_主 = (T + sup)/2，mu_客 = (T − sup)/2。
- 新队（升班马）初始分 = 当前评分最低 3 队均值（联赛内已有 ≥ 10 队时），否则 init。
"""
from __future__ import annotations

import math

import numpy as np

from . import ah_math as am


class EloGoals:
    def __init__(self, k: float = 20.0, hfa: float = 60.0, init: float = 1500.0):
        self.k, self.hfa, self.init = k, hfa, init
        self.r: dict[str, float] = {}
        self._x: list[float] = []
        self._sup: list[float] = []
        self._tot: list[float] = []
        self.a, self.b, self.total = 0.3, 0.004, 2.6

    def rating(self, team: str) -> float:
        if team not in self.r:
            if len(self.r) >= 10:
                self.r[team] = float(np.mean(sorted(self.r.values())[:3]))
            else:
                self.r[team] = self.init
        return self.r[team]

    def diff(self, home: str, away: str) -> float:
        return self.rating(home) + self.hfa - self.rating(away)

    def update(self, home: str, away: str, hg: int, ag: int) -> None:
        d = self.diff(home, away)
        self._x.append(d)
        self._sup.append(hg - ag)
        self._tot.append(hg + ag)
        e = 1.0 / (1.0 + 10 ** (-d / 400))
        s = 1.0 if hg > ag else (0.5 if hg == ag else 0.0)
        mult = math.log(abs(hg - ag) + 1) + 1 if hg != ag else 1.0
        delta = self.k * mult * (s - e)
        self.r[home] += delta
        self.r[away] -= delta

    def refit_goal_map(self, last_n: int = 2000) -> None:
        if len(self._x) < 50:
            return
        x = np.array(self._x[-last_n:])
        y = np.array(self._sup[-last_n:])
        self.b, self.a = np.polyfit(x, y, 1)
        self.total = float(np.mean(self._tot[-last_n:]))

    def means(self, home: str, away: str) -> tuple[float, float]:
        sup = self.a + self.b * self.diff(home, away)
        sup = max(-self.total + 0.1, min(self.total - 0.1, sup))
        return (self.total + sup) / 2, (self.total - sup) / 2

    def matrix(self, home: str, away: str, max_goals: int = 12) -> np.ndarray:
        mh, ma = self.means(home, away)
        return am.poisson_matrix(mh, ma, max_goals)
