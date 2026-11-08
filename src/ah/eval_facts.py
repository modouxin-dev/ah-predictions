"""评估边界 / 联赛事实 / 结构化告警（只读事实层，全标准库）。

收敛 A3~A5 三件「一次性可做完」的事，全部基于仓库已落盘字段，不改任何模型：

A3 **研究层 ≠ 线上层**：`backtest_report.json` / `rule_lab.json` 评估的是离线模型或
   线上规则的时序样本外表现，与每天真正发出推荐的规则档位不是同一套可互证结论。
   本模块只把两边已有的 `model_under_test` / `evaluation.status` / `thresholds` 逐字摆出来，
   显式标注「结论不可迁移」，不重算、不调和、不改任何文件。

A4 **按联赛事实分桶**：把 `stats.json.by_league` 的已结算计数与
   `selection_funnel.json` 的数据缺口计数并排展示；样本不足一律写「未取得」，
   不输出「哪个联赛更好推」这类结论。持续缺口只做天数计数，不称模型漂移。

A5 **结构化告警**：0 场日、连续数据缺口、上游截断、价格型 CLV 未取得、研究层口径边界
   全部落成带 evidence 的条目，`action` 一律 `notify_only`——只告警，
   不自动补发、不递归触发、不发送 Telegram。

硬约束（AGENTS.md）：不补造赛程/盘口/水位/比分/倾向；不改评分、阈值、策略、核验、展示推荐。
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import DATA, REPORTS

MISSING = "未取得"
MIN_SAMPLE = 30  # 与 review.py 一致，不另立门槛

FORBIDDEN_CLAIMS = (
    "准确率提升", "命中率提升", "模型更准", "ROI 改善", "收益改善",
    "保证命中", "稳赢", "必定", "已送达", "推送成功", "已成功推送",
)

NOTE = (
    "只读取仓库已落盘 JSON 与报告字段并原样陈列；缺文件写「暂无」，缺字段写「未取得」。"
    "研究层与线上发布层的结论不可互证、不可迁移；样本不足不输出命中率或 ROI 结论；"
    "告警只通知，不自动补发、不递归触发、不发送 Telegram；不补造任何赛程、盘口、水位、比分或倾向。"
)


def _stamp(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _model_scope(backtest, rule_lab) -> dict:
    b = backtest or {}
    rl = rule_lab or {}
    ev = b.get("evaluation") or {}
    wf = (rl.get("walk_forward") or {}).get("overall") or {}
    facts = []
    if rl.get("model_under_test"):
        facts.append(f"rule_lab.model_under_test：{rl['model_under_test']}")
    if rl.get("live_model_modified") is not None:
        facts.append(f"rule_lab.live_model_modified：{rl['live_model_modified']}")
    if rl.get("joined_decided_rows") is not None:
        facts.append(f"rule_lab.joined_decided_rows：{rl['joined_decided_rows']}")
    if ev.get("status"):
        facts.append(f"backtest.evaluation.status：{ev['status']}")
    if ev.get("thresholds"):
        facts.append(f"backtest 门槛：{json.dumps(ev['thresholds'], ensure_ascii=False)}")
    if wf.get("status"):
        facts.append(f"rule_lab 样本状态：{wf['status']}")
    return {
        "boundary": "研究层（离线回测 / 线上规则 walk-forward）与每日实际发布不是同一套可互证结论；"
                    "两边样本、输入与评估对象不同，任何一边的数字都不能迁移成另一边的准确率或 ROI 声明。",
        "research_facts": facts,
        "live_publication_note": "线上发布为规则档位，未经独立回测；不构成统计预测准确率（沿用 review.py 口径）。",
        "files": ["data/backtest_report.json", "data/rule_lab.json"],
        "status": "FACTS_ONLY" if facts else "暂无研究层事实文件",
    }


def _league_facts(stats, funnel) -> dict:
    by = (stats or {}).get("by_league") or {}
    gaps = ((funnel or {}).get("summary") or {}).get("data_gap_by_league") or {}
    names = sorted(set(by) | set(gaps))
    rows = {}
    for name in names:
        b = by.get(name) or {}
        decided = _int(b.get("decided")) or 0
        rows[name] = {
            "settled_rows": _int(b.get("picks")) or 0,
            "decided": decided,
            "data_gap_withheld": _int(gaps.get(name)) or 0,
            "hit_rate_conclusion": MISSING if decided < MIN_SAMPLE else (
                b.get("hit_rate") if b.get("hit_rate") is not None else MISSING),
            "sample_status": "INSUFFICIENT_SAMPLE" if decided < MIN_SAMPLE else "SUFFICIENT",
        }
    # 连续缺口天数：只数事实，不称「漂移」、不推断成因
    days = (funnel or {}).get("days") or []
    run = consecutive = 0
    for d in days:
        n = (d.get("categories") or {}).get("近期状态数据不足", 0)
        if isinstance(n, int) and n >= 5:
            run += 1
            consecutive = max(consecutive, run)
        else:
            run = 0
    return {
        "min_sample": MIN_SAMPLE,
        "by_league": rows,
        "consecutive_data_gap_days": consecutive,
        "note": "「已结算 / 数据缺口」都是字段计数；样本不足一律写「未取得」，"
                "不输出联赛优劣、可预测性或漂移结论。持续天数只作事实，不推断数据源原因。",
        "source": ["data/stats.json", "data/selection_funnel.json"],
    }


def _alerts(funnel, clv_join, scope, reports) -> list[dict]:
    out: list[dict] = []

    def add(code, severity, message, evidence):
        out.append({"code": code, "severity": severity, "message": message,
                    "evidence": evidence, "action": "notify_only"})

    f = funnel or {}
    days = f.get("days") or []
    summary = f.get("summary") or {}
    zeros = [d.get("date") for d in days if d.get("zero_pick_day")]
    if zeros:
        add("ZERO_PICK_DAY", "warning", f"存在 0 场推荐日：{', '.join(str(x) for x in zeros)}；漏斗照常出表可定位",
            "selection_funnel.json days[].zero_pick_day")
    obs = summary.get("observations") or []
    persistent = next((x for x in obs if "连续" in x and "近期状态数据不足" in x), None)
    if persistent:
        add("PERSISTENT_DATA_GAP", "warning", persistent, "selection_funnel.json summary.observations")
    mx = summary.get("max_outside_screen")
    if isinstance(mx, int) and mx > 0:
        add("UPSTREAM_SCREEN_CAP", "info", f"单日最多 {mx} 场因筛查上限未进候选池；未进候选的具体场次报告未记录",
            "selection_funnel.json summary.max_outside_screen")
    cj = clv_join or {}
    statuses = cj.get("price_clv_status_by_row") or {}
    if (cj.get("joined_unique_match_ids") or 0) >= 0 and statuses:
        not_quote = sum(v for k, v in statuses.items() if k != "已计算价格型 CLV")
        add("PRICE_CLV_NOT_QUOTED", "info",
            f"价格型 CLV 仍为「未取得」覆盖 {not_quote} 行；原因是仓库口径要求收盘盘口=下注盘口且两侧水位齐全，"
            "线差只写 line_move，绝不冒充 CLV。这是口径事实，不是采集失败。",
            "data/clv_join_facts.json price_clv_status_by_row")
    if (scope or {}).get("status") == "FACTS_ONLY":
        add("MODEL_SCOPE_BOUNDARY", "info",
            "研究层回测与线上发布规则不是同一套可互证结论；不得把任一边数字迁移成准确率声明",
            "data/eval_facts.json model_scope.boundary")
    # 报告里自述的采集缺口（逐字，不推断）
    if reports:
        add("COLLECTION_SELF_REPORT", "info",
            "报告 not_covered 自述「尚未自动采集伤停或首发」；不补造伤停信息",
            "reports/*/*/*.json not_covered 原文")
    return out


def build(data: Path = DATA, reports: Path = REPORTS, now: datetime | None = None) -> dict:
    data, reports = Path(data), Path(reports)
    backtest = _load(data / "backtest_report.json")
    rule_lab = _load(data / "rule_lab.json")
    stats = _load(data / "stats.json")
    funnel = _load(data / "selection_funnel.json")
    clv_join = _load(data / "clv_join_facts.json")
    scope = _model_scope(backtest, rule_lab)
    leagues = _league_facts(stats, funnel)
    report_files = sorted(p for p in reports.glob("*/*/*.json") if "rechecks" not in str(p))
    self_reported = any(
        "尚未自动采集伤停或首发" in str((_load(p) or {}).get("not_covered") or "")
        for p in report_files)
    alerts = _alerts(funnel, clv_join, scope, self_reported)
    out = {
        "schema": 1,
        "checked_at": _stamp(now),
        "evidence_grade": "【已验证】本地文件解析事实",
        "model_scope": scope,
        "league_facts": leagues,
        "alerts": alerts,
        "alert_policy": "notify_only：只落盘与展示，不自动补发、不递归触发、不发送 Telegram",
        "note": NOTE,
        "unverified": [
            "未进候选池的具体场次（报告未逐场记录）",
            "数据源为何缺失近期状态（需各自运行日志，不推断）",
            "收盘价是否即该机构最终收盘价",
            "Telegram 接收端与外部定时执行情况",
        ],
    }
    blob = json.dumps(out, ensure_ascii=False)
    for w in FORBIDDEN_CLAIMS:
        if w in blob and w not in NOTE:
            raise RuntimeError(f"评估事实文案出现禁止断言：{w}")
    return out


def cmd(out_path: Path | None = None, data: Path = DATA, reports: Path = REPORTS,
        now: datetime | None = None) -> dict:
    obj = build(data=data, reports=reports, now=now)
    out_path = Path(out_path or (data / "eval_facts.json"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".eval-", dir=str(out_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print(f"[eval-facts] model_scope={obj['model_scope']['status']} "
          f"leagues={len(obj['league_facts']['by_league'])} alerts={len(obj['alerts'])} -> {out_path}")
    return obj


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m ah eval-facts",
                                 description="评估边界 / 联赛事实 / 结构化告警（只读，不改模型）")
    ap.add_argument("--out", type=Path, default=None, help="输出 JSON（默认 data/eval_facts.json）")
    ap.add_argument("--data", type=Path, default=DATA, help="事实文件目录")
    ap.add_argument("--reports", type=Path, default=REPORTS, help="日报 JSON 目录")
    a = ap.parse_args(argv)
    cmd(out_path=a.out, data=a.data, reports=a.reports)


if __name__ == "__main__":
    main()
