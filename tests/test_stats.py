from ah.stats import compute


def row(league, conf, line, out, units):
    return {"league": league, "confidence": conf, "line": line, "ah_outcome": out, "units": units, "settled": "true"}


def test_empty():
    s = compute([])
    assert s["overall"]["picks"] == 0 and s["overall"]["hit_rate"] == 0.0


def test_compute():
    rows = [row("UCL", "high", "-0.5", "win", "0.9"), row("UCL", "medium", "-0.75", "half_win", "0.45"),
            row("ACL", "medium", "-0.75", "loss", "-1"), row("ACL", "low", "0", "push", "0"),
            {"league": "X", "confidence": "low", "line": "0", "ah_outcome": "", "units": "", "settled": "false"}]
    s = compute(rows)
    o = s["overall"]
    assert o["picks"] == 4 and o["push"] == 1
    assert o["hit_rate"] == round(1.5 / 3, 4)
    assert o["units"] == 0.35 and o["roi"] == round(0.35 / 3, 4)
    assert s["by_league"]["UCL"]["hit_rate"] == 0.75
    assert s["by_confidence"]["中"]["picks"] == 2
    assert s["by_line"]["-0.75"]["units"] == -0.55


def test_void_excluded_and_summary_line():
    from ah.stats import summary_line
    s = compute([row("ACL", "cancelled", "-0.75", "void", "0"), row("ACL", "high", "-0.5", "win", "0.9")])
    o = s["overall"]
    assert o["picks"] == 2 and o["void"] == 1 and o["decided"] == 1 and o["hit_rate"] == 1.0
    assert s["by_confidence"]["高"]["decided"] == 1
    line = summary_line(s)
    assert "100.0%" in line and "1场" in line and "+0.90" in line and "\n" not in line
    assert "暂无" in summary_line(compute([]))
