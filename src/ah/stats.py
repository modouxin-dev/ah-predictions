"""命中率与 ROI 统计 -> data/stats.json"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

WIN_LIKE = {"win", "half_win"}
LOSS_LIKE = {"loss", "half_loss"}


def _bucket() -> dict:
    return {"picks": 0, "win": 0, "half_win": 0, "push": 0, "half_loss": 0, "loss": 0,
            "hit_rate": 0.0, "units": 0.0, "roi": 0.0}


def _finish(b: dict) -> dict:
    decided = b["picks"] - b["push"]
    wins = b["win"] + b["half_win"]
    b["hit_rate"] = round(wins / decided, 4) if decided else 0.0
    b["units"] = round(b["units"], 4)
    b["roi"] = round(b["units"] / b["picks"], 4) if b["picks"] else 0.0
    return b


def compute(rows: list[dict]) -> dict:
    overall = _bucket()
    groups = {k: defaultdict(_bucket) for k in ("by_league", "by_confidence", "by_line")}
    for r in rows:
        if str(r.get("settled", "")).lower() != "true" or not r.get("ah_outcome"):
            continue
        out = r["ah_outcome"]
        units = float(r.get("units") or 0)
        keys = {"by_league": r.get("league", "?"), "by_confidence": r.get("confidence", "?"),
                "by_line": f"{float(r['line']):+g}"}
        for b in [overall] + [groups[g][k] for g, k in keys.items()]:
            b["picks"] += 1
            b[out] += 1
            b["units"] += units
    return {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hit_rate_definition": "(win+half_win)/(picks-push)",
        "roi_definition": "units/picks（每注1单位）",
        "overall": _finish(overall),
        **{g: {k: _finish(v) for k, v in sorted(d.items())} for g, d in groups.items()},
    }


def main() -> None:
    from . import DATA
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(DATA / "predictions.csv"))
    ap.add_argument("--out", default=str(DATA / "stats.json"))
    a = ap.parse_args()
    rows = list(csv.DictReader(Path(a.csv).open(encoding="utf-8")))
    s = compute(rows)
    Path(a.out).write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(s["overall"], ensure_ascii=False))


if __name__ == "__main__":
    main()
