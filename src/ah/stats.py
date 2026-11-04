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
OUTCOMES = {"win", "half_win", "push", "half_loss", "loss", "void"}
CONF_CN = {"high": "高", "medium": "中", "low": "低", "高": "高", "中": "中", "低": "低"}


def _bucket() -> dict:
    return {"picks": 0, "win": 0, "half_win": 0, "push": 0, "half_loss": 0, "loss": 0, "void": 0,
            "hit_rate": 0.0, "units": 0.0, "roi": 0.0}


def _finish(b: dict) -> dict:
    decided = b["picks"] - b["push"] - b["void"]
    b["decided"] = decided
    b["hits"] = b["win"] + 0.5 * b["half_win"]
    b["hit_rate"] = round(b["hits"] / decided, 4) if decided else 0.0
    b["units"] = round(b["units"], 4)
    b["roi"] = round(b["units"] / decided, 4) if decided else 0.0
    return b


def compute(rows: list[dict]) -> dict:
    overall = _bucket()
    groups = {k: defaultdict(_bucket) for k in ("by_league", "by_confidence", "by_line")}
    for r in rows:
        if str(r.get("settled", "")).lower() != "true" or not r.get("ah_outcome"):
            continue
        out = r["ah_outcome"]
        if out not in OUTCOMES:
            continue
        units = float(r.get("units") or 0)
        keys = {"by_league": r.get("league", "?"), "by_confidence": CONF_CN.get(r.get("confidence", ""), "取消/其他"),
                "by_line": f"{float(r['line']):+g}"}
        for b in [overall] + [groups[g][k] for g, k in keys.items()]:
            b["picks"] += 1
            b[out] += 1
            b["units"] += units
    return {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hit_rate_definition": "(win + 0.5*half_win)/(picks - push - void)",
        "roi_definition": "units/(picks - push - void)（每注1单位）",
        "overall": _finish(overall),
        **{g: {k: _finish(v) for k, v in sorted(d.items())} for g, d in groups.items()},
    }


def summary_line(s: dict) -> str:
    """Telegram 用一行战绩摘要。"""
    o = s.get("overall", {})
    if not o.get("decided"):
        return "📊 历史战绩：暂无已结算推荐"
    hi = s.get("by_confidence", {}).get("高")
    hi_txt = f"；高信心 {hi['hit_rate']:.1%}（{hi['decided']}场）" if hi and hi.get("decided") else ""
    return f"📊 历史命中率 {o['hit_rate']:.1%}（{o['decided']}场，{o['units']:+.2f}单位）{hi_txt}"


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
