"""时间外回测流水线（facts only）。

输入：
- data/samples/<date>.json：开球前候选快照（只含报告真实字段）
- data/predictions.csv：结算账本（赛果、亚盘结果、盈亏）
输出：
- data/backtest_report.json / .md

硬约束（研究报告第四节 + AGENTS.md）：
- 只有账本里真实存在的 result 才参与结算，绝不补造赛果或推断比分；
- 按日期做时间外切分，不随机打散；测试集只用于最终评估；
- 每档样本量 < MIN_PER_TIER 或总结算样本 < MIN_TOTAL 时，整体标 INSUFFICIENT_SAMPLE，
  只输出样本统计，**不输出任何准确率/校准/ROI 结论**；
- 没有校准概率时不算 Brier score / 可靠性曲线，标 NOT_COMPUTED；
- 市场基准只用单方水位换算的隐含概率，明确标注「未去水，仅参考」。
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import DATA

MIN_PER_TIER = 80
MIN_TOTAL = 300
SAMPLES_DIR = DATA / "samples"
LEDGER = DATA / "predictions.csv"
OUT_JSON = DATA / "backtest_report.json"
OUT_MD = DATA / "backtest_report.md"
MISSING = "未取得"
HIT_OUTCOMES = ("win", "half_win")


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("true", "1", "yes")


def load_pools() -> list[dict]:
    rows = []
    for f in sorted(SAMPLES_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for r in d.get("rows") or []:
            rows.append({"pool_date": d.get("date") or f.stem, **r})
    return rows


def load_ledger() -> dict:
    out = {}
    if not LEDGER.exists():
        return out
    with LEDGER.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            out[(r.get("date") or "", r.get("match_id") or "")] = r
    return out


def join(pools: list[dict], ledger: dict) -> list[dict]:
    out = []
    for p in pools:
        key = (p.get("date") or p.get("pool_date") or "", p.get("match_id") or "")
        row = ledger.get(key)
        item = {**p, "ledger_found": row is not None,
                "result": (row or {}).get("result") or MISSING,
                "ah_outcome": (row or {}).get("ah_outcome") or MISSING,
                "units": (row or {}).get("units") or MISSING,
                "settled": _truthy((row or {}).get("settled")) if row else False,
                "odds": (row or {}).get("odds_if_known") or p.get("odds") or MISSING,
                "confidence": (row or {}).get("confidence") or p.get("confidence") or MISSING}
        out.append(item)
    return out


def time_split(rows: list[dict]) -> dict:
    dates = sorted({str(r.get("date") or r.get("pool_date") or "") for r in rows if r.get("date") or r.get("pool_date")})
    n = len(dates)
    if n < 3:
        return {"dates": dates, "train": dates, "valid": [], "test": dates[-1:],
                "note": "日期不足 3 天，无法做三段切分；仅保留最后一天作测试集占位"}
    i, j = max(1, int(n * 0.6)), max(2, int(n * 0.8))
    return {"dates": dates, "train": dates[:i], "valid": dates[i:j], "test": dates[j:],
            "note": "按日期时间外切分（train 60% / valid 20% / test 20%），不随机打散；测试集仅用于最终评估"}


def market_implied(odds) -> float | None:
    """港盘水位 -> decimal -> 单方隐含概率。未去水，仅作参考基准。"""
    try:
        hk = float(odds)
    except (TypeError, ValueError):
        return None
    dec = 1.0 + hk
    return round(1.0 / dec, 4) if dec > 0 else None


def evaluate(rows: list[dict]) -> dict:
    decided = [r for r in rows if r["settled"] and r["result"] != MISSING]
    by_tier = defaultdict(lambda: {"n": 0, "hits": 0, "units_sum": 0.0})
    for r in decided:
        t = str(r["confidence"] or MISSING)
        b = by_tier[t]
        b["n"] += 1
        b["hits"] += 1 if str(r["ah_outcome"]) in HIT_OUTCOMES else 0
        try:
            b["units_sum"] += float(r["units"])
        except (TypeError, ValueError):
            pass
    per = {t: {"n": v["n"], "hits": v["hits"],
               "actual_hit_frequency": round(v["hits"] / v["n"], 4) if v["n"] else None,
               "units_sum": round(v["units_sum"], 3)} for t, v in sorted(by_tier.items())}
    enough = len(decided) >= MIN_TOTAL and all(v["n"] >= MIN_PER_TIER for v in by_tier.values()) and len(per) >= 3
    out = {
        "status": "EVALUATED" if enough else "INSUFFICIENT_SAMPLE",
        "thresholds": {"min_per_tier": MIN_PER_TIER, "min_total": MIN_TOTAL},
        "settled_rows": len(decided), "tiers": per,
        "brier_score": "NOT_COMPUTED",
        "reliability_curve": "NOT_COMPUTED",
        "note": ("无校准概率，Brier / 可靠性曲线不计算；不得把规则档位当作预测概率。"
                 if enough else
                 "样本未达门槛：只输出样本统计，不输出命中率结论、不输出校准结论、不宣称模型更准。"),
    }
    if enough:
        implied = [market_implied(r["odds"]) for r in decided]
        implied = [x for x in implied if x is not None]
        out["market_baseline"] = {
            "n": len(implied),
            "mean_single_side_implied_probability": round(sum(implied) / len(implied), 4) if implied else None,
            "note": "由港盘单方水位 1/(1+水位) 换算，未去水、无对手水位，仅作参考基准，不是市场真实概率",
        }
        out["overall"] = {
            "hits": sum(v["hits"] for v in by_tier.values()),
            "n": len(decided),
            "actual_hit_frequency": round(sum(v["hits"] for v in by_tier.values()) / len(decided), 4),
            "units_sum": round(sum(v["units_sum"] for v in by_tier.values()), 3),
            "note": "按发布时刻真实盘口账本结算；命中与盈亏并列，命中率高不代表有价值",
        }
    return out


def build() -> dict:
    pools = load_pools()
    ledger = load_ledger()
    rows = join(pools, ledger)
    split = time_split(rows)
    ev = evaluate(rows)
    return {
        "schema": 1,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pool_days": len({r["pool_date"] for r in rows}),
        "pool_rows": len(rows),
        "ledger_rows": len(ledger),
        "matched_rows": sum(1 for r in rows if r["ledger_found"]),
        "settled_rows": sum(1 for r in rows if r["settled"] and r["result"] != MISSING),
        "ledger_settled_rows": sum(1 for v in ledger.values() if _truthy(v.get("settled"))),
        "time_split": split,
        "evaluation": ev,
        "note": "赛果只来自 data/predictions.csv；未取得一律不推断；样本不足时不输出任何准确率结论。",
    }


def markdown(d: dict) -> str:
    ev = d["evaluation"]
    lines = [
        "# 时间外回测报告（facts only）",
        "",
        f"- 检查时间：{d['checked_at']}",
        f"- 样本池天数 {d['pool_days']}，候选 {d['pool_rows']} 条，账本 {d['ledger_rows']} 条，可对齐 {d['matched_rows']} 条，已结算 {d['settled_rows']} 条",
        f"- 时间外切分：{d['time_split']['note']}",
        f"- **状态：{ev['status']}**",
        "",
        "## 分档样本统计",
        "",
        "| 档位 | 已结算 | 命中 | 实际频率 | 盈亏(u) |",
        "|---|---:|---:|---:|---:|",
    ]
    for t, v in (ev["tiers"] or {}).items():
        freq = "未输出（样本不足）" if ev["status"] != "EVALUATED" else str(v["actual_hit_frequency"])
        lines.append(f"| {t} | {v['n']} | {v['hits']} | {freq} | {v['units_sum']} |")
    if not ev["tiers"]:
        lines.append("| 无 | 0 | 0 | — | 0 |")
    lines += ["", "## 明确未计算", "",
              f"- Brier score：{ev['brier_score']}（无校准概率）",
              f"- 可靠性曲线：{ev['reliability_curve']}（无校准概率）",
              f"- 说明：{ev['note']}", "",
              "> 赛果只来自账本；未取得不推断、不补造。样本不足时本文件不构成任何准确率或模型改进结论。",
              ""]
    return "\n".join(lines)


def main() -> None:
    d = build()
    OUT_JSON.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    OUT_MD.write_text(markdown(d), encoding="utf-8")
    print(f"BACKTEST status={d['evaluation']['status']} pool_rows={d['pool_rows']} "
          f"settled={d['settled_rows']} out={OUT_JSON}")
    if d["evaluation"]["status"] == "INSUFFICIENT_SAMPLE":
        print("INSUFFICIENT_SAMPLE 样本未达门槛：不输出命中率/校准结论（不是故障）")


if __name__ == "__main__":
    main()
