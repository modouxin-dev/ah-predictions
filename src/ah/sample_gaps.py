"""样本缺口告警 + 结算回填归因（facts only，不额外调用任何接口）。

- gap：统计样本池天数/候选数/已结算数/分档数，给出距校准门槛的缺口；
  按最近窗口日均新增推算还需天数，明确标注「推算，非承诺」。
- health：在 settle_health 基础上补「距开球分钟数」与「是否已满足 110 分钟回填条件」，
  只对事实做分类，不推断数据源为何没返回。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import DATA
from .backtest import MIN_PER_TIER, MIN_TOTAL, SAMPLES_DIR, LEDGER, load_ledger, load_pools, join, MISSING, _truthy
from .samples import health_payload

OUT_GAPS = DATA / "sample_gaps.json"
AUTOFILL_MINUTES = 110


def gap_payload(pools=None, ledger=None, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    rows = pools if pools is not None else load_pools()
    led = ledger if ledger is not None else load_ledger()
    joined = join(rows, led)
    dates = sorted({r["pool_date"] for r in rows})
    settled = [r for r in joined if r["settled"] and r["result"] != MISSING]
    by_tier = defaultdict(int)
    for r in settled:
        by_tier[str(r["confidence"] or MISSING)] += 1
    tiers = {t: {"settled": n, "needed": max(0, MIN_PER_TIER - n),
                 "eligible": n >= MIN_PER_TIER} for t, n in sorted(by_tier.items())}
    # 日均新增：按最近 7 个自然日窗口（事实窗口，不是预测）。
    # 达标口径是「已结算样本」，故推算必须用已结算速率；用候选速率会严重乐观。
    recent = [r for r in rows if r["pool_date"] in dates[-7:]]
    days = max(1, len({r["pool_date"] for r in recent}))
    daily_new_rows = round(len(recent) / days, 2)
    recent_settled = sum(1 for r in recent if r["settled"] and r["result"] != MISSING)
    settled_rate = round(recent_settled / days, 2)
    ledger_settled = sum(1 for v in led.values() if _truthy(v.get("settled")))
    total_needed = max(0, MIN_TOTAL - ledger_settled)
    eta = round(total_needed / settled_rate, 1) if settled_rate > 0 else None
    return {
        "schema": 1,
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pool_days": len(dates), "pool_rows": len(rows),
        "settled_rows": len(settled), "unsettled_rows": len(joined) - len(settled),
        "ledger_settled_rows": ledger_settled,
        "by_tier": tiers,
        "thresholds": {"min_per_tier": MIN_PER_TIER, "min_total": MIN_TOTAL},
        "recent_window_days": days, "recent_rows": len(recent),
        "observed_daily_new_rows": daily_new_rows,
        "observed_daily_settled_rows": settled_rate,
        "estimated_days_to_total_threshold": eta,
        "estimate_note": ("近期无已结算样本，无法推算达标天数（不编造估算）" if eta is None else
                          "按最近窗口实际日均【已结算】条数推算，仅作参考；赛程密度与回填成功率都会变化，不是承诺"),
        "alert": ("SAMPLE_INSUFFICIENT 样本未达校准门槛，暂不输出命中率/校准结论"
                  if len(settled) < MIN_TOTAL or not all(v["eligible"] for v in tiers.values()) or len(tiers) < 3
                  else "SAMPLE_THRESHOLD_REACHED 可进入时间外回测评估"),
        "note": "只统计真实落盘字段；未取得赛果一律不计入已结算，不补造比分。",
    }


def write_gap(out: Path | None = None) -> dict:
    d = gap_payload()
    dst = Path(out) if out else OUT_GAPS
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"SAMPLE_GAPS days={d['pool_days']} rows={d['pool_rows']} settled={d['settled_rows']} "
          f"ledger_settled={d['ledger_settled_rows']} "
          f"alert={d['alert'].split()[0]} out={dst}")
    return d


def enriched_health(csv_path: Path | None = None, out: Path | None = None,
                    run_id: str = "", conclusion: str = "", event: str = "",
                    now: datetime | None = None) -> dict:
    """settle_health + 事实归因：距开球多久、是否已满足自动回填时间条件。"""
    now = now or datetime.now(timezone.utc)
    src = Path(csv_path) if csv_path else LEDGER
    h = health_payload(src, run_id, conclusion, event)
    eligible = 0
    for x in h["unsettled"]:
        ko = x.get("kickoff_utc") or MISSING
        minutes = MISSING
        ok = False
        not_started = False
        if ko != MISSING:
            try:
                from .match import parse_dt
                k = parse_dt(ko)
                minutes = int((now - k).total_seconds() // 60)
                not_started = minutes < 0
                ok = (not not_started) and minutes >= AUTOFILL_MINUTES
            except (TypeError, ValueError):
                minutes = MISSING
        x["minutes_since_kickoff"] = minutes
        x["autofill_time_eligible"] = ok
        if minutes == MISSING:
            x["attribution"] = "开球时间无法解析，按规则暂不回填"
        elif not_started:
            x["attribution"] = "尚未开赛，按规则不回填"
        elif ok:
            x["attribution"] = "已满足 110 分钟回填条件，仍未取得赛果；原因见 settle 运行日志，本文件不推断"
        else:
            x["attribution"] = "已开赛但距开球不足 110 分钟，按规则暂不回填"
        if ok:
            eligible += 1
    h["autofill_eligible_unsettled"] = eligible
    h["autofill_minutes_rule"] = AUTOFILL_MINUTES
    h["alert"] = ("SETTLE_BACKLOG 已有 %d 场满足回填时间条件但仍未取得赛果；不假称已补齐" % eligible
                  if eligible else "NO_TIME_ELIGIBLE_BACKLOG 当前无超过回填时限的未结算条目")
    h["note"] = (h["note"] + " 归因仅按开球时间做事实分类，不推断数据源失败原因、不补造比分。")
    dst = Path(out) if out else DATA / "settle_health.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(h, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"SETTLE_HEALTH total={h['total_rows']} settled={h['settled_rows']} "
          f"unsettled={h['unsettled_rows']} eligible_backlog={eligible} out={dst}")
    return h


def cli_main(argv: list[str] | None = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="样本缺口告警 / 结算回填归因")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gap")
    p2 = sub.add_parser("health")
    p2.add_argument("--csv", default=None)
    p2.add_argument("--out", default=None)
    p2.add_argument("--run-id", default="")
    p2.add_argument("--conclusion", default="")
    p2.add_argument("--event", default="")
    a = ap.parse_args(argv)
    if a.cmd == "gap":
        write_gap()
    else:
        enriched_health(a.csv, a.out, a.run_id, a.conclusion, a.event)
