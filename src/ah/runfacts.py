"""运行事实打点与报告新鲜度检查（facts only，旁路层，全标准库）。

只做三件事，**不改评分/阈值/盘口核验/展示/推送渲染，不发送 Telegram**：
1. `mark`：把各阶段 UTC 时刻、run_id、slot 追加到 data/run_facts.json，便于定位
   cron 延迟、采集耗时、推送耗时；只保留最近 N 个 run，避免仓库无限膨胀。
2. `check-freshness`：只读检查报告 generated_at / price_ts 与当前时刻的偏差。
   超阈值输出 `::warning::` 并写事实文件，**不阻断发送**（阻断会让补发漏发）。
3. `show`：打印最近若干条事实，便于人工核对。

缺字段一律写「未取得」，不推断原因、不补造盘口或时间。
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import DATA

MISSING = "未取得"
MAX_RUNS = 30                  # 只保留最近 30 个 run 的事实
DEFAULT_MAX_AGE_MIN = 180      # 报告生成超过 3 小时告警（只标注，不阻断）
DEFAULT_MAX_PRICE_AGE_MIN = 240

NOTE = (
    "运行事实只记录 UTC 时刻、run_id、slot、阶段名与报告已有字段；"
    "缺字段写『未取得』，不推断延迟原因、不补造盘口水位或比分；"
    "新鲜度超阈值只告警，不阻断发送（阻断会让补发漏发）。"
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str | None) -> datetime | None:
    if not ts or ts == MISSING:
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    except ValueError:
        return None


def _atomic_write(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix="." + p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def load(path: Path) -> dict:
    if not path.exists():
        return {"schema": 1, "runs": {}}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("runs"), dict):
            return d
    except (OSError, ValueError):
        pass
    return {"schema": 1, "runs": {}}


def mark(stage: str, run_id: str = "", slot: str = "", path: Path | None = None,
         now: datetime | None = None) -> dict:
    if not stage or not stage.strip():
        raise RuntimeError("stage 不能为空")
    p = Path(path) if path else DATA / "run_facts.json"
    d = load(p)
    rid = str(run_id or MISSING).strip() or MISSING
    runs = d.setdefault("runs", {})
    entry = runs.setdefault(rid, {"run_id": rid, "events": []})
    entry["events"].append({
        "stage": stage.strip(),
        "at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "slot": str(slot or MISSING).strip() or MISSING,
    })
    # 只保留最近 MAX_RUNS 个 run（按最后事件时间）
    order = sorted(runs, key=lambda k: runs[k]["events"][-1]["at"] if runs[k]["events"] else "")
    for k in order[:-MAX_RUNS]:
        runs.pop(k, None)
    d["schema"] = 1
    d["updated_at"] = _now()
    d["note"] = NOTE
    _atomic_write(p, d)
    return entry["events"][-1]


def freshness(report: dict, max_age_min: int = DEFAULT_MAX_AGE_MIN,
              max_price_age_min: int = DEFAULT_MAX_PRICE_AGE_MIN,
              now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    gen_raw = report.get("generated_at")
    gen = _parse(gen_raw)
    out = {
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "report_date": report.get("date") or MISSING,
        "generated_at": gen_raw or MISSING,
        "report_age_minutes": MISSING,
        "picks": len(report.get("picks") or []),
        "price_ts_checked": 0,
        "price_ts_missing": 0,
        "price_ts_stale": 0,
        "max_age_minutes": max_age_min,
        "max_price_age_minutes": max_price_age_min,
        "alerts": [],
        "status": "OK",
        "note": NOTE,
    }
    if gen is None:
        out["alerts"].append({"code": "GENERATED_AT_MISSING", "severity": "warning",
                              "message": "报告 generated_at 未取得，无法判断新鲜度；不阻断发送"})
    else:
        age = int((now - gen).total_seconds() // 60)
        out["report_age_minutes"] = age
        if age > max_age_min:
            out["alerts"].append({"code": "STALE_REPORT", "severity": "warning",
                                  "message": f"报告已生成 {age} 分钟（阈值 {max_age_min}）；可能是旧报告，不推断原因"})
        if age < -5:
            out["alerts"].append({"code": "FUTURE_TIMESTAMP", "severity": "warning",
                                  "message": "报告时间晚于当前时间超过 5 分钟；只记录事实，不校正"})
    # 盘口时间戳：只数「缺 / 超时」，不输出具体盘口值
    items = [*(report.get("picks") or []), *(report.get("watched") or []),
             *(report.get("watchlist") or [])]
    for it in items:
        o = it.get("observation") if isinstance(it.get("observation"), dict) else {}
        ts = it.get("price_ts") or o.get("price_ts")
        out["price_ts_checked"] += 1
        if not ts or ts == MISSING:
            out["price_ts_missing"] += 1
            continue
        d = _parse(ts)
        if d is None:
            out["price_ts_missing"] += 1
        elif int((now - d).total_seconds() // 60) > max_price_age_min:
            out["price_ts_stale"] += 1
    if out["price_ts_missing"]:
        out["alerts"].append({"code": "PRICE_TS_MISSING", "severity": "info",
                              "message": f"{out['price_ts_missing']} 个条目 price_ts 未取得；不补造、不推断"})
    if out["price_ts_stale"]:
        out["alerts"].append({"code": "PRICE_TS_STALE", "severity": "warning",
                              "message": f"{out['price_ts_stale']} 个条目盘口时间戳超过 {max_price_age_min} 分钟；只标注"})
    if any(a["severity"] == "warning" for a in out["alerts"]):
        out["status"] = "STALE_OR_MISSING"
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="运行事实打点与报告新鲜度检查（只读事实，不改模型、不发消息）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mark", help="记录一个阶段时间点到 data/run_facts.json")
    m.add_argument("--stage", required=True)
    m.add_argument("--run-id", default="")
    m.add_argument("--slot", default="")

    f = sub.add_parser("check-freshness", help="检查报告新鲜度，只告警不阻断")
    f.add_argument("--report", required=True)
    f.add_argument("--max-age-min", type=int, default=DEFAULT_MAX_AGE_MIN)
    f.add_argument("--max-price-age-min", type=int, default=DEFAULT_MAX_PRICE_AGE_MIN)
    f.add_argument("--out", default=None)

    s = sub.add_parser("show", help="打印最近运行事实")
    s.add_argument("--last", type=int, default=3)

    a = ap.parse_args(argv)
    if a.cmd == "mark":
        e = mark(a.stage, a.run_id, a.slot)
        print(f"RUNFACT stage={e['stage']} at={e['at']} slot={e['slot']}")
    elif a.cmd == "check-freshness":
        rp = Path(a.report)
        if not rp.exists():
            raise RuntimeError(f"报告不存在：{rp}；拒绝检查不存在的报告")
        report = json.loads(rp.read_text(encoding="utf-8"))
        out = freshness(report, a.max_age_min, a.max_price_age_min)
        dst = Path(a.out) if a.out else DATA / "report_freshness.json"
        _atomic_write(dst, {"schema": 1, **out})
        for x in out["alerts"]:
            if x["severity"] == "warning":
                print(f"::warning::{x['code']} {x['message']}")
        print(f"FRESHNESS status={out['status']} age_min={out['report_age_minutes']} "
              f"price_missing={out['price_ts_missing']} stale={out['price_ts_stale']} out={dst}")
    else:
        d = load(DATA / "run_facts.json")
        keys = sorted(d.get("runs", {}), key=lambda k: d["runs"][k]["events"][-1]["at"] if d["runs"][k]["events"] else "")[-a.last:]
        for k in keys:
            e = d["runs"][k]
            print(f"RUN {k} events={len(e['events'])}")
            for x in e["events"]:
                print(f"  {x['at']} slot={x['slot']} stage={x['stage']}")


if __name__ == "__main__":
    main()
