"""市场基线：Pinnacle 亚盘去抽水 -> 公平价；我方推荐价格的 edge（含四分之一盘赢半/输半）。

唯一对外门槛函数：``market_gate(...)``。

- 同一盘口：公平赔率 = 1 / 无水有效赢率（精确，与总进球假设无关）。
- 盘口不同（例如我们拿到 -0.75，Pinnacle 主盘是 -0.5）：用 Pinnacle 无水价在固定总进球期望下
  反解 Poisson 让球深度，得到市场隐含比分矩阵，再在我方盘口上算赢/赢半/走/输半/输概率与 EV
  （此时结果依赖 ``total``，可传 Pinnacle 大小球主盘中位数；``line_converted=True`` 标记）。
- ``ev``：每 1 单位本金的期望收益 = W·(o−1) − L（W/L 为半注折算后的赢/输份额）。
- ``price_edge``：o / o_fair − 1（= 未退回本金部分的收益率，也是 CLV 的口径）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import ah_math as am


@dataclass
class GateResult:
    passes: bool
    ev: float                 # 每单位本金 EV（市场公平价下）
    price_edge: float         # odds / fair_odds - 1
    fair_odds: float          # 我方盘口、我方一侧的无水公平十进制赔率
    offered_odds: float       # 十进制
    pick_side: str
    pick_line: float          # 所选一方视角
    pin_home_line: float
    p_market_eff: float       # 我方一侧无水有效赢率
    probs: dict = field(default_factory=dict)   # 我方盘口 5 种结算概率（市场隐含）
    line_converted: bool = False
    method: str = "multiplicative"
    min_ev: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def market_gate(pick_side: str, pick_line: float, pick_odds: float,
                pin_home_line: float, pin_home_odds: float, pin_away_odds: float,
                *, odds_format: str = "decimal", method: str = "multiplicative",
                total: float = 2.6, min_ev: float = 0.0) -> GateResult:
    """我方推荐（pick_side, pick_line 所选一方视角, pick_odds）相对 Pinnacle 无水价是否有正 EV。

    odds_format: "decimal" 或 "hk"（港盘水位，作用于 pick_odds 与 Pinnacle 两个价格）。
    method: multiplicative | power | shin。
    passes = ev >= min_ev（价格不低于 Pinnacle 无水价即 min_ev=0）。
    """
    if pick_side not in ("home", "away"):
        raise ValueError("pick_side must be home/away")
    am.check_line(pick_line)
    am.check_line(pin_home_line)
    conv = am.hk_to_decimal if odds_format == "hk" else float
    o, ph, pa = conv(pick_odds), conv(pin_home_odds), conv(pin_away_odds)
    if min(o, ph, pa) <= 1.0:
        raise ValueError("十进制赔率必须 > 1（港盘水位请传 odds_format='hk'）")

    p_home, p_away = am.devig([ph, pa], method)
    home_line = pick_line if pick_side == "home" else -pick_line
    converted = abs(home_line - pin_home_line) > 1e-9
    m = am.implied_matrix_from_ah(pin_home_line, float(p_home), total=total)
    probs = am.ah_probs(m, pick_line, pick_side)
    if converted:
        fair = am.fair_odds(probs)
        p_eff = am.effective_prob(probs)
    else:
        p_eff = float(p_home if pick_side == "home" else p_away)
        fair = 1.0 / p_eff
    w, l_ = am.win_lose_share(probs)
    price_edge = o / fair - 1.0
    # 同盘口时 EV = (W+L)·price_edge（W+L 为未退回本金份额）；换盘时直接按隐含概率算
    ev = (w + l_) * price_edge if not converted else am.ev(probs, o)
    passes = ev >= min_ev
    reason = (f"EV {ev:+.2%} {'≥' if passes else '<'} 门槛 {min_ev:+.2%}；"
              f"我方 {o:.3f} vs 无水公平 {fair:.3f}" + ("（换盘估算）" if converted else ""))
    return GateResult(passes, ev, price_edge, fair, o, pick_side, pick_line, pin_home_line,
                      p_eff, probs, converted, method, min_ev, reason)


def clv(bet_odds: float, close_fair_odds: float) -> float:
    """CLV = 下注赔率 / 收盘无水公平赔率 − 1（同一盘口）。"""
    return bet_odds / close_fair_odds - 1.0
