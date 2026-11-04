"""把 reports/YYYY/MM/DATE.json 渲染为同名 .md，并校验 schema、追加到 predictions.csv。"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from . import DATA, REPORTS, SCHEMA, TEMPLATES
from .settle import LEDGER_FIELDS as CSV_FIELDS  # v2 表头（含 odds_format/status/final_* 等新列）


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
    from . import policy
    view = {**report, "picks": policy.published(report)}
    return env.get_template("report.md.j2").render(r=view)


def append_ledger(report: dict, path: Path = DATA / "predictions.csv") -> int:
    """把已发布推荐追加到 predictions.csv（按 v2 表头整表写回，旧 15 列文件会被补齐新列）。"""
    from .settle import new_ledger_row, norm_odds_format, read_ledger, write_ledger
    rows = read_ledger(path)
    seen = {(r["date"], r["match_id"]) for r in rows}
    try:
        fmt = norm_odds_format(report.get("odds_format"))
    except ValueError:
        fmt = "hk"
    n = 0
    from . import policy
    for p in policy.published(report):
        if (report["date"], p["match_id"]) in seen:
            continue
        rows.append(new_ledger_row(
            date=report["date"], match_id=p["match_id"], league=p["league"],
            home=p["home"], away=p["away"], kickoff_utc=p["kickoff_utc"],
            pick_side=p["pick_side"], pick=p.get("pick_team", p[p["pick_side"]]),
            line=p["line"], odds_if_known="" if p.get("odds") is None else p["odds"],
            confidence=p["confidence"], result="", ah_outcome="", units="", settled="false",
            odds_format=fmt, status="active"))
        n += 1
    if n or not path.exists():
        if rows:
            write_ledger(path, rows)
        elif not path.exists():
            with path.open("w", encoding="utf-8", newline="") as f:
                csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("date")
    a = ap.parse_args()
    jp, mp = report_paths(a.date)
    rep = json.loads(jp.read_text(encoding="utf-8"))
    from . import policy
    before = json.dumps(rep, ensure_ascii=False, sort_keys=True)
    policy.apply(rep)
    if json.dumps(rep, ensure_ascii=False, sort_keys=True) != before:
        jp.write_text(json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    validate(rep)
    mp.write_text(render_md(rep), encoding="utf-8")
    print(f"wrote {mp}; ledger +{append_ledger(rep)}")


if __name__ == "__main__":
    main()
