import pytest
from ah.settle import settle, profit, settle_row


@pytest.mark.parametrize("pg,og,line,exp", [
    # 整盘/半盘
    (1, 0, -0.5, "win"), (0, 0, -0.5, "loss"), (1, 1, 0, "push"), (2, 1, -1, "push"),
    (3, 1, -1, "win"), (0, 1, 0.5, "loss"), (1, 1, 0.5, "win"), (0, 1, 1, "push"),
    # 四分之一盘
    (1, 0, -0.75, "half_win"), (2, 0, -0.75, "win"), (0, 0, -0.75, "loss"),
    (1, 1, -0.25, "half_loss"), (1, 0, -0.25, "win"), (0, 0, 0.25, "half_win"),
    (0, 1, 0.25, "loss"), (0, 1, 0.75, "half_loss"), (2, 1, -1.25, "half_loss"),
    (3, 1, -1.25, "win"), (1, 1, -1.75, "loss"), (2, 0, -1.75, "half_win"),
    (3, 0, -1.75, "win"), (1, 2, 1.25, "half_win"), (1, 3, 1.75, "half_loss"),
])
def test_settle(pg, og, line, exp):
    assert settle(pg, og, line) == exp


def test_bad_line():
    with pytest.raises(ValueError):
        settle(1, 0, -0.3)


def test_profit():
    assert profit("win", 0.9) == 0.9
    assert profit("half_win", 0.9) == 0.45
    assert profit("push", 0.9) == 0
    assert profit("half_loss", 0.9) == -0.5
    assert profit("loss", 0.9) == -1
    assert profit("win", 1.95, odds_format="decimal") == 0.95
    assert profit("win", None) == 0.9


def test_settle_row_away_pick():
    r = {"result": "0-1", "pick_side": "away", "line": "-0.75", "odds_if_known": "0.78", "settled": "false"}
    settle_row(r)
    assert r["ah_outcome"] == "half_win" and r["settled"] == "true" and float(r["units"]) == 0.39


def test_settle_row_unsettled_left_alone():
    r = {"result": "", "pick_side": "home", "line": "-0.5", "settled": "false"}
    assert settle_row(dict(r)) == r
