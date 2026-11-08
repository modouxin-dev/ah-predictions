"""预测准确率自动统计与复盘（facts only，旁路层，全标准库）。

定位：`data/stats.json` 是命中率/ROI 的唯一权威；本模块**不重算一套新口径当结论**，
而是做三件 stats.json 没做的事：

1. **独立对账**：从 `data/predictions.csv` 按 stats.json 公开的同一口径独立复算，
   与 stats.json 逐字段比对；不一致时报告里命中率写「未取得」，绝不输出结论数字。
2. **结算完整性 / 幸存者偏差防护**：`未取得` 赛果、`未结算` 积压、`void/cancelled`
   全部单列；只看已结算行的命中率会虚高，故必须同时报分母来源。
3. **可回溯复盘**：日/周报告 + 累计快照，逐场字段来自 CSV，按窗口与分桶标注样本量；
   样本不足一律 `INSUFFICIENT_SAMPLE`，禁止「准确率提升 / 模型更准 / ROI 改善」类断言。

硬约束（AGENTS.md）：
- 赛果只来自 `predictions.csv`；空值写「未取得」，不补造比分、不推断原因。
- 正式统计只认 `track=official`；实验盘不计入。
- 不改评分、阈值、盘口核验、展示与推送渲染；不发送 Telegram。
- 界面必须写「规则档位，未经独立回测；不构成统计预测准确率」。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import DATA

MISSING = "未取得"
OUTCOMES = ("win", "half_win", "push", "half_loss", "loss", "void")
MIN_SAMPLE = 30          # 总体结论所需最小已结算场次
MIN_BUCKET_SAMPLE = 10   # 分桶结论所需最小已结算场次
BACKLOG_MINUTES = 110    # 与 settle 的 autofill 等待规则一致
DRAWDOWN_STREAK = 5      # 连续未命中告警阈值

HIT_RATE_DEFINITION = "(win + 0.5*half_win)/(picks - push - void)"
ROI_DEFINITION = "units/(picks - push - void)（每注1单位）"
DISCLAIMER = "规则档位，未经独立回测；不构成统计预测准确率"

# 生成后自检：任何复盘文案都不得出现这些断言（fail-closed）
FORBIDDEN_CLAIMS = (
    "准确率提升", "命中率提升", "模型更准", "ROI 改善", "收益改善",
    "保证命中", "稳赢", "必定", "已送达", "推送成功", "已成功推送",
)

NOTE = (
    "赛果只来自 data/predictions.csv；空值写『未取得』，不补造比分、不推断未结算原因。"
    "命中率口径与 data/stats.json 一致；对账不一致时写『未取得』，不输出结论数字。"
    "样本不足时写 INSUFFICIENT_SAMPLE，不宣称准确率或 ROI 变化。"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(dt: datetime | None = None) -> str:
    return (dt or _now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bool(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("true", "1", "yes")


def _num(v: str | None) -> float:
    try:
        return float(str(v or "0").strip())
    except (TypeError, ValueError):
        return 0.0


def _official(r: dict) -> bool:
    """正式统计只认 official；缺省按 official 兼容旧 15 列文件。"""
    return (str(r.get("track") or "official").strip() or "official") == "official"


def _outcome(r: dict) -> str:
    """复刻 stats.compute 的作废归一：cancelled/void 一律 void、0 单位。"""
    if str(r.get("status") or "").strip() in ("cancelled", "void"):
        return "void"
    return str(r.get("ah_outcome") or "").strip()


def load_rows(csv_path: Path) -> list[dict]:
    if not csv_path.exists():
        raise RuntimeError(f"predictions.csv 不存在：{csv_path}；拒绝复盘不存在的数据")
    with csv_path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------- 对账

def recompute(rows: list[dict]) -> dict:
    """按 stats.json 公开口径独立复算总体桶；只用于与权威值对账，不作为对外结论。"""
    b = {"picks": 0, "win": 0, "half_win": 0, "push": 0, "half_loss": 0,
         "loss": 0, "void": 0, "units": 0.0}
    for r in rows:
        if not _official(r) or not _bool(r.get("settled")):
            continue
        if not str(r.get("ah_outcome") or "").strip():
            continue  # 与 stats.compute 一致：无结算结果的行不进桶
        out = _outcome(r)
        if out not in OUTCOMES:
            continue
        b["picks"] += 1
        b[out] += 1
        if out != "void":
            b["units"] += _num(r.get("units"))
    decided = b["picks"] - b["push"] - b["void"]
    hits = b["win"] + 0.5 * b["half_win"]
    b["decided"] = decided
    b["hits"] = hits
    b["hit_rate"] = round(hits / decided, 4) if decided else 0.0
    b["units"] = round(b["units"], 4)
    b["roi"] = round(b["units"] / decided, 4) if decided else 0.0
    return b


def reconcile(rows: list[dict], stats: dict) -> dict:
    """独立复算 vs stats.json 权威值；不一致时报告必须写「未取得」。"""
    mine = recompute(rows)
    theirs = stats.get("overall") or {}
    fields = ("picks", "win", "half_win", "push", "half_loss", "loss", "void",
              "decided", "hits", "units", "roi", "hit_rate")
    diffs = []
    for k in fields:
        if k not in theirs:
            diffs.append({"field": k, "csv": mine.get(k), "stats": MISSING})
            continue
        a, b = mine.get(k), theirs.get(k)
        if isinstance(a, float) or isinstance(b, float):
            same = abs(float(a or 0) - float(b or 0)) < 0.005
        else:
            same = a == b
        if not same:
            diffs.append({"field": k, "csv": a, "stats": b})
    return {
        "ok": not diffs,
        "status": "RECONCILED" if not diffs else "RECONCILIATION_MISMATCH",
        "hit_rate_definition": stats.get("hit_rate_definition") or HIT_RATE_DEFINITION,
        "independent_from_csv": mine,
        "authoritative_stats": {k: theirs.get(k, MISSING) for k in fields},
        "mismatches": diffs,
        "note": "对账一致才允许引用 stats.json 的命中率；不一致时复盘报告写『未取得』",
    }


# ---------------------------------------------------------------- 结算完整性

def settlement_integrity(rows: list[dict], now: datetime | None = None) -> dict:
    now = now or _now()
    total = len(rows)
    settled = pending = missing_result = void = decided = 0
    overdue = []
    result_missing = []
    for r in rows:
        if not _official(r):
            continue
        is_settled = _bool(r.get("settled"))
        out = _outcome(r)
        result = str(r.get("result") or "").strip()
        if not is_settled:
            pending += 1
            ko = str(r.get("kickoff_utc") or "").strip()
            try:
                ko_dt = datetime.strptime(ko, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if now - ko_dt > timedelta(minutes=BACKLOG_MINUTES):
                    overdue.append({"date": r.get("date") or MISSING,
                                    "match_id": r.get("match_id") or MISSING,
                                    "kickoff_utc": ko,
                                    "minutes_since_kickoff": int((now - ko_dt).total_seconds() // 60)})
            except ValueError:
                pass  # 开球时间未取得：不推断、不补造
            continue
        settled += 1
        if out == "void":
            void += 1
        if out not in OUTCOMES:
            continue
        if out not in ("push", "void"):
            decided += 1
        # settled=true 却没有真实比分：分母黑洞，必须单列
        if out not in ("void",) and (not result or result == MISSING):
            missing_result += 1
            result_missing.append({"date": r.get("date") or MISSING,
                                   "match_id": r.get("match_id") or MISSING,
                                   "ah_outcome": out,
                                   "result": MISSING})
    return {
        "total_rows": total,
        "settled_rows": settled,
        "pending_rows": pending,
        "decided_rows": decided,
        "void_rows": void,
        "missing_result_rows": missing_result,
        "missing_result": result_missing,
        "overdue_pending": overdue,
        "autofill_minutes_rule": BACKLOG_MINUTES,
        "note": "只看已结算行会让命中率虚高；pending / 未取得 / void 均单列，不进分母",
    }


# ---------------------------------------------------------------- 窗口 / 分桶 / 连败

def window(series: list[dict], end: str, days: int, quotable: bool = True) -> dict:
    """近 days 天（含 end）窗口；样本不足只报数字与样本量，不作结论。"""
    try:
        end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return {"days": days, "decided": 0, "status": "INSUFFICIENT_SAMPLE", "note": "日期格式无效"}
    start = end_dt - timedelta(days=days - 1)
    picked = []
    for x in series:
        try:
            d = datetime.strptime(str(x.get("date") or ""), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if start <= d <= end_dt and x.get("outcome") not in ("void", "push", None):
            picked.append(x)
    hits = sum(1.0 if x.get("outcome") == "win" else 0.5 if x.get("outcome") == "half_win" else 0.0
               for x in picked)
    units = round(sum(float(x.get("units") or 0) for x in picked), 4)
    return {
        "days": days,
        "start": start.strftime("%Y-%m-%d"),
        "end": end,
        "decided": len(picked),
        "hits": hits,
        # 总样本不足 / 对账不一致时，窗口也不输出命中率数字（防自相矛盾）。
        "hit_rate": (round(hits / len(picked), 4) if picked else None) if quotable else None,
        "units": units if quotable else None,
        "status": ("SUFFICIENT" if len(picked) >= MIN_BUCKET_SAMPLE else "INSUFFICIENT_SAMPLE")
                  if quotable else "INSUFFICIENT_SAMPLE",
    }


def current_streak(series: list[dict]) -> dict:
    """当前连续未命中（loss / half_loss）长度；只做事实计数，不预测未来。"""
    n = 0
    for x in reversed(series):
        if x.get("outcome") in ("loss", "half_loss"):
            n += 1
        else:
            break
    return {"current_loss_streak": n, "threshold": DRAWDOWN_STREAK,
            "note": "事实计数，不构成对未来场次的预测"}


def buckets(stats: dict, quotable: bool = True) -> dict:
    """只读引用 stats.json 的分桶，逐桶标注样本是否足以作结论。"""
    out = {}
    for name in ("by_confidence", "by_league", "by_depth", "by_role", "by_line"):
        src = stats.get(name) or {}
        items = {}
        for k, v in src.items():
            decided = int(v.get("decided") or 0)
            items[k] = {
                "decided": decided,
                "hit_rate": v.get("hit_rate", MISSING) if quotable else MISSING,
                "units": v.get("units", MISSING) if quotable else MISSING,
                "status": ("SUFFICIENT" if decided >= MIN_BUCKET_SAMPLE else "INSUFFICIENT_SAMPLE")
                          if quotable else "INSUFFICIENT_SAMPLE",
            }
        out[name] = items
    return out


# ---------------------------------------------------------------- 告警

def build_alerts(rec: dict, integ: dict, streak: dict, stats: dict) -> list[dict]:
    alerts: list[dict] = []

    def add(code: str, severity: str, message: str, evidence: dict) -> None:
        alerts.append({"code": code, "severity": severity, "message": message, "evidence": evidence})

    if not rec["ok"]:
        add("RECONCILIATION_MISMATCH", "error",
            "CSV 独立复算与 stats.json 权威值不一致；本次复盘不输出命中率结论",
            {"mismatches": rec["mismatches"]})
    if integ["overdue_pending"]:
        add("SETTLE_BACKLOG", "warning",
            f"有 {len(integ['overdue_pending'])} 场已过开球 +{BACKLOG_MINUTES} 分钟仍未结算；命中率分母不完整",
            {"rows": integ["overdue_pending"][:10]})
    if integ["missing_result_rows"]:
        add("MISSING_RESULT", "warning",
            f"有 {integ['missing_result_rows']} 场标记已结算但赛果为空；不补造比分，单列为分母黑洞",
            {"rows": integ["missing_result"][:10]})
    if integ["decided_rows"] < MIN_SAMPLE:
        add("INSUFFICIENT_SAMPLE", "warning",
            f"已结算 {integ['decided_rows']} 场 < {MIN_SAMPLE} 场；不构成统计预测准确率，禁止写准确率变化结论",
            {"decided": integ["decided_rows"], "min_sample": MIN_SAMPLE})
    if streak["current_loss_streak"] >= DRAWDOWN_STREAK:
        add("DRAWDOWN", "warning",
            f"当前连续未命中 {streak['current_loss_streak']} 场（阈值 {DRAWDOWN_STREAK}）；事实计数，非预测",
            {"streak": streak["current_loss_streak"]})
    stale = str(stats.get("updated_at") or "").strip()
    if stale:
        try:
            age_h = (_now() - datetime.strptime(stale, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds() / 3600
            if age_h > 26:
                add("STALE_STATS", "warning",
                    f"stats.json 已 {age_h:.1f} 小时未更新；本复盘可能基于旧结算数据，不推断原因",
                    {"updated_at": stale, "age_hours": round(age_h, 1)})
        except ValueError:
            pass
    est = (stats.get("odds_estimated") or {}).get("picks") or 0
    if est:
        add("ODDS_ESTIMATED", "info",
            f"有 {est} 场水位为估算值（0.9 港盘）；ROI 不可当作真实收益",
            {"picks": est})
    if not alerts:
        add("NO_ALERT", "info", "对账一致、结算完整、样本充足；无异常", {})
    return alerts


# ---------------------------------------------------------------- 渲染

def _line(v) -> str:
    return MISSING if v is None or v == "" else str(v)


def render_daily(date: str, payload: dict, run_id: str) -> str:
    rec, integ = payload["reconciliation"], payload["settlement_integrity"]
    ok = rec["ok"]
    can_quote = payload["quotable"]
    overall = rec["authoritative_stats"]
    rate = f"{float(overall['hit_rate']):.1%}" if can_quote and overall.get("hit_rate") not in (None, MISSING) else MISSING
    units = _line(overall.get("units")) if can_quote else MISSING

    L = [
        f"# 预测准确率日复盘 · {date}",
        "",
        f"- 生成时间：{payload['generated_at']}（UTC）",
        f"- run_id：{_line(run_id or MISSING)}",
        f"- 证据等级：【已验证】本地文件；【未验证】Telegram 接收端与外部定时执行情况",
        f"- 口径：{HIT_RATE_DEFINITION}；{ROI_DEFINITION}",
        f"- **{DISCLAIMER}**",
        "",
        "## 一、结算完整性（分母从哪来）",
        "",
        "| 项目 | 场数 | 说明 |",
        "|---|---|---|",
        f"| CSV 总行数 | {integ['total_rows']} | 含实验盘；下列计数仅 official |",
        f"| 已结算 | {integ['settled_rows']} | settled=true |",
        f"| 进入命中率分母 | {integ['decided_rows']} | 排除 push / void |",
        f"| 未结算 | {integ['pending_rows']} | 不进分母 |",
        f"| 作废 void | {integ['void_rows']} | 撤销/腰斩，0 单位 |",
        f"| 已结算但赛果未取得 | {integ['missing_result_rows']} | 分母黑洞，不补造比分 |",
        "",
        "> 只看已结算行会让命中率虚高。以上各项同时成立才构成完整分母。",
        "",
        "## 二、与 stats.json 对账",
        "",
        f"- 状态：**{rec['status']}**",
        f"- CSV 独立复算：{json.dumps(rec['independent_from_csv'], ensure_ascii=False)}",
    ]
    if not ok:
        L.append(f"- 不一致字段：{json.dumps(rec['mismatches'], ensure_ascii=False)}")
        L.append("- **对账不一致，本期命中率写「未取得」，不输出任何结论数字。**")
    else:
        L.append("- 逐字段一致（picks / win / half_win / push / loss / void / decided / hits / units / roi / hit_rate）。")
    L += [
        "",
        "## 三、本期表现",
        "",
        f"- 样本状态：**{payload['sample_status']}**（已结算 {integ['decided_rows']} 场，门槛 {MIN_SAMPLE} 场）",
        f"- 历史累计命中率：{rate}",
        f"- 历史累计单位盈亏：{units}",
    ]
    if not can_quote:
        L.append("- 未达到引用条件，数字留空；**不作改善类结论。**")
    L += ["", "## 四、窗口与分桶（均带样本量）", ""]
    for w in payload["windows"]:
        hr = f"{w['hit_rate']:.1%}" if w.get("hit_rate") is not None else MISSING
        L.append(f"- 近 {w['days']} 天（{w.get('start', MISSING)} ~ {w['end']}）："
                 f"已结算 {w['decided']} 场，命中率 {hr}，单位 {_line(w.get('units'))}，"
                 f"状态 **{w['status']}**")
    L.append(f"- 当前连续未命中：{payload['streak']['current_loss_streak']} 场（阈值 {DRAWDOWN_STREAK}；事实计数，非预测）")
    L += ["", "| 分桶 | 桶 | 已结算 | 状态 |", "|---|---|---|---|"]
    for gname, items in payload["buckets"].items():
        for k, v in items.items():
            L.append(f"| {gname} | {k} | {v['decided']} | {v['status']} |")
    L += [
        "",
        "> 分桶 `INSUFFICIENT_SAMPLE` 时不作结论；桶名逐字取自 stats.json，不做译名推断。",
        "",
        "## 五、告警",
        "",
    ]
    for a in payload["alerts"]:
        L.append(f"- **{a['code']}**〔{a['severity']}〕{a['message']}")
    L += [
        "",
        "## 六、边界声明",
        "",
        f"- {NOTE}",
        "- 本报告不发送 Telegram；「复盘结论」不等于接收端确认。",
        "- 未触碰模型评分、阈值、盘口核验、展示与推送渲染；未改动任何赛程、盘口、水位、比分或推荐倾向。",
        "",
    ]
    return "\n".join(L)


def render_weekly(end: str, payload: dict) -> str:
    w30 = next((w for w in payload["windows"] if w["days"] == 30), None)
    w7 = next((w for w in payload["windows"] if w["days"] == 7), None)
    L = [
        f"# 预测准确率周复盘 · 截至 {end}",
        "",
        f"- 生成时间：{payload['generated_at']}（UTC）",
        f"- 口径：{HIT_RATE_DEFINITION}",
        f"- **{DISCLAIMER}**",
        "",
        "## 本期窗口（数字变化 ≠ 准确率变化）",
        "",
        "| 窗口 | 已结算 | 命中率 | 单位 | 样本状态 |",
        "|---|---|---|---|---|",
    ]
    for w in (w7, w30):
        if not w:
            continue
        hr = f"{w['hit_rate']:.1%}" if w.get("hit_rate") is not None else MISSING
        L.append(f"| 近 {w['days']} 天 | {w['decided']} | {hr} | {_line(w.get('units'))} | {w['status']} |")
    L += [
        "",
        "> 窗口之间样本量不同，**只陈述数值，不写改善类结论**；"
        "样本不足的窗口一律标注 INSUFFICIENT_SAMPLE。",
        "",
        "## 本期告警",
        "",
    ]
    for a in payload["alerts"]:
        L.append(f"- {a['code']}〔{a['severity']}〕{a['message']}")
    L += [
        "",
        "## 待办与未验证",
        "",
        "- 未结算积压的成因：需查 settle 运行日志，本报告不推断。",
        "- Telegram 接收端是否读到复盘结论：未验证（本流程不发送）。",
        "- 概率校准 / 可靠性曲线：无校准概率，NOT_COMPUTED。",
        "",
        f"- {NOTE}",
        "",
    ]
    return "\n".join(L)


def _assert_no_claims(*texts: str) -> None:
    for t in texts:
        for word in FORBIDDEN_CLAIMS:
            if word in t:
                raise RuntimeError(f"复盘文案含禁止断言词 {word!r}，拒绝输出")


def _atomic_write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
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


# ---------------------------------------------------------------- 主流程

def build_payload(csv_path: Path, stats_path: Path, date: str,
                  run_id: str = "", now: datetime | None = None) -> dict:
    if not stats_path.exists():
        raise RuntimeError(f"stats.json 不存在：{stats_path}；拒绝复盘不存在的数据")
    rows = load_rows(csv_path)
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    rec = reconcile(rows, stats)
    integ = settlement_integrity(rows, now)
    series = stats.get("series") or []
    enough = integ["decided_rows"] >= MIN_SAMPLE
    quotable = bool(rec["ok"] and enough)
    windows = [window(series, date, d, quotable) for d in (7, 30)]
    streak = current_streak(series)
    alerts = build_alerts(rec, integ, streak, stats)
    return {
        "schema": 1,
        "generated_at": _stamp(now),
        "date": date,
        "run_id": run_id or MISSING,
        "source": {"results": str(csv_path), "authoritative_stats": str(stats_path)},
        "hit_rate_definition": HIT_RATE_DEFINITION,
        "roi_definition": ROI_DEFINITION,
        "disclaimer": DISCLAIMER,
        "sample_status": "SUFFICIENT" if enough else "INSUFFICIENT_SAMPLE",
        "quotable": quotable,
        "min_sample": MIN_SAMPLE,
        "reconciliation": rec,
        "settlement_integrity": integ,
        "windows": windows,
        "streak": streak,
        "buckets": buckets(stats, quotable),
        "alerts": alerts,
        "note": NOTE,
    }


def run(date: str, csv_path: Path | None = None, stats_path: Path | None = None,
        out_dir: Path | None = None, run_id: str = "", check: bool = False,
        now: datetime | None = None) -> dict:
    # 运行时读取包常量：测试可安全重定向到临时目录，不污染仓库真实数据。
    from . import DATA as runtime_data
    csv_p = Path(csv_path) if csv_path else runtime_data / "predictions.csv"
    stats_p = Path(stats_path) if stats_path else runtime_data / "stats.json"
    payload = build_payload(csv_p, stats_p, date, run_id, now)
    daily_md = render_daily(date, payload, run_id)
    weekly_md = render_weekly(date, payload)
    _assert_no_claims(daily_md, weekly_md)

    if check:
        print("REVIEW_CHECK " + json.dumps({
            "date": date,
            "reconciliation": payload["reconciliation"]["status"],
            "sample_status": payload["sample_status"],
            "decided": payload["settlement_integrity"]["decided_rows"],
            "alerts": [a["code"] for a in payload["alerts"]],
        }, ensure_ascii=False))
        return payload

    out = Path(out_dir) if out_dir else runtime_data.parent / "docs" / "review"
    ledger = runtime_data / "accuracy_review.json"
    # 累计快照：只追加，不覆盖历史；同日重复运行覆盖当日快照，保持幂等。
    hist = {"schema": 1, "snapshots": []}
    if ledger.exists():
        try:
            hist = json.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            hist = {"schema": 1, "snapshots": []}
    snap = {
        "date": date,
        "generated_at": payload["generated_at"],
        "run_id": run_id or MISSING,
        "sample_status": payload["sample_status"],
        "reconciliation": payload["reconciliation"]["status"],
        "decided": payload["settlement_integrity"]["decided_rows"],
        "pending": payload["settlement_integrity"]["pending_rows"],
        "missing_result": payload["settlement_integrity"]["missing_result_rows"],
        "hit_rate": (payload["reconciliation"]["authoritative_stats"].get("hit_rate")
                     if payload["reconciliation"]["ok"] else MISSING),
        "alerts": [a["code"] for a in payload["alerts"]],
    }
    snaps = [x for x in hist.get("snapshots", []) if x.get("date") != date]
    snaps.append(snap)
    snaps.sort(key=lambda x: x.get("date") or "")
    hist["schema"] = 1
    hist["snapshots"] = snaps
    hist["updated_at"] = payload["generated_at"]
    hist["note"] = NOTE

    _atomic_write(ledger, json.dumps(hist, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(out / f"{date}.md", daily_md)
    iso = datetime.strptime(date, "%Y-%m-%d").isocalendar()
    _atomic_write(out / f"weekly-{iso[0]}-W{iso[1]:02d}.md", weekly_md)

    for a in payload["alerts"]:
        if a["severity"] == "info":
            continue
        tag = "::error::" if a["severity"] == "error" else "::warning::"
        print(f"{tag}{a['code']} {a['message']}")
    print(f"REVIEW_OK date={date} reconciliation={payload['reconciliation']['status']} "
          f"sample={payload['sample_status']} decided={payload['settlement_integrity']['decided_rows']} "
          f"alerts={','.join(a['code'] for a in payload['alerts'])} out={out}")
    return payload


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="预测准确率自动统计与复盘（只读事实，不改模型、不发消息）")
    ap.add_argument("--date", default=_now().strftime("%Y-%m-%d"), help="复盘日期 YYYY-MM-DD（默认今天 UTC）")
    ap.add_argument("--csv", default=None, help="predictions.csv 路径")
    ap.add_argument("--stats", default=None, help="stats.json 路径")
    ap.add_argument("--out-dir", default=None, help="复盘报告目录（默认 docs/review）")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--check", action="store_true", help="只做对账与告警，不落盘")
    a = ap.parse_args(argv)
    datetime.strptime(a.date, "%Y-%m-%d")  # 非法日期立即失败
    run(a.date, a.csv, a.stats, a.out_dir, a.run_id, a.check)


if __name__ == "__main__":
    main()
