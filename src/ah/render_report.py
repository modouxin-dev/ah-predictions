"""把 reports/YYYY/MM/DATE.json 渲染为同名 .md，并校验 schema、追加到 predictions.csv。"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from . import DATA, REPORTS, SCHEMA, TEMPLATES

CSV_FIELDS = ["date", "match_id", "league", "home", "away", "kickoff_utc", "pick_side", "pick",
              "line", "odds_if_known", "confidence", "result", "ah_outcome", "units", "settled"]


def report_paths(date: str) -> tuple[Path, Path]:
    y, m, _ = date.split("-")
    d = REPORTS / y / m
    return d / f"{date}.json", d / f"{date}.md"


def validate(report: dict) -> None:
    try:
        import jsonschema
    except ImportError:  # pragma: no cover
        return
    jsonschema.validate(report, json.loads((SCHEMA / "report.schema.json").read_text(encoding="utf-8")))


def render_md(report: dict) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), trim_blocks=True, lstrip_blocks=True)
    return env.get_template("report.md.j2").render(r=report)


def append_ledger(report: dict, path: Path = DATA / "predictions.csv") -> int:
    exists = path.exists() and path.stat().st_size > 0
    seen = set()
    if exists:
        seen = {(r["date"], r["match_id"]) for r in csv.DictReader(path.open(encoding="utf-8"))}
    n = 0
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not exists:
            w.writeheader()
        for p in report["picks"]:
            if (report["date"], p["match_id"]) in seen:
                continue
            w.writerow({
                "date": report["date"], "match_id": p["match_id"], "league": p["league"],
                "home": p["home"], "away": p["away"], "kickoff_utc": p["kickoff_utc"],
                "pick_side": p["pick_side"], "pick": p.get("pick_team", p[p["pick_side"]]),
                "line": p["line"], "odds_if_known": "" if p.get("odds") is None else p["odds"],
                "confidence": p["confidence"], "result": "", "ah_outcome": "", "units": "", "settled": "false",
            })
            n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("date")
    a = ap.parse_args()
    jp, mp = report_paths(a.date)
    rep = json.loads(jp.read_text(encoding="utf-8"))
    validate(rep)
    mp.write_text(render_md(rep), encoding="utf-8")
    print(f"wrote {mp}; ledger +{append_ledger(rep)}")


if __name__ == "__main__":
    main()
