import numpy as np
import pytest

from ah.model import ah_math as am
from ah.model import blend as bl
from ah.model.baseline import EloGoals


def test_blend_endpoints():
    a, b = am.poisson_matrix(1.8, 0.9), am.poisson_matrix(1.2, 1.2)
    assert np.allclose(bl.pool_matrices(a, b, 1.0), a / a.sum())
    assert np.allclose(bl.pool_matrices(a, b, 0.0), b / b.sum())
    assert bl.logit_blend(0.6, 0.4, 0.5) == pytest.approx(0.5)
    assert bl.logit_blend(0.7, 0.3, 1.0) == pytest.approx(0.7)
    p = bl.loglinear_1x2(np.array([0.5, 0.3, 0.2]), np.array([0.4, 0.3, 0.3]), 0.25)
    assert p.sum() == pytest.approx(1.0)


def test_edge_quarter_line():
    m = np.zeros((4, 4))
    m[2, 0], m[1, 0], m[0, 0] = 0.4, 0.35, 0.25
    assert bl.edge(m, "home", -0.75, 1.9) == pytest.approx(0.2675)


def test_elo_baseline():
    e = EloGoals()
    for _ in range(30):
        e.update("A", "B", 2, 0)
    assert e.rating("A") > 1500 > e.rating("B")
    assert e.rating("A") + e.rating("B") == pytest.approx(3000)
    e.refit_goal_map()
    mh, ma = e.means("A", "B")
    assert mh > ma
    assert e.matrix("A", "B").sum() == pytest.approx(1.0)


def test_implied_matrix_hits_prices():
    m = am.implied_matrix(-0.75, 0.52, 0.56)
    assert am.effective_prob(am.ah_probs(m, -0.75, "home")) == pytest.approx(0.52, abs=1e-6)
    under = sum(m[i, j] for i in range(3) for j in range(3 - i))
    assert 1 - under == pytest.approx(0.56, abs=1e-4)


def test_matches_penaltyblog_ah():
    pb = pytest.importorskip("penaltyblog")
    m = am.poisson_matrix(1.7, 1.0, max_goals=15, rho=-0.08)
    g = pb.models.FootballProbabilityGrid(m, 1.7, 1.0)
    for ln in [-1.75, -1.25, -1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.75, 1.0]:
        ref = g.asian_handicap_probs("home", ln)
        w, l_ = am.win_lose_share(am.ah_probs(m, ln, "home"))
        assert w == pytest.approx(ref["win"], abs=1e-9)
        assert l_ == pytest.approx(ref["lose"], abs=1e-9)
