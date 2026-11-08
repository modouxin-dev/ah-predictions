"""数据质量事实盘点（facts only，纯旁路，只读，不改模型/台账/报告/展示）。

把深度检查里 B1/B6/C1/C2/C4/B3 的缺口变成每次运行可核对的数字：
  1. clv_field_coverage：predictions.csv 的 open/close/时间戳字段覆盖，以及
     snapshots/odds 中能按 match_id join 回预测行的场数（数据采了没回填的缺口）。
  2. candidate_coverage：报告 not_covered 写明的「上限 / 窗口 / 未覆盖」事实。
  3. source_concentration：picks 的 source / bookmaker 分布（单源依赖可见性）。
  4. factor_gaps：feature_evidence 里 value 为空或明确「未取得」的因子计数。
  5. zero_pick_reasons：0 场时列出 collection_notes / collection_errors 的事实条目。
  6. sample_precision：已结算场数与 Wilson 区间宽度（只作精度说明，不构成优势结论）。

硬约束：不改任何 provider / 评分 / 阈值 / 核验口径 / 展示 / 推送；不写回 CSV；
缺字段写「未取得」；不推断原因；不补造赛程、盘口、水位、比分、推荐或命中率。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import DATA, ROOT

MISSING = "未取得"

CLV_FIELDS = ("line", "line_open", "price_ts", "final_line", "final_odds",
              "close_line", "close_odds", "odds_if_known")
DECIDED_OUTCOMES = {"win", "half_win", "half_loss", "loss"}

NOTE = (
    "只读盘点：不写回 predictions.csv / 报告 / 台账，不改任何模型或核验口径；"
    "所有数字来自文件解析，缺字段写『未取得』，不推断原因、不补造任何赛程盘口水位比分；"
    "样本精度只说明区间宽度，不构成命中率或优势结论（是否达门槛由 review.py 判定）。"
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _load_json(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_csv(p: Path) -> tuple[list[str], list[dict]]:
    if not p.exists():
        return [], []
    try:
        with p.open(encoding="utf-8", newline="") as f:
            rd = csv.DictReader(f)
            return list(rd.fieldnames or []), list(rd)
    except (OSError, UnicodeError, csv.Error):
        return [], []


def report_path(date: str, root: Path | None = None) -> Path:
    base = Path(root) if root else ROOT
    parts = date.split("-")
    if len(parts) != 3:
        raise RuntimeError(f"报告日期格式无效：{date!r}（应为 YYYY-MM-DD）")
    return base / "reports" / parts[0] / parts[1] / f"{date}.json"


def candidate_coverage(report: dict) -> dict:
    raw = report.get("not_covered")
    out = {"raw": raw if isinstance(raw, str) else MISSING,
           "max_fixtures": MISSING, "window_total": MISSING,
           "not_covered_count": MISSING, "note": "只解析报告已写明的数字，不推断漏采原因"}
    if isinstance(raw, str):
        m = re.search(r"上限\s*(\d+)\s*场.*?共发现\s*(\d+)\s*场", raw, re.S)
        if m:
            cap, total = int(m.group(1)), int(m.group(2))
            out["max_fixtures"] = cap
            out["window_total"] = total
            out["not_covered_count"] = max(0, total - cap)
    return out


def verification_facts(report: dict) -> dict:
    """只解析报告 not_covered 里已写明的「盘口核验」数字，不推断含义、不补造结论。

    例：'盘口核验：no_quotes 1场；no_shared_book 3场；sources:1<2 2场；
         stale>7200s 20场；verified:0<1 6场。' -> 逐项 {标签: 场数}
    """
    raw = report.get("not_covered")
    out = {"raw": MISSING, "items": {}, "note": "只抄录报告已写明的项目与场数，不解释成因、不推断数据充足"}
    if not isinstance(raw, str) or "盘口核验" not in raw:
        return out
    seg = raw.split("盘口核验", 1)[1].split("。", 1)[0]
    out["raw"] = seg.strip().lstrip("：: ").strip()
    for label, count in re.findall(r"([A-Za-z][A-Za-z0-9_<>:]*)\s*(\d+)\s*场", seg):
        out["items"][label.rstrip(":：")] = int(count)
    return out


def clv_field_coverage(ledger: Path, snapshots_dir: Path) -> dict:
    cols, rows = _read_csv(ledger)
    out = {
        "ledger": str(ledger),
        "ledger_exists": ledger.exists(),
        "rows": len(rows),
        "columns_present": [c for c in CLV_FIELDS if c in cols],
        "columns_absent": [c for c in CLV_FIELDS if c not in cols],
        "field_filled": {},
        "snapshots_dir": str(snapshots_dir),
        "snapshot_files": 0,
        "snapshot_match_ids": MISSING,
        "joinable_rows": MISSING,
        "odds_estimated_true": MISSING,
        "note": "joinable 仅指 match_id 同时出现在预测行与任一快照 csv；不代表已回填 CLV",
    }
    if not rows:
        return out
    for c in CLV_FIELDS:
        out["field_filled"][c] = sum(1 for r in rows if str(r.get(c) or "").strip()) if c in cols else 0
    if "odds_estimated" in cols:
        out["odds_estimated_true"] = sum(
            1 for r in rows if str(r.get("odds_estimated") or "").strip().lower() == "true")
    snaps = sorted(snapshots_dir.glob("*.csv")) if snapshots_dir.is_dir() else []
    out["snapshot_files"] = len(snaps)
    if snaps and "match_id" in cols:
        ids: set[str] = set()
        cols_seen: set[str] = set()
        for sp in snaps:
            sc, sr = _read_csv(sp)
            cols_seen |= set(sc)
            key = next((k for k in ("match_id", "fixture_id", "id") if k in sc), None)
            if key:
                ids |= {str(r.get(key) or "").strip() for r in sr if str(r.get(key) or "").strip()}
        if ids:
            out["snapshot_match_ids"] = len(ids)
            out["joinable_rows"] = sum(
                1 for r in rows if str(r.get("match_id") or "").strip() in ids)
    return out


def source_concentration(report: dict) -> dict:
    picks = report.get("picks") or []
    src, book = Counter(), Counter()
    for p in picks:
        o = p.get("observation") if isinstance(p.get("observation"), dict) else {}
        src[str(p.get("source") or o.get("source") or MISSING)] += 1
        book[str(p.get("bookmaker") or o.get("bookmaker") or MISSING)] += 1
    return {"picks": len(picks), "by_source": dict(src), "by_bookmaker": dict(book),
            "distinct_sources": len([k for k in src if k != MISSING]),
            "note": "只统计报告已有字段；单一来源不代表数据不足，也不推断覆盖率"}


def factor_gaps(report: dict) -> dict:
    fe = report.get("feature_evidence")
    if not isinstance(fe, dict):
        return {"status": "FEATURE_EVIDENCE_MISSING", "note": "报告无因子证据；不补造"}
    by = fe.get("by_match")
    if not isinstance(by, dict):
        return {"status": "BY_MATCH_MISSING", "note": "因子证据无 by_match；不补造"}
    total = 0
    empty = 0
    explicit_missing = 0
    observation = 0
    for _mid, item in by.items():
        if not isinstance(item, dict):
            continue
        if "observation" in str(item.get("mode") or ""):
            observation += 1
        factors = item.get("factors")
        if not isinstance(factors, dict):
            continue
        for _k, v in factors.items():
            total += 1
            if not isinstance(v, dict):
                empty += 1
                continue
            val = v.get("value")
            if val is None or str(val).strip() in ("", MISSING, "None"):
                empty += 1
            for key in ("status", "coverage_status", "availability"):
                if MISSING in str(v.get(key) or ""):
                    explicit_missing += 1
                    break
    return {"matches": len(by), "factors_total": total, "factors_value_missing": empty,
            "factors_explicit_unavailable": explicit_missing,
            "observation_mode_matches": observation,
            "note": "只数报告里已标为空/未取得的因子；observation 因子不参与评分，不代表模型缺陷"}


def zero_pick_reasons(report: dict) -> dict:
    picks = report.get("picks") or []
    notes = report.get("collection_notes") or []
    errs = report.get("collection_errors") or []
    return {"picks": len(picks), "is_zero_pick": len(picks) == 0,
            "collection_notes": [str(x) for x in notes] if isinstance(notes, list) else MISSING,
            "collection_errors": [str(x) for x in errs] if isinstance(errs, list) else MISSING,
            "note": "只列报告里已有的事实条目；不推断 0 场原因，不补造场次"}


def sample_precision(ledger: Path) -> dict:
    cols, rows = _read_csv(ledger)
    out = {"rows": len(rows), "settled_true": MISSING, "decided": MISSING,
           "hits": MISSING, "point_estimate": MISSING,
           "wilson_95_width": MISSING, "note": "点估计与区间宽度只说明精度，不构成命中率或优势结论"}
    if not rows or "ah_outcome" not in cols:
        return out
    if "settled" in cols:
        out["settled_true"] = sum(1 for r in rows if str(r.get("settled") or "").strip().lower() == "true")
    dec = [r for r in rows if str(r.get("ah_outcome") or "").strip().lower() in DECIDED_OUTCOMES]
    out["decided"] = len(dec)
    if not dec:
        return out
    wins = sum(1.0 if str(r["ah_outcome"]).strip().lower() == "win" else
               0.5 if str(r["ah_outcome"]).strip().lower() == "half_win" else 0.0 for r in dec)
    n, p = len(dec), wins / len(dec)
    out["hits"] = round(wins, 4)
    out["point_estimate"] = round(p, 4)
    z = 1.96
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    out["wilson_95_width"] = round(2 * half, 4)
    out["note"] += "；区间宽度越大说明样本越不足以支撑结论"
    return out


def build(date: str, root: Path | None = None, ledger: Path | None = None,
          snapshots_dir: Path | None = None) -> dict:
    base = Path(root) if root else ROOT
    rp = report_path(date, base)
    report = _load_json(rp)
    led = Path(ledger) if ledger else base / "data" / "predictions.csv"
    snaps = Path(snapshots_dir) if snapshots_dir else base / "data" / "snapshots" / "odds"
    out = {
        "schema": 1,
        "checked_at": _now(),
        "evidence_grade": "【已验证】本地文件解析事实",
        "report_date": date,
        "report_path": str(rp),
        "report_exists": rp.exists(),
        "report_parsed": report is not None,
        "note": NOTE,
        "unverified": [
            "盘口/水位是否经第二家独立来源核验（本盘点只读报告字段）",
            "快照时点是否即收盘时点（不读运行日志，不推断）",
            "采集实际使用了哪个源（本盘点不读日志）",
            "Telegram 接收端（本命令不发送任何消息）",
        ],
        "alerts": [],
    }
    if report is None:
        out["candidate_coverage"] = {"status": "REPORT_MISSING"}
        out["verification_facts"] = {"status": "REPORT_MISSING"}
        out["clv_field_coverage"] = clv_field_coverage(led, snaps)
        out["source_concentration"] = {"status": "REPORT_MISSING"}
        out["factor_gaps"] = {"status": "REPORT_MISSING"}
        out["zero_pick_reasons"] = {"status": "REPORT_MISSING"}
        out["sample_precision"] = sample_precision(led)
        out["alerts"].append({"code": "REPORT_MISSING", "severity": "info",
                              "message": "当日报告未找到；只记录事实，不补造", "evidence": {}})
        return out
    out["candidate_coverage"] = candidate_coverage(report)
    out["verification_facts"] = verification_facts(report)
    out["clv_field_coverage"] = clv_field_coverage(led, snaps)
    out["source_concentration"] = source_concentration(report)
    out["factor_gaps"] = factor_gaps(report)
    out["zero_pick_reasons"] = zero_pick_reasons(report)
    out["sample_precision"] = sample_precision(led)

    cc = out["candidate_coverage"]
    if isinstance(cc.get("not_covered_count"), int) and cc["not_covered_count"] > 0:
        out["alerts"].append({"code": "CANDIDATE_TRUNCATION", "severity": "info",
                              "message": f"窗口内 {cc['not_covered_count']} 场未进候选池（配置事实，不推断漏采）",
                              "evidence": {"max": cc["max_fixtures"], "total": cc["window_total"]}})
    clv = out["clv_field_coverage"]
    decided = out["sample_precision"].get("decided")
    if isinstance(decided, int) and decided > 0:
        close_filled = clv["field_filled"].get("final_line", 0)
        if close_filled < decided:
            out["alerts"].append({"code": "CLV_CLOSE_FIELDS_SPARSE", "severity": "info",
                                  "message": (f"收盘线字段 final_line 有值 {close_filled} 行，已结算 {decided} 行；"
                                              "只记录覆盖缺口，不推断结算质量"),
                                  "evidence": {"final_line_filled": close_filled, "decided": decided}})
    vf = out["verification_facts"]
    if isinstance(vf.get("items"), dict) and vf["items"]:
        out["alerts"].append({"code": "VERIFICATION_FACTS_RECORDED", "severity": "info",
                              "message": "报告已写明盘口核验各项场数，事实已抄录；不解释成因、不推断覆盖",
                              "evidence": vf["items"]})
    sc = out["source_concentration"]
    if sc.get("distinct_sources") == 1 and sc.get("picks", 0) > 0:
        out["alerts"].append({"code": "SINGLE_SOURCE_IN_REPORT", "severity": "info",
                              "message": "当日 picks 全部来自单一 source；只记录事实，不代表数据不足",
                              "evidence": {"by_source": sc["by_source"]}})
    if not out["alerts"]:
        out["alerts"].append({"code": "NO_ALERT", "severity": "info",
                              "message": "各项数据质量事实已记录，无异常", "evidence": {}})
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="数据质量事实盘点（只读，不写回台账/报告）")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD；默认北京今天")
    ap.add_argument("--out", default=None)
    ap.add_argument("--ledger", default=None)
    ap.add_argument("--snapshots-dir", default=None)
    a = ap.parse_args(argv)
    from datetime import timedelta
    date = a.date or (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")
    result = build(date, ledger=Path(a.ledger) if a.ledger else None,
                   snapshots_dir=Path(a.snapshots_dir) if a.snapshots_dir else None)
    if a.out:
        _atomic_write(Path(a.out), result)
    for x in result["alerts"]:
        if x["severity"] != "info":
            print(f"::{x['severity']}::{x['code']} {x['message']}")
    s = result["sample_precision"]
    clv = result["clv_field_coverage"]
    print(f"DATA_QUALITY date={date} report={result['report_parsed']} "
          f"rows={clv.get('rows')} joinable={clv.get('joinable_rows')} "
          f"decided={s.get('decided')} wilson_width={s.get('wilson_95_width')}")
    print(f"  candidate_not_covered={result['candidate_coverage'].get('not_covered_count')} "
          f"distinct_sources={result['source_concentration'].get('distinct_sources')} "
          f"alerts={','.join(x['code'] for x in result['alerts'])}")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
