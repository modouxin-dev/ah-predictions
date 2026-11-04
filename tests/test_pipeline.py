import json
from pathlib import Path

from ah.analyze import Candidate, line_movement_score, score, select
from ah.render_report import render_md, validate

ROOT = Path(__file__).resolve().parents[1]
CFG = {"max_picks_per_day": 5, "min_confidence_to_publish": "medium",
       "confidence_thresholds": {"high": 7.0, "medium": 4.5, "low": 3.0}}


def test_reports_valid_and_render():
    for f in ROOT.glob("reports/*/*/*.json"):
        r = json.loads(f.read_text(encoding="utf-8"))
        validate(r)
        md = render_md(r)
        assert r["date"] in md and "观察但不推荐" in md


def test_line_movement():
    assert line_movement_score(-0.5, -0.75) == 1
    assert line_movement_score(-0.25, -0.75) == 2
    assert line_movement_score(-1, -0.5) == -2
    assert line_movement_score(None, -0.5) == 0


def test_select_requires_real_line_and_threshold():
    base = dict(match_id="m", league="L", home="H", away="A", kickoff_utc="2026-11-04T12:00:00Z",
                pick_side="home", odds=0.9, sources=["https://x"])
    no_line = Candidate(line=None, features={"form": 2, "injuries": 2}, **base)
    weak = Candidate(line=-0.5, line_open=-0.5, features={"form": 1}, **base)
    strong = Candidate(line=-0.75, line_open=-0.5, features={"form": 2, "injuries": 2}, **base)
    assert score(strong) == 1.5 + 2.4 + 2.4
    picks = select([no_line, weak, strong], CFG)
    assert len(picks) == 1 and picks[0]["confidence"] == "medium"
