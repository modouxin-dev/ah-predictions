import numpy as np
import pytest

from ah.model import ah_math as am
from ah.model.market import clv, market_gate
from ah.settle import settle


def mat(cells, n=6):
    m = np.zeros((n, n))
    for (h, a), p in cells.items():
        m[h, a] = p
    return m


@pytest.mark.parametrize("line", [x / 4 for x in range(-14, 15)])
@pytest.mark.parametrize("margin", range(-5, 6))
def test_settle_margin_matches_repo_settle(margin, line):
    pg, og = (margin, 0) if margin >= 0 else (0, -margin)
    assert am.settle_margin(margin, line) == settle(pg, og, line)


def test_split_line():
    assert am.split_line(-0.75) == (-1.0, -0.5)
    assert am.split_line(0.25) == (0.0, 0.5)
    assert am.split_line(-1.0) == (-1.0,)
    with pytest.raises(ValueError):
        am.split_line(0.1)


def test_quarter_minus_025_hand():
    # 1-0: 0.5, 0-0: 0.3, 0-1: 0.2 ; 主 -0.25（平半）
    m = mat({(1, 0): 0.5, (0, 0): 0.3, (0, 1): 0.2})
    p = am.ah_probs(m, -0.25, "home")
    assert p == pytest.approx({"win": 0.5, "half_win": 0, "push": 0, "half_loss": 0.3, "loss": 0.2})
    # W=0.5, L=0.2+0.15=0.35 -> EV@2.0 = 0.5*1 - 0.35 = 0.15 ; fair = 0.85/0.5 = 1.7
    assert am.ev(p, 2.0) == pytest.approx(0.15)
    assert am.fair_odds(p) == pytest.approx(1.7)
    # 客 +0.25：0-0 赢半
    q = am.ah_probs(m, 0.25, "away")
    assert q == pytest.approx({"win": 0.2, "half_win": 0.3, "push": 0, "half_loss": 0, "loss": 0.5})
    assert am.effective_prob(p) + am.effective_prob(q) == pytest.approx(1.0)


def test_quarter_minus_075_hand():
    # 2-0: 0.4, 1-0: 0.35, 0-0: 0.25 ; 主 -0.75（半一）
    m = mat({(2, 0): 0.4, (1, 0): 0.35, (0, 0): 0.25})
    p = am.ah_probs(m, -0.75, "home")
    assert p == pytest.approx({"win": 0.4, "half_win": 0.35, "push": 0, "half_loss": 0, "loss": 0.25})
    # W = 0.4 + 0.175 = 0.575, L = 0.25 -> EV@1.9 = 0.575*0.9 - 0.25 = 0.2675
    assert am.ev(p, 1.9) == pytest.approx(0.2675)
    q = am.ah_probs(m, 0.75, "away")
    assert q == pytest.approx({"win": 0.25, "half_win": 0, "push": 0, "half_loss": 0.35, "loss": 0.4})


def test_whole_and_half_lines_hand():
    m = mat({(2, 0): 0.3, (1, 0): 0.3, (1, 1): 0.2, (0, 2): 0.2})
    assert am.ah_probs(m, -1.0, "home") == pytest.approx(
        {"win": 0.3, "half_win": 0, "push": 0.3, "half_loss": 0, "loss": 0.4})
    assert am.ah_probs(m, -1.25, "home") == pytest.approx(
        {"win": 0.3, "half_win": 0, "push": 0, "half_loss": 0.3, "loss": 0.4})
    # 客 +1.75：0-2/1-1/1-0 全赢；2-0 净负 2 -> 一半 +1.5 输、一半 +2 走 = 输半
    assert am.ah_probs(m, 1.75, "away") == pytest.approx(
        {"win": 0.7, "half_win": 0, "push": 0, "half_loss": 0.3, "loss": 0.0})
    assert am.ah_probs(m, 0.0, "home") == pytest.approx(
        {"win": 0.6, "half_win": 0, "push": 0.2, "half_loss": 0, "loss": 0.2})
    # 平手：走盘退本金 -> EV@2.0 = 0.6 - 0.2
    assert am.ev(am.ah_probs(m, 0.0, "home"), 2.0) == pytest.approx(0.4)


