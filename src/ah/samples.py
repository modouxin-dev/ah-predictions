"""样本池与结算健康盘点（facts only，全标准库）。

不生成预测、不推断赛果、不改动模型算法/评分/阈值/核验：
- pool：把当日报告里**真实存在的全部候选**（picks / watched / watchlist）逐条落盘，
  供回测积累「开球前照片」；群消息与网页仍只显示正式推荐，互不影响。
- health：盘点 predictions.csv 里未结算条目，写事实清单；
  赛果字段为空就写「未取得」，绝不补造比分、不假称已补齐。

缺字段统一写「未取得」；列表字段为空数组表示报告未提供；布尔字段保留原值。
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from . import DATA, REPORTS

MISSING = "未取得"
ROLES = ("recommended", "observed", "not_recommended")

NOTE_POOL = (
    "开球前候选快照：所有字段逐字取自当日报告 JSON，未生成任何预测结论、盘口或赛果；"
    "缺字段标『未取得』；result 固定为『未取得』、settled=false，赛果由 settle 任务回填；"
    "sample_role 仅标记该场在报告里的归属，不代表模型判断。"
)
NOTE_HEALTH = (
    "结算事实盘点：仅读取 predictions.csv 现有字段；赛果字段为空即写『未取得』；"
    "不填比分、不推断原因、不假称已补齐；赛果由 settle 任务按已配置数据源回填。"
)


def _val(d: dict, key: str):
    v = d.get(key)
    if v is None:
        return MISSING
    if isinstance(v, str) and not v.strip():
        return MISSING
    return v


def _row(item: dict, role: str, report: dict) -> dict:
    o = item.get("observation") if isinstance(item.get("observation"), dict) else {}
    obs = None
    if o:
        obs = {k: _val(o, k) for k in (
            "score", "line", "line_open", "odds", "pick_team", "home_form", "away_form",
            "source", "bookmaker", "price_ts", "freshness", "form_source")}
    return {
        "date": _val(report, "date"),
        "captured_at": _val(report, "generated_at"),
        "match_id": _val(item, "match_id"),
        "league": _val(item, "league"),
        "home": _val(item, "home"),
        "away": _val(item, "away"),
        "kickoff_utc": _val(item, "kickoff_utc"),
        "sample_role": role,
        "confidence": _val(item, "confidence"),
        "score": _val(item, "score"),
        "pick_side": _val(item, "pick_side"),
        "pick_team": _val(item, "pick_team"),
        "line": _val(item, "line"),
        "line_open": _val(item, "line_open"),
        "odds": _val(item, "odds"),
        "market_consensus": item.get("market_consensus") or MISSING,
        "verification_advisory": bool(item.get("verification_advisory")),
        "withheld_reason": _val(item, "withheld_reason"),
        "withheld_category": _val(item, "withheld_category"),
        "sources": item.get("sources") or [],
        "reasons": item.get("reasons") or [],
        "risks": item.get("risks") or [],
        "observation": obs if obs is not None else MISSING,
        "result": MISSING,
        "settled": False,
        "note": "字段来自报告；不含预测结论与赛果；赛果待 settle 回填",
    }


def pool_rows(report: dict) -> list[dict]:
    """按 (match_id, home, away, kickoff_utc) 去重；picks 优先于 watched 优先于 watchlist。"""
    rows, seen = [], set()
    buckets = (("recommended", report.get("picks") or []),
               ("observed", report.get("watched") or []),
               ("not_recommended", report.get("watchlist") or []))
    for role, items in buckets:
        for it in items:
            if not isinstance(it, dict):
                continue
            key = (it.get("match_id"), it.get("home"), it.get("away"), it.get("kickoff_utc"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(_row(it, role, report))
    return rows


def write_pool(date: str, reports: Path | None = None, out_dir: Path | None = None) -> dict:
    if reports is None:
        y, m, _ = date.split("-")
        src = REPORTS / y / m / f"{date}.json"
    else:
        y, m, _ = date.split("-")
        src = Path(reports) / y / m / f"{date}.json"
    if not src.exists():
        raise SystemExit(f"当日报告不存在，拒绝生成样本池：{src}")
    report = json.loads(src.read_text(encoding="utf-8"))
    rows = pool_rows(report)
    counts = {r: sum(1 for x in rows if x["sample_role"] == r) for r in ROLES}
    payload = {
        "schema": 1,
        "kind": "pre-kickoff candidate pool",
        "date": date,
        "source_report": f"reports/{y}/{m}/{date}.json",
        "captured_at": _val(report, "generated_at"),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counts": {"total": len(rows), **counts},
        "rows": rows,
        "note": NOTE_POOL,
    }
    out = (Path(out_dir) if out_dir else DATA / "samples") / f"{date}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"SAMPLE_POOL date={date} total={len(rows)} "
          + " ".join(f"{k}={v}" for k, v in counts.items()) + f" out={out}")
    return payload


def health_payload(csv_path: Path, run_id: str, conclusion: str, event: str) -> dict:
    rows = []
    if csv_path.exists():
        with csv_path.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    settled = [r for r in rows if str(r.get("settled") or "").strip().lower() in ("true", "1", "yes")]
    unsettled = []
    for r in rows:
        if str(r.get("settled") or "").strip().lower() in ("true", "1", "yes"):
            continue
        unsettled.append({k: (r.get(k) or MISSING) for k in
                          ("date", "match_id", "home", "away", "kickoff_utc",
                           "status", "confidence", "track")} | {"result": MISSING})
    return {
        "schema": 1,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": run_id or MISSING,
        "job_conclusion": conclusion or MISSING,
        "event": event or MISSING,
        "source": "data/predictions.csv",
        "total_rows": len(rows),
        "settled_rows": len(settled),
        "unsettled_rows": len(unsettled),
        "unsettled": unsettled,
        "note": NOTE_HEALTH,
    }


def write_health(csv_path: Path | None = None, out: Path | None = None,
                 run_id: str = "", conclusion: str = "", event: str = "") -> dict:
    src = Path(csv_path) if csv_path else DATA / "predictions.csv"
    payload = health_payload(src, run_id, conclusion, event)
    dst = Path(out) if out else DATA / "settle_health.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"SETTLE_HEALTH total={payload['total_rows']} settled={payload['settled_rows']} "
          f"unsettled={payload['unsettled_rows']} conclusion={payload['job_conclusion']} out={dst}")
    return payload


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="样本池 / 结算健康盘点（只读事实，不生成预测）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("pool", help="把当日报告全部候选写入 data/samples/<date>.json")
    p1.add_argument("date")
    p1.add_argument("--reports", default=None)
    p1.add_argument("--out-dir", default=None)
    p2 = sub.add_parser("health", help="盘点 predictions.csv 未结算条目，写 data/settle_health.json")
    p2.add_argument("--csv", default=None)
    p2.add_argument("--out", default=None)
    p2.add_argument("--run-id", default="")
    p2.add_argument("--conclusion", default="")
    p2.add_argument("--event", default="")
    a = ap.parse_args(argv)
    if a.cmd == "pool":
        write_pool(a.date, a.reports, a.out_dir)
    else:
        write_health(a.csv, a.out, a.run_id, a.conclusion, a.event)


if __name__ == "__main__":
    main()
