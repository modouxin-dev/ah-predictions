"""选场漏斗：为什么今天只推了这几场 / 为什么 0 场（只读事实层，全标准库）。

定位：`reports/<YYYY>/<MM>/<date>.json` 里其实已经写了淘汰原因，但散在
`watchlist[].withheld_category`、`not_covered`、`collection_notes` 三处，跨天看不出
瓶颈在哪。本模块把它们收敛成一张按日事实表，**不重算、不推断、不补造**：

1. 按 `withheld_category` 白名单分成「数据缺口 / 盘口核验 / 模型判断 / 未知」；
   未知类别单列，绝不猜测归属。
2. 上游截断只从 `not_covered` 文本里严格正则取「筛查上限 N 场」「窗口内共发现 M 场」；
   缺字段写「未取得」，不拿配置值倒推。
3. 0 场日照常输出一行，保证「推不出」时也能定位。
4. 跨天只做计数与事实句，不输出「准确率提升 / 模型更准」类断言（生成后 fail-closed 自检）。

硬约束（AGENTS.md）：
- 不改评分、阈值、盘口核验、采集、推送与展示推荐内容；不发送 Telegram。
- 赛程 / 盘口 / 倾向一律不补造；所有数字来自报告已落盘字段。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import CONFIG, DATA, REPORTS

MISSING = "未取得"

# 类别白名单：只收录报告里真实出现过的**语义单一**类别名；复合类别一律不归并。
# 新版（11-06 起）与旧版（11-05 及更早）的类别名不同，逐字保留、不改原文。
CATEGORY_BUCKET = {
    # 新版
    "近期状态数据不足": "data_gap",
    "盘口报价过期，降级后不足": "data_gap",
    "盘口跨来源核验未通过": "verification",
    "基本面与盘口评分不足": "model_judgment",
    "盘口过深": "model_judgment",
    # 旧版（2026-11-05 报告实际取值）
    "无盘口": "data_gap",
    "深盘": "model_judgment",
    "信号不足": "model_judgment",
    "盘口与基本面背离": "model_judgment",
    "逆向退盘": "model_judgment",
    "已开球": "timing",
    "临近开赛": "timing",
    # 「深盘+机构分歧」是复合类别（同时含深度与核验），不猜测主因，留在 unknown。
}
BUCKETS = ("data_gap", "verification", "model_judgment", "timing", "unknown")
BUCKET_CN = {
    "data_gap": "数据缺口",
    "verification": "盘口核验",
    "model_judgment": "模型判断",
    "timing": "时间窗口",
    "unknown": "复合/未分类（不猜测）",
}

# 生成后自检：漏斗文案不得出现夸大/结论式断言（与 review.py 同一原则）。
FORBIDDEN_CLAIMS = (
    "准确率提升", "命中率提升", "模型更准", "ROI 改善", "收益改善",
    "保证命中", "稳赢", "必定", "已送达", "推送成功", "已成功推送",
)

NOTE = (
    "只读取 reports/*/*/*.json 已落盘字段：window_fixtures/screening_cap/outside_screen（新报告结构化）、not_covered、"
    "collection_notes、picks、collection_errors。缺字段写『未取得』，不推断未进候选的场次、"
    "不补造赛程/盘口/比分/倾向、不改任何评分或阈值。0 场日也照常输出事实行。"
)


def _stamp(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _upstream(report: dict) -> dict:
    """上游数字优先取报告结构化字段；旧报告没有才降级到 not_covered 文本正则。

    已写明的字段照实展示；缺字段写「未取得」，绝不拿 settings.yaml 的配置值倒推，
    也不在缺任一字段时推算「未进候选」。
    """
    out = {"window_fixtures": MISSING, "screening_cap": MISSING,
           "outside_screen": MISSING, "status": "NOT_RECORDED", "source": "NOT_RECORDED"}
    win, cap = report.get("window_fixtures"), report.get("screening_cap")
    if isinstance(win, int) and isinstance(cap, int) and win >= 0 and cap >= 0:
        out.update(window_fixtures=win, screening_cap=cap,
                   outside_screen=max(0, win - cap), status="RECORDED",
                   source="report 结构化字段")
        return out
    text = report.get("not_covered")
    if not isinstance(text, str) or not text:
        return out
    m_cap = re.search(r"筛查上限\s*(\d+)\s*场", text)
    m_win = re.search(r"窗口内共发现\s*(\d+)\s*场", text)
    if m_cap:
        out["screening_cap"] = int(m_cap.group(1))
    if m_win:
        out["window_fixtures"] = int(m_win.group(1))
    if m_cap and m_win:
        cap, win = int(m_cap.group(1)), int(m_win.group(1))
        out.update(outside_screen=max(0, win - cap), status="RECORDED",
                   source="not_covered 文本正则（旧报告兼容）")
    return out


def _day(path: Path) -> dict | None:
    r = _load_json(path)
    if not isinstance(r, dict) or not r.get("date"):
        return None
    wl = [x for x in (r.get("watchlist") or []) if isinstance(x, dict)]
    notes = [str(x) for x in (r.get("collection_notes") or [])]
    cats: dict[str, int] = {}
    buckets = {b: 0 for b in BUCKETS}
    gap_leagues: dict[str, int] = {}
    for x in wl:
        c = str(x.get("withheld_category") or "").strip() or "（未填写类别）"
        cats[c] = cats.get(c, 0) + 1
        b = CATEGORY_BUCKET.get(c, "unknown")
        buckets[b] += 1
        if b == "data_gap":
            lg = str(x.get("league") or "未填写联赛")
            gap_leagues[lg] = gap_leagues.get(lg, 0) + 1
    # 盘口核验明细：逐字拆 self-reported 事实，不解析成结论
    ver_facts: list[str] = []
    for n in notes:
        if n.startswith("盘口核验："):
            ver_facts.extend(p.strip() for p in n.split("：", 1)[1].split("；") if p.strip())
    gap_notes = [n for n in notes if n.startswith("近期状态不足：")]
    watched = r.get("watched") or []
    return {
        "date": str(r["date"]),
        "picks": len(r.get("picks") or []),
        "watchlist": len(wl),
        "watched_free_text_entries": len(watched) if isinstance(watched, list) else 0,
        "zero_pick_day": len(r.get("picks") or []) == 0,
        "generated_at": r.get("generated_at") or MISSING,
        "categories": dict(sorted(cats.items(), key=lambda kv: (-kv[1], kv[0]))),
        "buckets": buckets,
        "data_gap_by_league": dict(sorted(gap_leagues.items(), key=lambda kv: (-kv[1], kv[0]))),
        "upstream": _upstream(r),
        "verification_facts": ver_facts,
        "state_gap_note_count": len(gap_notes),
        "collection_notes": notes,
        "collection_errors": len(r.get("collection_errors") or []),
        "not_covered_text": r.get("not_covered") if isinstance(r.get("not_covered"), str) else MISSING,
    }


def _observations(days: list[dict], summary: dict) -> list[str]:
    """只写可逐字回溯的事实句，不写模型优劣或改善类结论。"""
    out: list[str] = []
    # 连续「近期状态数据不足」天数
    run, best_run, best_tail = 0, 0, []
    for d in days:
        n = (d["categories"] or {}).get("近期状态数据不足", 0)
        if n >= 5:
            run += 1
            best_tail.append(f"{d['date']}:{n}")
            if run > best_run:
                best_run, best_tail = run, best_tail[-run:]
        else:
            run = 0
            best_tail = []
    if best_run >= 2:
        out.append(f"连续 {best_run} 天『近期状态数据不足』≥5 场（{' / '.join(best_tail)}）；"
                   "依据：各日报告 watchlist.withheld_category 计数【已验证】报告字段")
    # 上游截断
    pairs = [(d["date"], d["upstream"]) for d in days if d["upstream"]["status"] == "RECORDED"]
    if pairs:
        mx = max(pairs, key=lambda kv: kv[1]["outside_screen"])
        out.append(f"窗口内最多有 {mx[1]['outside_screen']} 场因筛查上限未进入候选"
                   f"（{mx[0]}：{mx[1]['window_fixtures']}−{mx[1]['screening_cap']}）；"
                   "依据：报告 not_covered 文本【已验证】报告字段")
    # 数据侧 vs 模型侧
    dg = summary["buckets"]["data_gap"] + summary["buckets"]["verification"]
    mj = summary["buckets"]["model_judgment"]
    if dg or mj:
        out.append(f"未推场次中，数据/核验类合计 {dg} 场、模型判断类 {mj} 场；"
                   "这是分类计数，不构成模型优劣或准确率结论【已验证】报告字段汇总")
    if summary["buckets"]["unknown"]:
        names = sorted({c for d in days for c in d["categories"]
                        if CATEGORY_BUCKET.get(c, "unknown") == "unknown"})
        out.append(f"另有 {summary['buckets']['unknown']} 场使用复合/未收录类别"
                   f"（{'、'.join(names) or '未填写'}），已单列未分类，不猜测归属【已验证】报告字段")
    legacy = sorted({c for d in days for c in d["categories"]
                     if c in ("深盘", "信号不足", "盘口与基本面背离", "逆向退盘",
                              "已开球", "临近开赛", "无盘口")})
    if legacy:
        out.append(f"旧版报告类别名（{'、'.join(legacy)}）已逐字收录并按语义单一性归并；"
                   "复合类别不归并【已验证】报告字段")
    zeros = [d["date"] for d in days if d["zero_pick_day"]]
    if zeros:
        out.append(f"0 场推荐日：{', '.join(zeros)}；漏斗照常出表，便于定位【已验证】报告字段")
    # 伤停/首发事实
    if any("尚未自动采集伤停或首发" in str(d["not_covered_text"]) for d in days):
        out.append("报告自述『尚未自动采集伤停或首发』；不补造伤停信息【已验证】报告文本")
    return out


def build(reports: Path = REPORTS, config: Path | None = None,
          now: datetime | None = None) -> dict:
    """纯函数：只返回事实 dict，不写任何文件。"""
    files = sorted(p for p in Path(reports).glob("*/*/*.json") if "rechecks" not in str(p))
    days = [d for d in (_day(p) for p in files) if d]
    totals = {b: sum(d["buckets"][b] for d in days) for b in BUCKETS}
    gap_leagues: dict[str, int] = {}
    for d in days:
        for k, v in d["data_gap_by_league"].items():
            gap_leagues[k] = gap_leagues.get(k, 0) + v
    outside = [d["upstream"]["outside_screen"] for d in days
               if isinstance(d["upstream"]["outside_screen"], int)]
    alerts = []
    if totals["data_gap"] >= 5:
        alerts.append("DATA_GAP_PRESENT")
    if outside and max(outside) > 0:
        alerts.append("UPSTREAM_SCREEN_CAP")
    if any(d["zero_pick_day"] for d in days):
        alerts.append("ZERO_PICK_DAY")
    summary = {
        "days": len(days),
        "total_picks": sum(d["picks"] for d in days),
        "total_watchlist": sum(d["watchlist"] for d in days),
        "buckets": totals,
        "data_gap_by_league": dict(sorted(gap_leagues.items(), key=lambda kv: (-kv[1], kv[0]))),
        "max_outside_screen": max(outside) if outside else MISSING,
        "observations": [],
        "alerts": alerts,
    }
    summary["observations"] = _observations(days, summary)
    out = {
        "schema": 1,
        "checked_at": _stamp(now),
        "source": "reports/*/*/*.json（只读；不含 rechecks）",
        "report_files": len(files),
        "category_buckets": CATEGORY_BUCKET,
        "bucket_labels": BUCKET_CN,
        "note": NOTE,
        "days": days,
        "summary": summary,
        "unverified": [
            "未进入候选池的场次具体是哪些（报告未逐场记录）",
            "数据源为何缺失近期状态（需各自运行日志，本模块不推断）",
            "Telegram 接收端与外部定时执行情况",
        ],
    }
    # fail-closed：任何文案出现夸大断言就直接失败，绝不落盘。
    blob = json.dumps(out, ensure_ascii=False)
    for w in FORBIDDEN_CLAIMS:
        if w in blob and w not in NOTE:
            raise RuntimeError(f"漏斗文案出现禁止断言：{w}")
    return out


def cmd(out_path: Path | None = None, reports: Path = REPORTS,
        config: Path | None = None, now: datetime | None = None) -> dict:
    obj = build(reports=reports, config=config, now=now)
    out_path = out_path or (DATA / "selection_funnel.json")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".funnel-", dir=str(out_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print(f"[funnel] days={obj['summary']['days']} picks={obj['summary']['total_picks']} "
          f"data_gap={obj['summary']['buckets']['data_gap']} "
          f"model_judgment={obj['summary']['buckets']['model_judgment']} -> {out_path}")
    return obj


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m ah funnel", description="选场漏斗事实（只读，不改模型）")
    ap.add_argument("--out", type=Path, default=None, help="输出 JSON（默认 data/selection_funnel.json）")
    ap.add_argument("--reports", type=Path, default=REPORTS, help="日报 JSON 目录")
    ap.add_argument("--config", type=Path, default=None, help="配置目录（保留参数，当前不读取阈值）")
    a = ap.parse_args(argv)
    cmd(out_path=a.out, reports=a.reports, config=a.config)


if __name__ == "__main__":
    main()
