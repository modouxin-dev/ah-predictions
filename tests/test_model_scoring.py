import pytest

from ah.model import ah_math as am
from ah.model.scoring import brier_settled, expected_score, settled_score, validate_probs


def test_settled_score_and_expected():
    assert [settled_score(o) for o in am.OUTCOMES] == [1.0, 0.75, 0.5, 0.25, 0.0]
    p = {"win": 0.4, "half_win": 0.2, "push": 0.0, "half_loss": 0.0, "loss": 0.4}
    assert expected_score(p) == pytest.approx(0.4 + 0.15)


def test_brier_settled_hand():
    # (0.55-1)^2=0.2025, (0.55-0.75)^2=0.04, (0.5-0.5)^2=0, (0.3-0)^2=0.09 -> mean 0.083125
    assert brier_settled([0.55, 0.55, 0.5, 0.3], ["win", "half_win", "push", "loss"]) == pytest.approx(0.083125)
    with pytest.raises(ValueError):
        brier_settled([0.5], [])


@pytest.mark.parametrize("line", [x / 4 for x in range(-10, 11)])
def test_matrix_probs_pass_validation(line):
    m = am.poisson_matrix(1.5, 1.1, rho=-0.05)
    for side in ("home", "away"):
        validate_probs(am.ah_probs(m, line, side), line)


def test_validation_rejects():
    ok = {"win": 0.5, "half_win": 0.0, "push": 0.2, "half_loss": 0.0, "loss": 0.3}
    validate_probs(ok, -1.0)
    with pytest.raises(ValueError):
        validate_probs(ok, -0.75)  # 四分之一盘不能有走
    with pytest.raises(ValueError):
        validate_probs({**ok, "half_win": 0.1, "win": 0.4}, -1.0)  # 整数盘不能有赢半
    with pytest.raises(ValueError):
        validate_probs({**ok, "loss": 0.31}, -1.0)  # 和不为 1
    with pytest.raises(ValueError):
        validate_probs(ok, -0.5)  # 半球盘不能有走
