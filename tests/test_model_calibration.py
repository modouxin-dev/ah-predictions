import numpy as np
import pytest

from ah.model import ah_math as am
from ah.model import calibration as cal
from ah.model.scoring import expected_score, validate_probs


def test_temper_multiclass_t1_identity():
    p = np.array([0.5, 0.3, 0.2])
    assert cal.temper_multiclass(p, 1.0) == pytest.approx(p)


def test_temper_binary_overconfident():
    # T>1 把 0.8 压向 0.5
    assert cal.temper_binary(0.8, 2.0) < 0.8
    assert cal.temper_binary(0.8, 2.0) > 0.5
    assert cal.temper_binary(0.2, 2.0) > 0.2
    assert cal.temper_binary(0.2, 2.0) < 0.5


def test_fit_t_binary_recovers_overconfidence():
    rng = np.random.default_rng(0)
    # 真实频率 ~0.55，但模型报 0.75 → 应学到 T>1
    q = np.full(400, 0.75)
    y = rng.binomial(1, 0.55, size=400).astype(float)
    T = cal.fit_t_binary(q, y)
    assert T > 1.2


def test_fit_t_multiclass_t_near_one_when_calibrated():
    rng = np.random.default_rng(1)
    P = np.tile([0.5, 0.3, 0.2], (500, 1))
    y = rng.choice(3, size=500, p=[0.5, 0.3, 0.2])
    T = cal.fit_t_multiclass(P, y)
    assert 0.7 < T < 1.4


def test_recalibrate_ah_preserves_line_constraints():
    m = am.poisson_matrix(1.6, 1.1, rho=-0.05)
    for line in (-1.0, -0.75, -0.5, 0.0, 0.25, 0.5):
        probs = am.ah_probs(m, line, "home")
        for T in (0.7, 1.0, 1.5, 2.0):
            out = cal.recalibrate_ah(probs, T)
            validate_probs(out, line)
            assert sum(out.values()) == pytest.approx(1.0, abs=1e-9)


def test_recalibrate_ah_to_q_hits_target():
    m = am.poisson_matrix(1.8, 1.0)
    probs = am.ah_probs(m, -0.75, "home")
    out = cal.recalibrate_ah_to_q(probs, 0.55)
    assert cal.ahead_prob(out) == pytest.approx(0.55, abs=1e-6)
    assert out["push"] == pytest.approx(probs["push"])


def test_recalibrate_changes_expected_score_toward_half():
    m = am.poisson_matrix(2.2, 0.8)
    probs = am.ah_probs(m, -1.0, "home")
    raw = expected_score(probs)
    soft = expected_score(cal.recalibrate_ah(probs, 2.0))
    # 过度自信压向 0.5 时期望结算分更靠近 0.5
    assert abs(soft - 0.5) < abs(raw - 0.5)


def test_pav_monotonic_and_predict():
    x = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8])
    y = np.array([0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0])
    model = cal.pav_fit(x, y)
    pred = cal.pav_predict(model, x)
    assert np.all(np.diff(pred) >= -1e-12)
    assert pred.min() >= 0.01 and pred.max() <= 0.99


def test_temperature_calibrator_roundtrip():
    c = cal.TemperatureCalibrator(t_1x2=1.3, t_ah=1.5)
    d = c.to_dict()
    assert d == {"t_1x2": 1.3, "t_ah": 1.5, "t_ou": 1.0}
    p = c.p1x2([0.6, 0.25, 0.15])
    assert p.sum() == pytest.approx(1.0)
