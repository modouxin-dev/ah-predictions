"""推荐记录：赛前确认(prematch)与赛后回测(result)，写在 reports/YYYY/MM/YYYY-MM-DD.json 每个 pick 上。

数据格式（pick 上的两个对象）::

  "prematch": {"checked_at": "2026-11-05T02:10:00Z",      # UTC ISO；网站显示北京时间
               "decision": "保留" | "调整" | "撤销",
               "new_line": -0.5 | null, "new_odds": 0.92 | null,   # 仅「调整」时填写（推荐方视角，港盘水位）
               "confidence": "高" | "中" | "低" | null,            # 复核后的信心
               "reason": "…", "sources": ["https://…"]}
  "result":   {"score": "2-1",                                 # 主-客，90 分钟（含补时）
               "settlement": "win|half_win|push|half_loss|loss|void",
               "settlement_cn": "赢|赢半|走水|输半|输|取消",
               "units": 0.92,                                  # 每注 1 单位
               "review": "…复盘…", "settled_at": "…Z", "sources": [...]}

旧字段 final_check（2026-11-04）只读兼容：prematch_of() 会把它映射为同样的结构。
同时同步 data/predictions.csv（stats 的输入），保证网站战绩与记录一致。
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from . import DATA, REPORTS, policy
from .settle import parse_score, profit, settle

DECISIONS = {"保留": "保留", "keep": "保留", "调整": "调整", "adjust": "调整", "撤销": "撤销", "cancel": "撤销"}
SETTLE_CN = {"win": "赢", "half_win": "赢半", "push": "走水", "half_loss": "输半", "loss": "输", "void": "取消"}
STATUSES = ("待确认", "已确认", "已撤销", "已结算")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def report_path(date: str, reports: Path = REPORTS) -> Path:
    y, m, _ = date.split("-")
    return reports / y / m / f"{date}.json"


def load_report(date: str, reports: Path = REPORTS) -> dict:
    return json.loads(report_path(date, reports).read_text(encoding="utf-8"))


def save_report(r: dict, reports: Path = REPORTS) -> Path:
    p = report_path(r["date"], reports)
    p.write_text(json.dumps(r, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return p


def find_pick(r: dict, match_id: str) -> dict:
    for p in r.get("picks", []):
        if p.get("match_id") == match_id:
            return p
    raise KeyError(f"{r.get('date')} 报告中没有 {match_id}")


# ---------- 读取（兼容旧 final_check） ----------

def prematch_of(p: dict) -> dict | None:
    if p.get("prematch"):
        return p["prematch"]
    fc = p.get("final_check")
    if not fc:
        return None
    return {"checked_at": fc.get("checked_at"), "decision": DECISIONS.get(fc.get("decision", ""), fc.get("decision")),
            "new_line": None, "new_odds": None, "confidence": None, "reason": fc.get("reason", ""),
            "current_line": fc.get("current_line"), "sources": fc.get("sources", []), "legacy": True}


def is_cancelled(p: dict) -> bool:
    pm = prematch_of(p) or {}
    return bool(p.get("cancelled")) or p.get("confidence") == "cancelled" or pm.get("decision") == "撤销"


def status(p: dict) -> str:
    if is_cancelled(p):
        return "已撤销"
    if (p.get("result") or {}).get("settlement"):
        return "已结算"
    if prematch_of(p):
        return "已确认"
    return "待确认"


def effective(p: dict) -> tuple[float, float | None, str]:
    """赛前调整后的 (盘口, 水位, 信心)。"""
    pm = prematch_of(p) or {}
    line = pm.get("new_line") if pm.get("decision") == "调整" and pm.get("new_line") is not None else p.get("line")
    odds = pm.get("new_odds") if pm.get("decision") == "调整" and pm.get("new_odds") is not None else p.get("odds")
    conf = pm.get("confidence") or p.get("confidence")
    return line, odds, policy.CN.get(conf, conf)


def published_picks(r: dict, pol: dict | None = None) -> list[dict]:
    """曾公开发布的推荐（含之后撤销的），用于「推荐记录」。新规则下未达标（watchlist/低信心）不出现。"""
    pol = pol or policy.load()
    if not policy.applies(r, pol):
        return list(r.get("picks", []))
    return [p for p in r.get("picks", []) if is_cancelled(p) or policy.check(p, pol)[0] is not None]


# ---------- 写入 ----------

def set_prematch(r: dict, match_id: str, decision: str, reason: str, new_line=None, new_odds=None,
                 confidence: str | None = None, sources=None, checked_at: str | None = None,
                 pol: dict | None = None) -> dict:
    if decision not in DECISIONS:
        raise ValueError(f"decision 必须是 保留/调整/撤销，收到 {decision!r}")
    if not reason:
        raise ValueError("reason 必填")
    dec = DECISIONS[decision]
    if dec == "调整" and new_line is None and new_odds is None and confidence is None:
        raise ValueError("「调整」需要至少给出 new_line / new_odds / confidence 之一")
    if new_line is not None and (float(new_line) * 4) % 1:
        raise ValueError(f"非法盘口 {new_line}")
    pol = pol or policy.load()
    p = find_pick(r, match_id)
    conf = policy.CN.get(confidence) if confidence else None
    if confidence and conf is None:
        raise ValueError(f"confidence 必须是 高/中/低，收到 {confidence!r}")
    old = policy.EN.get(p.get("confidence", ""))
    auto = ""
    if (dec != "撤销" and conf and old and pol.get("prematch_cancel_on_confidence_drop", True)
            and policy.ORDER[policy.EN[conf]] > policy.ORDER[old]):
        dec, auto = "撤销", f"（信心由{policy.CN[old]}降至{conf}，按规则自动撤销）"
    pm = {"checked_at": checked_at or _now(), "decision": dec,
          "new_line": None if new_line is None else float(new_line),
          "new_odds": None if new_odds is None else float(new_odds),
          "confidence": conf, "reason": reason + auto, "sources": list(sources or [])}
    p["prematch"] = pm
    if dec == "撤销":
        p["cancelled"] = True
        p["cancel_reason"] = pm["reason"]
    else:
        p.pop("cancelled", None)
    return pm


def set_result(r: dict, match_id: str, score: str | None, review: str | None = None, sources=None,
               settled_at: str | None = None, odds_format: str = "hk") -> dict:
    p = find_pick(r, match_id)
    prev = p.get("result") or {}
    line, odds, _ = effective(p)
    if is_cancelled(p):
        out, units = "void", 0.0
    else:
        if not score:
            raise ValueError("未撤销的推荐需要比分 --score 主-客")
        hg, ag = parse_score(score)
        pg, og = (hg, ag) if p["pick_side"] == "home" else (ag, hg)
        out = settle(pg, og, float(line))
        units = profit(out, odds, odds_format=odds_format)
    res = {"score": score or prev.get("score"), "settlement": out, "settlement_cn": SETTLE_CN[out], "units": units,
           "line": line, "odds": odds, "review": review if review is not None else prev.get("review"),
           "settled_at": settled_at or prev.get("settled_at") or _now(),
           "sources": list(sources if sources is not None else prev.get("sources", []))}
    p["result"] = res
    return res


# ---------- 与 predictions.csv 同步 ----------

def _read_csv(path: Path) -> list[dict]:
    from .settle import read_ledger  # v2 表头，兼容旧 15 列
    return read_ledger(path)


def _write_csv(path: Path, rows: list[dict]) -> None:
    from .settle import write_ledger  # 按 v2 表头写，不丢新列
    write_ledger(path, rows)


def sync_csv(r: dict, csv_path: Path = DATA / "predictions.csv") -> int:
    """把报告里的 prematch/result 写回 predictions.csv 对应行（按 date+match_id）。返回更新行数。"""
    rows = _read_csv(csv_path)
    n = 0
    for p in r.get("picks", []):
        row = next((x for x in rows if x.get("date") == r["date"] and x.get("match_id") == p.get("match_id")
                    and x.get("track", "official") == "official"), None)  # 实验盘行不与正式推荐同步
        if row is None:
            continue
        line, odds, conf = effective(p)
        res = p.get("result") or {}
        pm = prematch_of(p) or {}
        if is_cancelled(p):
            # 保留原信心（status 列标记撤销），并记录撤销时盘口 final_line/final_odds
            orig = policy.EN.get(p.get("confidence", ""), "")
            if orig and str(row.get("confidence", "")).lower() in ("", "cancelled"):
                row["confidence"] = orig
            row.update({"status": "cancelled", "ah_outcome": "void", "units": "0", "settled": "true"})
            if not row.get("final_line") and not row.get("final_odds"):
                from .migrate_csv import final_from_pick
                fl, fo = final_from_pick(p)
                row["final_line"] = "" if fl is None else f"{float(fl):g}"
                row["final_odds"] = "" if fo is None else f"{float(fo):g}"
            if res.get("score"):
                row["result"] = res["score"]
        else:
            row["line"] = f"{float(line):g}"
            if odds is not None:
                row["odds_if_known"] = f"{float(odds):g}"
                row["odds_estimated"] = "false"
            if pm.get("decision") == "调整":
                row["final_line"] = f"{float(line):g}"
                row["final_odds"] = "" if odds is None else f"{float(odds):g}"
            if row.get("status") == "cancelled":
                row["status"] = "active"  # 撤销被改回保留/调整
            row["confidence"] = policy.EN.get(conf, row.get("confidence", ""))
            if res.get("settlement"):
                row.update({"result": res.get("score") or "", "ah_outcome": res["settlement"],
                            "units": str(res["units"]), "settled": "true"})
        n += 1
    _write_csv(csv_path, rows)
    return n


def sync_from_csv(csv_path: Path = DATA / "predictions.csv", reports: Path = REPORTS) -> list[str]:
    """settle（自动回填比分）之后：把 csv 中已结算的比分/结果写入报告 JSON（不覆盖已有复盘）。"""
    changed = []
    by_date: dict[str, list[dict]] = {}
    for row in _read_csv(csv_path):
        if str(row.get("settled", "")).lower() == "true" and row.get("result") \
                and row.get("track", "official") == "official":  # 实验盘不写回报告
            by_date.setdefault(row["date"], []).append(row)
    for date, rows in by_date.items():
        try:
            r = load_report(date, reports)
        except (OSError, ValueError):
            continue
        dirty = False
        for row in rows:
            try:
                p = find_pick(r, row["match_id"])
            except KeyError:
                continue
            res = p.get("result") or {}
            if res.get("score") == row["result"] and res.get("settlement"):
                continue
            set_result(r, row["match_id"], row["result"], odds_format=row.get("odds_format") or "hk")
            dirty = True
            changed.append(f"{date} {row['match_id']}")
        if dirty:
            save_report(r, reports)
    return changed


def _refresh_stats(csv_path: Path) -> None:
    from .stats import compute
    s = compute(_read_csv(csv_path))
    (DATA / "stats.json").write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------- CLI ----------

def prematch_main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m ah prematch-record", description="写入赛前最终确认记录")
    ap.add_argument("--date", required=True, help="报告日期 YYYY-MM-DD")
    ap.add_argument("--match-id", required=True)
    ap.add_argument("--decision", required=True, help="保留 / 调整 / 撤销（或 keep/adjust/cancel）")
    ap.add_argument("--reason", required=True)
    ap.add_argument("--new-line", type=float, help="调整后盘口（推荐方视角，负=让球）")
    ap.add_argument("--new-odds", type=float, help="调整后港盘水位")
    ap.add_argument("--confidence", help="复核后信心 高/中/低；低于原信心将自动撤销")
    ap.add_argument("--source", action="append", default=[], help="可重复")
    ap.add_argument("--checked-at", help="UTC ISO，默认现在")
    a = ap.parse_args(argv)
    r = load_report(a.date)
    pm = set_prematch(r, a.match_id, a.decision, a.reason, a.new_line, a.new_odds, a.confidence, a.source, a.checked_at)
    if a.decision in DECISIONS and DECISIONS[a.decision] == "撤销" or pm["decision"] == "撤销":
        set_result(r, a.match_id, None)          # 撤销 = 作废，不计入命中率
    save_report(r)
    csv_path = DATA / "predictions.csv"
    sync_csv(r, csv_path)
    _refresh_stats(csv_path)
    print(json.dumps({"match_id": a.match_id, "status": status(find_pick(r, a.match_id)), **pm}, ensure_ascii=False))


def backtest_main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m ah backtest-record", description="写入赛后回测（比分、结算、复盘）")
    ap.add_argument("--date", required=True)
    ap.add_argument("--match-id", required=True)
    ap.add_argument("--score", help="主-客 90 分钟比分，如 2-1（已撤销的推荐可省略）")
    ap.add_argument("--review", required=True, help="复盘：判断对在哪、错在哪")
    ap.add_argument("--source", action="append", default=None)
    a = ap.parse_args(argv)
    r = load_report(a.date)
    res = set_result(r, a.match_id, a.score, a.review, a.source)
    save_report(r)
    csv_path = DATA / "predictions.csv"
    sync_csv(r, csv_path)
    _refresh_stats(csv_path)
    print(json.dumps({"match_id": a.match_id, "status": status(find_pick(r, a.match_id)), **res}, ensure_ascii=False))