def test_probs_sum_and_symmetry():
    m = am.poisson_matrix(1.6, 1.1, rho=-0.1)
    for ln in [x / 4 for x in range(-10, 11)]:
        p = am.ah_probs(m, ln, "home")
        assert sum(p.values()) == pytest.approx(1.0)
        q = am.ah_probs(m, -ln, "away")
        assert p["win"] == pytest.approx(q["loss"]) and p["half_win"] == pytest.approx(q["half_loss"])
        assert am.effective_prob(p) + am.effective_prob(q) == pytest.approx(1.0)


def test_fair_line():
    sym = am.poisson_matrix(1.3, 1.3)
    assert am.fair_line(sym).line == 0.0
    fl = am.fair_line(am.poisson_matrix(2.2, 0.8))
    assert fl.line < -0.75
    assert abs(1 / fl.home_fair_odds - 0.5) < 0.1
    assert 1 / fl.home_fair_odds + 1 / fl.away_fair_odds == pytest.approx(1.0)


def test_probs_1x2():
    m = mat({(1, 0): 0.5, (0, 0): 0.3, (0, 1): 0.2})
    assert am.probs_1x2(m) == pytest.approx((0.5, 0.3, 0.2))


def test_devig():
    assert am.devig_multiplicative([1.9, 2.0]) == pytest.approx([0.526316 / 1.026316, 0.5 / 1.026316], rel=1e-5)
    for f in (am.devig_multiplicative, am.devig_power, am.devig_shin):
        assert f([1.95, 1.95]) == pytest.approx([0.5, 0.5])
        p = f([1.5, 4.2, 7.0])
        assert p.sum() == pytest.approx(1.0)
    # 冷门偏差：power / Shin 给冷门的概率低于按比例法
    mult, pw, sh = (f([1.25, 6.5, 13.0]) for f in (am.devig_multiplicative, am.devig_power, am.devig_shin))
    assert pw[2] < mult[2] and sh[2] < mult[2]
    assert pw[0] > mult[0] and sh[0] > mult[0]


def test_market_gate_same_half_line():
    g = market_gate("home", -0.5, 2.05, -0.5, 1.95, 1.95)
    assert g.fair_odds == pytest.approx(2.0)
    assert g.ev == pytest.approx(0.025)  # 半球盘无走盘：EV = 2.05/2 - 1
    assert g.passes and not g.line_converted
    hk = market_gate("home", -0.5, 1.05, -0.5, 0.95, 0.95, odds_format="hk")
    assert hk.ev == pytest.approx(0.025)
    bad = market_gate("away", 0.5, 1.90, -0.5, 1.95, 1.95)
    assert bad.ev == pytest.approx(-0.05) and not bad.passes


def test_market_gate_same_quarter_line():
    g = market_gate("home", -0.25, 2.05, -0.25, 1.95, 1.95)
    assert g.fair_odds == pytest.approx(2.0)
    assert g.price_edge == pytest.approx(0.025)
    # 平半有输半：未退回本金份额 W+L < 1，EV 小于 price_edge 但同号
    w, l_ = am.win_lose_share(g.probs)
    assert w + l_ < 1
    assert g.ev == pytest.approx((w + l_) * 0.025)
    assert g.ev == pytest.approx(am.ev(g.probs, 2.05), abs=1e-6)  # 与矩阵直接计算一致
    assert market_gate("home", -0.25, 2.05, -0.25, 1.95, 1.95, min_ev=0.03).passes is False


def test_market_gate_converted_line():
    base = market_gate("home", -0.5, 2.0, -0.5, 1.95, 1.95)
    deeper = market_gate("home", -0.75, 2.0, -0.5, 1.95, 1.95)
    assert deeper.line_converted
    assert deeper.fair_odds > base.fair_odds  # 让更多 -> 公平赔率更高
    shallower = market_gate("away", 0.75, 2.0, -0.5, 1.95, 1.95)
    assert shallower.fair_odds < 2.0
    for meth in ("power", "shin"):
        assert market_gate("home", -0.5, 2.05, -0.5, 1.90, 2.00, method=meth).fair_odds > 1.9


def test_clv():
    assert clv(2.10, 2.0) == pytest.approx(0.05)
