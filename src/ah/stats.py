"""命中率与 ROI 统计 -> data/stats.json"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

WIN_LIKE = {"win", "half_win"}
LOSS_LIKE = {"loss", "half_loss"}
OUTCOMES = {"win", "half_win", "push", "half_loss", "loss", "void"}
CONF_CN = {"high": "高", "medium": "中", "low": "低", "高": "高", "中": "中", "低": "低"}


def _bucket() -> dict:
    # cancelled: 赛前撤销场数（已计入 void，不计入命中率）；odds_estimated: 按 0.9 估算水位结算的场数
    return {"picks": 0, "win": 0, "half_win": 0, "push": 0, "half_loss": 0, "loss": 0, "void": 0,
            "hit_rate": 0.0, "units": 0.0, "roi": 0.0, "cancelled": 0, "odds_estimated": 0}


def _finish(b: dict) -> dict:
    decided = b["picks"] - b["push"] - b["void"]
    b["decided"] = decided
    b["hits"] = b["win"] + 0.5 * b["half_win"]
    b["hit_rate"] = round(b["hits"] / decided, 4) if decided else 0.0
    b["units"] = round(b["units"], 4)
    b["roi"] = round(b["units"] / decided, 4) if decided else 0.0
    return b


def depth_label(line: float) -> str:
    a = abs(float(line))
    return {0: "平手 0", 0.25: "平半 0.25", 0.5: "半球 0.5", 0.75: "半一 0.75", 1.0: "一球 1"}.get(a, "深盘 ≥1.25" if a >= 1.25 else f"{a:g}")


def role_label(line: float) -> str:
    x = float(line)
    return "让球方（上盘）" if x < 0 else "受让方（下盘）" if x > 0 else "平手盘"


def compute(rows: list[dict]) -> dict:
    from .settle import normalize_row, row_confidence
    overall = _bucket()
    est = {"picks": 0, "decided": 0, "units": 0.0, "match_ids": []}
    groups = {k: defaultdict(_bucket) for k in ("by_league", "by_confidence", "by_line", "by_depth", "by_role")}
    series = []
    for r in rows:
        r = normalize_row(dict(r))  # 兼容旧 15 列格式；不修改调用方数据
        if str(r.get("settled", "")).lower() != "true" or not r.get("ah_outcome"):
            continue
        out = r["ah_outcome"]
        if out not in OUTCOMES:
            continue
        units = float(r.get("units") or 0)
        cancelled = r["status"] == "cancelled"
        if r["status"] in ("cancelled", "void"):  # 撤销/作废一律按 void、0 单位，不计入命中率
            out, units = "void", 0.0
        estimated = r["odds_estimated"] == "true" and out != "void"
        keys = {"by_league": r.get("league", "?"), "by_confidence": CONF_CN.get(row_confidence(r), "取消/其他"),
                "by_line": f"{float(r['line']):+g}", "by_depth": depth_label(r["line"]), "by_role": role_label(r["line"])}
        for b in [overall] + [groups[g][k] for g, k in keys.items()]:
            b["picks"] += 1
            b[out] += 1
            b["units"] += units
            b["cancelled"] += cancelled
            b["odds_estimated"] += estimated
        if estimated:
            est["picks"] += 1
            est["decided"] += out != "push"
            est["units"] += units
            est["match_ids"].append(r.get("match_id", ""))
        if out != "void":
            series.append({"date": r.get("date", ""), "kickoff_utc": r.get("kickoff_utc", ""), "match_id": r.get("match_id", ""),
                           "match": f"{r.get('home', '')} vs {r.get('away', '')}", "league": r.get("league", ""),
                           "outcome": out, "units": round(units, 4), "odds_estimated": estimated})
    series.sort(key=lambda x: (x["date"], x["kickoff_utc"]))
    cum = 0.0
    for x in series:
        cum += x["units"]
        x["cum_units"] = round(cum, 4)
    return {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hit_rate_definition": "(win + 0.5*half_win)/(picks - push - void)",
        "roi_definition": "units/(picks - push - void)（每注1单位）",
        "cancelled_definition": "cancelled = 赛前撤销场数（已计入 void，不计入命中率/ROI）",
        "overall": _finish(overall),
        "odds_estimated": {**est, "units": round(est["units"], 4),
                           "note": f"无真实水位、按 0.9 港盘水位估算盈亏的已结算场次（{est['picks']} 场），战绩页需注明"},
        **{g: {k: _finish(v) for k, v in sorted(d.items())} for g, d in groups.items()},
        "series": series,
    }


def window(series: list[dict], end: str, days: int) -> dict:
    """截至 end(YYYY-MM-DD，含) 的近 days 天：{decided, hit_rate, units}（series 已排除取消）。"""
    from datetime import date, timedelta
    e = date.fromisoformat(end)
    start = (e - timedelta(days=days - 1)).isoformat()
    xs = [x for x in series if start <= x["date"] <= end]
    dec = [x for x in xs if x["outcome"] != "push"]
    hits = sum(1 if x["outcome"] == "win" else 0.5 if x["outcome"] == "half_win" else 0 for x in dec)
    return {"decided": len(dec), "hit_rate": round(hits / len(dec), 4) if dec else None,
            "units": round(sum(x["units"] for x in xs), 4), "picks": len(xs)}


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
    from .settle import read_ledger
    s = compute(read_ledger(Path(a.csv)))
    Path(a.out).write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(s["overall"], ensure_ascii=False))


if __name__ == "__main__":
    main()
