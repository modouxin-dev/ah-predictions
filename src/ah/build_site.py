"""生成 site/ 静态看板（GitHub Pages）。

页面：index(今日) / records(推荐记录) / stats(战绩，含 CLV 与模拟单) / sources(数据源) / rules(规则) / history(按日报告)
以及 reports/<date>.html。所有时间显示为北京时间（Asia/Shanghai）。
同时导出 site/data/*.json；排序/筛选/图表/VIP 门禁由 site/assets/app.js 在浏览器完成（无外部依赖）。
VIP 门禁仅前端隐藏：页面 HTML 与仓库数据仍然公开。
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import CONFIG, DATA, REPORTS, ROOT, SITE, TEMPLATES, funnel as funnel_mod, model_lab, policy, records, source_status
from .review import DISCLAIMER as REVIEW_DISCLAIMER, MIN_BUCKET_SAMPLE, MIN_SAMPLE, MISSING
from .stats import compute, window

from .timeutil import BJT
CONF = {"high": "高", "medium": "中", "low": "低", "高": "高", "中": "中", "低": "低", "cancelled": "已撤销"}
NAV = [("index.html", "今日"), ("records.html", "推荐记录"), ("stats.html", "战绩"), ("review.html", "复盘"),
       ("sources.html", "数据源"), ("rules.html", "规则"), ("model-lab.html", "实验场")]

# 复盘/研究事实文件：只展示仓库里真实落盘的内容，缺失一律写「暂无」，不推断、不补造。
REVIEW_FACTS = [
    ("accuracy_review.json", "准确率复盘快照"),
    ("rule_lab.json", "线上规则滚动回测"),
    ("backtest_report.json", "时序回测报告"),
    ("sample_gaps.json", "样本缺口盘点"),
    ("feature_readiness.json", "因子就绪度"),
    ("clv_backfill.json", "CLV 回填事实"),
    ("clv_join_facts.json", "收盘快照 join 事实"),
    ("eval_facts.json", "评估边界 / 联赛事实 / 告警"),
]
LOCK = "🔒 VIP 可见，输入密码解锁"
NONE = "暂无"
CREDIT = "Football data by 5DollarFootballAPI"
FREEZE_STAMP = "赛前研究·已冻结·非临场"
e = html.escape


# ---------- 时间 ----------

def parse_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def bjt(s, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """UTC ISO -> 'YYYY-MM-DD HH:MM 北京时间'；无法解析返回暂无。"""
    d = s if isinstance(s, datetime) else parse_utc(s)
    return f"{d.astimezone(BJT).strftime(fmt)} 北京时间" if d else NONE


def bjt_today(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(BJT).strftime("%Y-%m-%d")


# ---------- 格式 ----------

def fl(x) -> str:
    if x is None or x == "":
        return NONE
    v = float(x)
    return "0" if v == 0 else f"{v:+g}"


def water(x) -> str:
    return NONE if x is None or x == "" else f"{float(x):.2f}"


def units_html(u) -> str:
    if u is None or u == "":
        return NONE
    u = float(u)
    return f"<span class='{'pos' if u > 0 else 'neg' if u < 0 else ''}'>{u:+.2f}</span>"


def pct(x) -> str:
    return NONE if x is None else f"{x:.1%}"


# ---------- 命中率结论门槛（与 review.py 的复盘层同一口径） ----------

def quotable(decided, bucket: bool = False) -> bool:
    """样本不足不展示命中率/ROI 结论数字：总体 30 场、分桶 10 场。"""
    if decided is None:
        return False
    try:
        return int(decided) >= (MIN_BUCKET_SAMPLE if bucket else MIN_SAMPLE)
    except (TypeError, ValueError):
        return False


def site_can_quote(s) -> bool:
    """与 review.py 一致：总体样本不足时，窗口/分桶结论也一律不输出。"""
    return quotable((s or {}).get("overall", {}).get("decided"))


def rate_cell(rate, decided, bucket: bool = False, s=None) -> str:
    """不足门槛一律写「未取得」，避免把小样本当结论（复盘层同样处理）。"""
    if s is not None and not site_can_quote(s):
        return MISSING
    return pct(rate) if quotable(decided, bucket) and rate is not None else MISSING


def site_summary_line(s: dict | None) -> str:
    """站点专用摘要。stats.py 的 summary_line 供 Telegram 使用，此处不改动、不复用。"""
    if not s:
        return "暂无统计数据"
    o = s.get("overall", {})
    n = int(o.get("decided") or 0)
    if quotable(n):
        return f"📊 历史命中率 {o['hit_rate']:.1%}（{n}场，{o['units']:+.2f}单位）"
    return (f"📊 历史命中率 {MISSING} · 已结算 {n} 场 < {MIN_SAMPLE} 场门槛，"
            f"{REVIEW_DISCLAIMER}；详见 <a href='review.html'>复盘页</a>")


def lock(inner: str, block: bool = True) -> str:
    return f"<div class='vip'>{inner}</div><div class='vip-lock' role='button' tabindex='0'>{LOCK}</div>"


def tbl(head: list[str], rows: list[str], cls: str = "sortable", tid: str = "", empty: str = "暂无数据") -> str:
    if not rows:
        rows = [f"<tr class='emptyrow'><td colspan='{len(head)}'>{e(empty)}</td></tr>"]
    idattr = f" id='{tid}'" if tid else ""
    return (f"<div class='tbl'><table class='{cls}'{idattr}><thead><tr>" + "".join(f"<th>{h}</th>" for h in head)
            + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")


# ---------- 公共骨架 ----------

def settings() -> dict:
    try:
        return yaml.safe_load((CONFIG / "settings.yaml").read_text(encoding="utf-8")) or {}
    except OSError:
        return {}


def page(title: str, body: str, cur: str = "", base: str = "", generated: str = "", vip_hash: str = "") -> str:
    # 不含反斜杠的 f-string 表达式：兼容 Python 3.10/3.12，渲染结果不变。
    nav_items = []
    for h, t in NAV:
        cls = ' class="cur"' if h == cur else ''
        nav_items.append(f"<a href='{base}{h}'{cls}>{t}</a>")
    nav = "".join(nav_items)
    gen = f" · 页面生成于 {e(generated)}" if generated else ""
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>"
        f"<meta name='vip-hash' content='{e(vip_hash)}'><meta name='theme-color' content='#0b6e4f'>"
        f"<title>{e(title)} · 先知</title><link rel='stylesheet' href='{base}assets/app.css'></head><body>"
        f"<header class='top'><div class='wrap bar'><a class='logo' href='{base}index.html'>先知<small>亚盘预测看板</small></a>"
        f"<nav class='main'>{nav}</nav><button id='vipbtn' class='vipbtn' type='button'>🔒 VIP</button></div></header>"
        f"<main class='wrap'><h1>{e(title)}</h1>{body}</main>"
        f"<footer class='wrap'><div class='disc'>{e(policy.DISCLAIMER)}</div>"
        f"<div>先知 · 所有时间为北京时间（UTC+8）{gen}</div><div>{CREDIT}</div></footer>"
        "<div id='vipmodal' class='modal' role='dialog' aria-modal='true'><div class='box'><b>VIP 解锁</b>"
        "<div class='muted' style='font-size:13px'>输入 VIP 密码查看盘口面板、理由、赛前/赛后记录与完整战绩。</div>"
        "<input type='password' inputmode='numeric' autocomplete='off' maxlength='32' aria-label='VIP 密码'>"
        "<div class='err'></div><div class='row'><button class='cancel' type='button'>取消</button>"
        "<button class='ok' type='button'>解锁</button></div></div></div>"
        f"<script src='{base}assets/app.js'></script></body></html>")


def summary_bar(today_n: int | None, today_label: str, s: dict | None, today: str) -> str:
    o = (s or {}).get("overall", {})
    decided = o.get("decided")
    hit = rate_cell(o.get("hit_rate"), decided, s=s)
    hit_sub = (f"总体命中率（{decided}场）" if quotable(decided)
               else f"总体命中率 {MISSING}（已结算 {int(decided or 0)}/{MIN_SAMPLE} 场门槛）")
    w = window((s or {}).get("series", []), today, 30)
    u = f"{w['units']:+.2f}" if w["picks"] else "暂无"
    cls = "pos" if w["picks"] and w["units"] > 0 else "neg" if w["picks"] and w["units"] < 0 else ""
    n = "暂无" if today_n is None else str(today_n)
    return (f"<div class='summary'><div class='kpi'><b>{n}</b><span>{e(today_label)}</span></div>"
            f"<div class='kpi'><b>{hit}</b><span>{e(hit_sub)}</span></div>"
            f"<div class='kpi'><b class='{cls}'>{u}</b><span>近30天盈亏（单位）</span></div></div>")


# ---------- 推荐卡片 ----------

def odds_panel(p: dict) -> str:
    team = p.get("pick_team") or p.get(p.get("pick_side", ""), "")
    mc = p.get("market_consensus") or {}
    pin = mc.get("pinnacle") or {}
    mv = p.get("bet365_movement") or {}
    if mc:
        cons = (f"{fl(mc.get('pick_line'))} @ {water(mc.get('pick_water'))}（{e(str(mc.get('source', '')))}，"
                f"{mc.get('bookmakers', NONE)} 家公司，{mc.get('agree', NONE)} 家一致）")
        gap = abs(float(p["line"]) - float(mc["pick_line"])) if mc.get("pick_line") is not None and p.get("line") is not None else None
        gap_s = NONE if gap is None else (f"{gap:g}（一致）" if gap == 0 else f"{gap:g}" + ("（>0.5 应撤下）" if gap > 0.5 else "（>0.25 降一档）" if gap > 0.25 else ""))
    else:
        cons, gap_s = NONE, NONE
    pin_s = f"{fl(pin.get('pick_line'))} @ {water(pin.get('pick_water'))}" if pin else NONE
    mv_s = (f"{fl(mv.get('open_pick_line'))} @ {water(mv.get('open_pick_water'))} → {fl(mv.get('pick_line'))} @ {water(mv.get('pick_water'))}"
            if mv else NONE)
    opn = f"{fl(p.get('line_open'))} → {fl(p.get('line'))}" if p.get("line_open") is not None else NONE
    return ("<div class='panel'><h3>盘口面板（推荐方视角，港盘水位）</h3><dl class='kv'>"
            f"<dt>我们的推荐</dt><dd>{e(str(team))} {fl(p.get('line'))} @ {water(p.get('odds'))}</dd>"
            f"<dt>初盘→推荐时</dt><dd>{opn}</dd>"
            f"<dt>多家共识中位盘</dt><dd>{cons}</dd>"
            f"<dt>与共识偏离</dt><dd>{gap_s}</dd>"
            f"<dt>Pinnacle</dt><dd>{pin_s}</dd>"
            f"<dt>Bet365 初→即</dt><dd>{mv_s}</dd></dl></div>")


def prematch_panel(p: dict, pol: dict) -> str:
    pm = records.prematch_of(p)
    if not pm:
        ko = parse_utc(p.get("kickoff_utc"))
        mins = ((settings().get("push_rules") or {}).get("pre_match_final_check") or {}).get("minutes_before_kickoff", 50)
        when = bjt(ko - timedelta(minutes=mins), "%m-%d %H:%M") if ko else NONE
        return f"<div class='panel'><h3>赛前确认</h3><p class='muted'>尚未确认 · 计划于开赛前 {mins} 分钟（约 {when}）复核</p></div>"
    extra = ""
    if pm.get("new_line") is not None or pm.get("new_odds") is not None:
        extra += f"<dt>调整为</dt><dd>{fl(pm.get('new_line'))} @ {water(pm.get('new_odds'))}</dd>"
    if pm.get("confidence"):
        extra += f"<dt>复核信心</dt><dd>{e(pm['confidence'])}</dd>"
    if pm.get("current_line"):
        extra += f"<dt>临场盘口</dt><dd>{e(str(pm['current_line']))}</dd>"
    srcs = "".join(f"<li><a href='{e(s.split(' ')[0])}' rel='noopener'>{e(s)}</a></li>" for s in pm.get("sources") or [])
    return ("<div class='panel'><h3>赛前确认</h3><dl class='kv'>"
            f"<dt>结论</dt><dd><b>{e(str(pm.get('decision')))}</b></dd><dt>时间</dt><dd>{bjt(pm.get('checked_at'))}</dd>{extra}"
            f"<dt>理由</dt><dd>{e(pm.get('reason') or NONE)}</dd></dl>"
            + (f"<ul class='src'>{srcs}</ul>" if srcs else "") + "</div>")


def result_panel(p: dict) -> str:
    res = p.get("result") or {}
    if not res.get("settlement"):
        return "<div class='panel'><h3>赛后回测</h3><p class='muted'>比赛结束后自动回测（比分、亚盘结算、复盘）</p></div>"
    note = "（推荐已撤销，作废，不计入命中率）" if res["settlement"] == "void" else ""
    return ("<div class='panel'><h3>赛后回测</h3><dl class='kv'>"
            f"<dt>比分</dt><dd>{e(res.get('score') or NONE)}</dd>"
            f"<dt>结算</dt><dd class='res-{e(res['settlement'])}'>{e(res.get('settlement_cn', ''))}{note}</dd>"
            f"<dt>盈亏</dt><dd>{units_html(res.get('units'))} 单位</dd>"
            f"<dt>复盘</dt><dd>{e(res.get('review') or '复盘待补充')}</dd>"
            f"<dt>回测时间</dt><dd>{bjt(res.get('settled_at'))}</dd></dl></div>")


def compare_table(p: dict) -> str:
    from . import publish_telegram as pt
    rows = "".join(f"<tr><td>{e(r[0])}</td><td class='num'>{e(r[1])}</td><td class='num'>{e(r[2])}</td></tr>"
                   for r in pt.comparison_rows(p))
    dv = pt.deviation(p)
    return ("<div class='panel cmp'><h3>机构对比（推荐方视角，港盘水位）</h3><div class='tablewrap'><table class='cmp-t'>"
            f"<thead><tr><th>机构</th><th>盘口</th><th>水位</th></tr></thead><tbody>{rows}</tbody></table></div>"
            + (f"<p class='dev'>📊 {dv}</p>" if dv else "") + "</div>")


def hit_chips(p: dict, stats: dict | None) -> str:
    from . import publish_telegram as pt
    conf = CONF.get(p.get("confidence", ""), p.get("confidence", ""))
    s = stats or {}
    lg = (s.get("by_league") or {}).get(p.get("league"))
    cf = (s.get("by_confidence") or {}).get(conf)
    return (f"<div class='chips'><span class='chip'>🏆 本联赛 {e(pt.record_of(lg))}</span>"
            f"<span class='chip'>🔥 {e(conf)}信心 {e(pt.record_of(cf))}</span></div>")


def pick_card(p: dict, pol: dict, stats: dict | None = None) -> str:
    from . import publish_telegram as pt
    from . import zh
    st = records.status(p)
    team = zh.team(str(p.get("pick_team") or p.get(p.get("pick_side", ""), "")))
    conf = CONF.get(p.get("confidence", ""), p.get("confidence", ""))
    odds = f" @{water(p.get('odds'))}" if p.get("odds") is not None else ""
    src = "".join(f"<a class='srcl' href='{e(u)}' rel='noopener'>{e(pt.link_label(u))}</a>"
                  for u in p.get("sources", []) if str(u).startswith("http"))
    detail = ("<div class='pick-body'><div>" + compare_table(p) + hit_chips(p, stats) +
              "<h3>✅ 为什么推</h3><ol class='reasons'>" + "".join(f"<li>{e(x)}</li>" for x in p.get("reasons", [])) + "</ol>"
              "<h3>⚠️ 风险在哪</h3><ul class='risks'>" + "".join(f"<li>{e(x)}</li>" for x in p.get("risks", [])) + "</ul>"
              f"<h3>🔗 数据来源</h3><div class='srcs'>{src or NONE}</div>"
              f"</div><div style='display:grid;gap:10px;align-content:start'>{prematch_panel(p, pol)}{result_panel(p)}</div></div>")
    return (f"<article class='card pick'><div class='meta'>🏆 {e(zh.league(str(p.get('league', ''))))} · 🕐 {bjt(p.get('kickoff_utc'))}"
            f"<span class='badge st-{st}'>{st}</span></div>"
            f"<div class='pick-head'><span class='teams'>{e(zh.team(str(p['home'])))} 对阵 {e(zh.team(str(p['away'])))}</span></div>"
            f"<div class='verdict'><span class='v-ico'>🎯</span><span class='v-main'>推荐 {e(str(team))} {fl(p.get('line'))}{odds}</span>"
            f"<span class='conf conf-{e(conf)}'>{pt.CONF_ICON.get(conf, '')} {e(conf)}信心</span></div>"
            + lock(detail) + "</article>")


def vip_inline(inner: str) -> str:
    """行内 VIP 内容（与 lock() 同一套 .vip / body.vip-on 机制）。"""
    return f"<span class='vip'>{inner}</span><span class='vip-lock-inline' role='button' tabindex='0'>🔒 VIP</span>"


def conf_counts(items: list[dict]) -> dict:
    out = {}
    for x in items:
        c = CONF.get(x.get("confidence") or "", "未定")
        out[c] = out.get(c, 0) + 1
    return out


def report_head(r: dict, pol: dict, n_live: int, n_shown: int) -> str:
    wl = r.get("watchlist") or []
    chips = []
    if r.get("screened") is not None:
        chips.append(f"<span class='hchip'>🔎 筛选 <b>{e(str(r['screened']))}</b> 场</span>")
    chips.append(f"<span class='hchip'>🎯 推荐 <b>{n_live}</b> 场</span>")
    if n_shown > n_live:
        chips.append(f"<span class='hchip'>↩️ 已撤销 <b>{n_shown - n_live}</b></span>")
    if wl or r.get("watched"):
        chips.append(f"<span class='hchip'>👀 观察 <b>{len(wl) or len(r.get('watched') or [])}</b> 场</span>")
    ed = " · 提前版" if r.get("edition") == "early" else ""
    stamp = f"<span class='freeze-stamp' title='赛前研究已冻结，非临场盘口'>{e(FREEZE_STAMP)}</span>"
    return (f"<div class='rhead'><div class='rhead-top'><div class='rdate'>📅 {e(r['date'])}<small>{ed}</small></div>{stamp}</div>"
            f"<div class='muted rgen'>生成于 {bjt(r.get('generated_at'))} · 盘口为港盘水位（十进制赔率 = 1 + 水位）</div>"
            f"<div class='hchips'>{''.join(chips)}</div></div>")


def no_pick_html(r: dict) -> str:
    """无推荐状态：按 watchlist 自动统计各信心档与不推类别（不使用手写的 no_pick_reason，避免数字不一致）。"""
    wl = r.get("watchlist") or []
    cc = conf_counts(wl)
    cnt = " · ".join(f"{c}信心 {cc[c]} 场" for c in ("高", "中", "低", "未定") if cc.get(c))
    cats = {}
    for x in wl:
        if x.get("withheld_category"):
            cats[x["withheld_category"]] = cats.get(x["withheld_category"], 0) + 1
    chips = "".join(f"<span class='cat'>{e(k)} {v}</span>" for k, v in sorted(cats.items(), key=lambda kv: -kv[1]))
    title = policy.no_pick_title()
    return (f"<div class='nopick'><div class='np-ico'>🛡️</div><div><div class='np-title'>{e(title)}</div>"
            "<div class='np-sub'>宁缺毋滥：没有达到发布标准的场次，今天不推。</div>"
            + (f"<div class='np-cnt'>观察名单 {len(wl)} 场：{cnt}</div>" if wl else "")
            + (f"<div class='np-cats'>{chips}</div>" if chips else "")
            + "</div></div>")


def watchlist_html(r: dict) -> str:
    wl = sorted(r.get("watchlist") or [], key=lambda x: (x.get("kickoff_utc") or "", x.get("league") or ""))
    if not wl:
        return ""
    rows = []
    for x in wl:
        hl, ho = x.get("ah_home_line"), x.get("ah_home_line_open")
        line = (f"{fl(ho)} → {fl(hl)}" if ho is not None and ho != hl else fl(hl)) if hl is not None else NONE
        cons = x.get("consensus") or {}
        cs = (f"{fl(cons.get('home_line'))}（{cons.get('agree')}/{cons.get('bookmakers')} 家）"
              if cons.get("home_line") is not None and cons.get("bookmakers") else "")
        conf = CONF.get(x.get("confidence") or "", "")
        ko = bjt(x.get("kickoff_utc"), "%m-%d %H:%M").replace(" 北京时间", "") if x.get("kickoff_utc") else NONE
        rows.append(
            f"<tr class='crow'><td data-label='开赛' data-v='{e(x.get('kickoff_utc') or '')}'>{e(ko)}</td>"
            f"<td data-label='对阵' class='m-full'>{e(x.get('home', ''))} vs {e(x.get('away', ''))}"
            f"<div class='muted sm'>{e(x.get('league', ''))}</div></td>"
            f"<td data-label='主队盘口' class='num'>{line}" + (f"<div class='muted sm'>共识 {cs}</div>" if cs else "") + "</td>"
            f"<td data-label='信心'>" + (f"<span class='conf conf-{e(conf)}'>{e(conf)}</span>" if conf else NONE) + "</td>"
            f"<td data-label='类别'><span class='cat'>{e(x.get('withheld_category') or NONE)}</span></td>"
            f"<td data-label='不推原因' class='wrap-text note'>{vip_inline(e(x.get('withheld_reason') or NONE))}</td></tr>")
    return ("<h2>👀 观察名单（不推荐）</h2>"
            "<p class='muted'>主队视角盘口，负数 = 主队让球；初盘 → 即时。</p>"
            + tbl(["开赛（北京时间）", "对阵", "主队盘口", "信心", "类别", "不推原因"], rows, cls="sortable cardify", tid="watch"))


def report_html(r: dict, pol: dict | None = None, stats: dict | None = None, after_head: str = "") -> str:
    # 首页只显示正式推荐；0 场时保留如实兜底（观察名单，不含预测结论）。
    # 观察但不推荐 / 已撤销 / 赛程事实清单 移入后台数据，不在首页混排。
    pol = pol or policy.load()
    live = policy.published(r, pol)
    shown = [p for p in records.published_picks(r, pol)]
    out = [report_head(r, pol, len(live), len(shown)), after_head]
    out.append("<p class='muted'>规则档位，未经独立回测；不构成统计预测准确率</p>")
    if not live:
        out.append(no_pick_html(r))
        out.append(watchlist_html(r))
    else:
        out.extend(pick_card(p, pol, stats) for p in live)
    return "".join(out)


def stats_brief(s: dict | None) -> str:
    """首页底部统计表：总体 / 分联赛 / 分信心命中率（VIP，与战绩页一致）。"""
    if not s:
        return ""
    def rows(d):
        return [f"<tr><td>{e(k)}</td><td class='num'>{v['picks']}</td><td class='num'>{v['decided']}</td>"
                f"<td class='num'>{rate_cell(v['hit_rate'], v['decided'], bucket=True, s=s)}</td>"
                f"<td class='num'>{units_html(v['units'])}</td></tr>"
                for k, v in d.items()]
    head = ["分组", "场次", "有效", "命中率", "盈亏"]
    body = ("<div class='grid3'>"
            + "<div><h3>总体</h3>" + tbl(head, rows({"全部": s.get("overall", {})}) if s.get("overall") else [], cls="", empty="暂无已结算推荐") + "</div>"
            + "<div><h3>分联赛</h3>" + tbl(head, rows(s.get("by_league", {})), cls="", empty="暂无已结算推荐") + "</div>"
            + "<div><h3>分信心</h3>" + tbl(head, rows(s.get("by_confidence", {})), cls="", empty="暂无已结算推荐") + "</div></div>")
    return (f"<h2>📊 战绩概览</h2><p class='muted'>{e(site_summary_line(s))} · 完整图表见 <a href='stats.html'>战绩页</a>。"
            f"分桶结论需 ≥{MIN_BUCKET_SAMPLE} 场，总体结论需 ≥{MIN_SAMPLE} 场。</p>"
            + lock(body))


# ---------- 推荐记录 ----------

def record_rows(reps: list[dict], pol: dict) -> list[dict]:
    out = []
    for r in reps:
        for p in records.published_picks(r, pol):
            line, odds, conf = records.effective(p)
            res = p.get("result") or {}
            out.append({"date": r["date"], "match_id": p.get("match_id"), "league": p.get("league", ""),
                        "home": p["home"], "away": p["away"], "kickoff_utc": p.get("kickoff_utc"),
                        "kickoff_bjt": bjt(p.get("kickoff_utc")), "pick_team": p.get("pick_team") or p.get(p.get("pick_side", ""), ""),
                        "pick_side": p.get("pick_side"), "line": p.get("line"), "odds": p.get("odds"),
                        "effective_line": line, "effective_odds": odds,
                        "confidence": CONF.get(p.get("confidence", ""), p.get("confidence")), "status": records.status(p),
                        "prematch": records.prematch_of(p), "result": res or None,
                        "market_consensus": p.get("market_consensus"), "bet365_movement": p.get("bet365_movement"),
                        "reasons": p.get("reasons", []), "risks": p.get("risks", []), "sources": p.get("sources", []),
                        "_pick": p})
    return out


def records_html(rows: list[dict], pol: dict) -> str:
    leagues = sorted({x["league"] for x in rows})
    opts = "".join(f"<option value='{e(l)}'>{e(l)}</option>" for l in leagues)
    res_opts = "".join(f"<option value='{v}'>{t}</option>" for v, t in
                       [("待确认", "待确认"), ("已确认", "已确认"), ("已撤销", "已撤销"), ("已结算", "已结算（全部）"),
                        ("win", "赢"), ("half_win", "赢半"), ("push", "走水"), ("half_loss", "输半"), ("loss", "输")])
    filters = ("<form id='filters' class='filters' onsubmit='return false'>"
               f"<label>联赛<select name='league'><option value=''>全部联赛</option>{opts}</select></label>"
               "<label>开始日期<input type='date' name='from'></label><label>结束日期<input type='date' name='to'></label>"
               f"<label>状态/结果<select name='result'><option value=''>全部</option>{res_opts}</select></label>"
               "<label>&nbsp;<button name='reset' type='button'>重置</button></label></form>")
    trs = []
    for i, x in enumerate(rows):
        res = x["result"] or {}
        rid = f"r{i}"
        trs.append(
            f"<tr class='rec' data-id='{rid}' data-league='{e(x['league'])}' data-date='{e(x['date'])}' data-status='{e(x['status'])}'"
            f" data-outcome='{e(res.get('settlement', ''))}' aria-expanded='false'>"
            f"<td data-label='日期' data-v='{e(x['date'])}'>{e(x['date'])}</td><td data-label='开赛' data-v='{e(x['kickoff_utc'] or '')}'>{e(x['kickoff_bjt'].replace(' 北京时间', ''))}</td>"
            f"<td data-label='联赛'>{e(x['league'])}</td><td data-label='对阵' class='m-full'>{e(x['home'])} vs {e(x['away'])}</td>"
            f"<td data-label='推荐'>{e(str(x['pick_team']))} {fl(x['effective_line'])} @ {water(x['effective_odds'])}</td>"
            f"<td data-label='信心'>{e(str(x['confidence']))}</td><td data-label='状态'><span class='badge st-{x['status']}'>{x['status']}</span></td>"
            f"<td data-label='比分'>{e(res.get('score') or NONE)}</td><td data-label='结算' class='res-{e(res.get('settlement', ''))}'>{e(res.get('settlement_cn') or NONE)}</td>"
            f"<td data-label='盈亏' data-v='{res.get('units', '')}'>{units_html(res.get('units'))}</td></tr>")
        p = x["_pick"]
        trail = (f"<div class='grid2'><div><h3>① 初次推荐（{e(x['date'])} 日报）</h3>"
                 f"<p>{e(str(x['pick_team']))} {fl(x['line'])} @ {water(x['odds'])} · 信心 {e(str(x['confidence']))}</p>"
                 "<b>理由</b><ol>" + "".join(f"<li>{e(t)}</li>" for t in x["reasons"]) + "</ol>"
                 "<b>风险</b><ul>" + "".join(f"<li>{e(t)}</li>" for t in x["risks"]) + "</ul>"
                 "<b>来源</b><ul class='src'>" + "".join(f"<li><a href='{e(s)}' rel='noopener'>{e(s)}</a></li>" for s in x["sources"]) + "</ul>"
                 f"</div><div style='display:grid;gap:10px;align-content:start'>{odds_panel(p)}"
                 f"{prematch_panel(p, pol)}{result_panel(p)}</div></div>")
        trs.append(f"<tr class='detail' id='d-{rid}' hidden><td colspan='10'>{trail}</td></tr>")
    head = ["日期", "开赛（北京时间）", "联赛", "对阵", "推荐", "信心", "状态", "比分", "结算", "盈亏"]
    body = (f"<p class='muted'>共 <b id='rec-count'>{len(rows)}</b> 条公开推荐（含赛前撤销）。点击任一行展开完整记录：初次推荐 → 赛前确认 → 赛果与复盘 → 数据来源。"
            f" 按日报告见 <a href='history.html'>历史日报</a>。</p>" + filters
            + tbl(head, trs, cls="sortable cardify", tid="records", empty="暂无推荐记录"))
    return lock(body)


# ---------- 战绩 ----------

def chart_data(s: dict) -> dict:
    series = s.get("series", [])
    labels, cum, h7, h30, notes = [], [], [], [], []
    for x in series:
        labels.append(x["date"])
        cum.append(x["cum_units"])
        w7, w30 = window(series, x["date"], 7), window(series, x["date"], 30)
        # 总体不足 30 场时，滚动命中率一律不画（app.js 已支持 null 断点），与复盘层一致。
        ok = site_can_quote(s)
        h7.append(w7["hit_rate"] if ok and quotable(w7["decided"]) else None)
        h30.append(w30["hit_rate"] if ok and quotable(w30["decided"]) else None)
        notes.append(f"{x['match']} {records.SETTLE_CN.get(x['outcome'], x['outcome'])} {x['units']:+.2f}")
    return {"labels": labels, "cum": cum, "hit7": h7, "hit30": h30, "notes": notes}


def stats_html(s: dict | None, today: str) -> str:
    if not s:
        return "<div class='empty'>暂无统计数据</div>"
    pub = f"<div class='card'><b>{e(site_summary_line(s))}</b><div class='muted'>统计更新于 {bjt(s.get('updated_at'))} · 结论口径见 <a href='review.html'>复盘页</a></div></div>"
    w7, w30 = window(s.get("series", []), today, 7), window(s.get("series", []), today, 30)

    def kpi(w, t):
        return (f"<div class='kpi'><b>{rate_cell(w['hit_rate'], w['decided'], s=s)}</b><span>{t}命中率"
                f"（{w['decided']}场，门槛 {MIN_SAMPLE}）</span></div>")

    def table(title, d):
        rows = [f"<tr><td>{e(k)}</td><td data-v='{v['picks']}'>{v['picks']}</td><td data-v='{v['decided']}'>{v['decided']}</td>"
                f"<td>{v['win']}/{v['half_win']}/{v.get('push', 0)}/{v['half_loss']}/{v['loss']}/{v.get('void', 0)}</td>"
                f"<td data-v='{v['hit_rate'] if site_can_quote(s) and quotable(v['decided'], True) else -1}'>"
                f"{rate_cell(v['hit_rate'], v['decided'], bucket=True, s=s)}</td>"
                f"<td data-v='{v['units']}'>{units_html(v['units'])}</td>"
                f"<td data-v='{v['roi'] if site_can_quote(s) and quotable(v['decided'], True) else -1}'>"
                f"{pct(v['roi']) if site_can_quote(s) and quotable(v['decided'], True) else MISSING}</td></tr>"
                for k, v in d.items()]
        return f"<div><h2>{title}</h2>" + tbl(["分组", "场次", "有效场", "赢/赢半/走/输半/输/取消", "命中率", "盈亏(单位)", "ROI"],
                                              rows, empty="暂无已结算推荐") + "</div>"
    has = bool(s.get("series"))
    cd = chart_data(s)
    enough = site_can_quote(s)
    charts = ("<div class='grid2'><div class='card'><h3>累计盈亏（单位，每注 1 单位 · 过程记录）</h3>"
              + ("<div class='chart' id='chart-units'></div>" if has else "<div class='empty'>暂无已结算推荐，结算后自动生成曲线</div>")
              + "</div><div class='card'><h3>滚动命中率（近7天 / 近30天）</h3>"
              + ("<div class='chart' id='chart-hit'></div>" if enough and has
                 else f"<div class='empty'>样本不足（门槛 {MIN_SAMPLE} 场），滚动命中率 {MISSING}，不构成结论</div>")
              + "</div></div>"
              "<script id='chart-data' type='application/json'>" + json.dumps(cd, ensure_ascii=False).replace("</", "<\\/") + "</script>")
    tables = ("<div class='grid2'>" + table("总体", {"全部": s["overall"]}) + table("按联赛", s.get("by_league", {}))
              + table("按信心等级", s.get("by_confidence", {})) + table("按盘口深度", s.get("by_depth", {}))
              + table("上盘 / 下盘", s.get("by_role", {})) + table("按具体盘口", s.get("by_line", {})) + "</div>")
    note = ("<p class='muted'>命中率 = (赢 + 0.5×赢半) / (场次 − 走水 − 取消)；盈亏按每注 1 单位、港盘水位计算；"
            f"ROI = 盈亏 / 有效场次。赛前撤销的推荐记为「取消」，不计入命中率。{e(REVIEW_DISCLAIMER)}；"
            f"总体结论需 ≥{MIN_SAMPLE} 场、分桶结论需 ≥{MIN_BUCKET_SAMPLE} 场，不足一律写「{MISSING}」，"
            "与复盘层口径一致。累计盈亏曲线仅作过程记录，不是准确率结论。</p>")
    return pub + lock(f"<div class='summary' style='grid-template-columns:1fr 1fr'>{kpi(w7, '近7天')}{kpi(w30, '近30天')}</div>"
                      + charts + tables + note)


# ---------- CLV / 模拟单（data/paper_trades.csv，回测审计员生成；字段见 docs/paper_trades.md） ----------

# 列名映射集中在这里：规范名 -> CSV 中可接受的列名（按顺序取第一个非空值；大小写/空白不敏感）。格式调整时只改这里。
PAPER_COLUMNS: dict[str, tuple[str, ...]] = {
    "date": ("date",), "match_id": ("match_id",), "league": ("league",), "home": ("home",), "away": ("away",),
    "kickoff_bj": ("kickoff_bj",), "kickoff_utc": ("kickoff_utc",),
    "status": ("status",), "confidence": ("confidence",),
    "pick": ("pick", "pick_team"), "line": ("line",),
    "odds_taken": ("odds_taken", "odds"), "odds_close": ("odds_close", "close_odds"), "odds_format": ("odds_format",),
    "line_close": ("line_close", "close_line"), "line_move": ("line_move",),
    "clv": ("clv",), "clv_pct": ("clv_pct",), "clv_line": ("clv_line",),
    "model_p": ("model_p",), "brier": ("brier",),
    "stake": ("stake",), "outcome": ("ah_outcome", "outcome", "settlement"), "score": ("result",),
    "pnl": ("pnl", "units"), "settled_at": ("settled_at",), "settled": ("settled",), "note": ("note",),
}
PAPER_NUM = ("line", "odds_taken", "odds_close", "line_close", "line_move", "clv", "clv_pct", "clv_line",
             "model_p", "brier", "stake", "pnl")
PAPER_RESULT = {  # 结算取值归一化 -> records.SETTLE_CN 的键
    "win": "win", "赢": "win", "half_win": "half_win", "halfwin": "half_win", "赢半": "half_win",
    "push": "push", "走": "push", "走水": "push", "half_loss": "half_loss", "halfloss": "half_loss", "输半": "half_loss",
    "loss": "loss", "lose": "loss", "输": "loss", "void": "void", "取消": "void",
}
PAPER_STATUS_CN = {"active": "有效", "cancelled": "已撤销", "void": "作废", "watch": "观察"}


def _num(x) -> float | None:
    try:
        return float(str(x).strip().replace("%", "")) if x is not None and str(x).strip() != "" else None
    except ValueError:
        return None


def load_paper_trades(path: Path) -> list[dict]:
    """宽容解析：文件不存在/为空/缺列/数值非法都不报错（缺的值为 None）。"""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        return []
    norm = {str(f).strip().lower(): f for f in reader.fieldnames if f}
    cols = {k: [norm[x.lower()] for x in alias if x.lower() in norm] for k, alias in PAPER_COLUMNS.items()}
    rows = []
    for raw in reader:
        r = {k: next((str(raw.get(c) or "").strip() for c in cs if str(raw.get(c) or "").strip()), "") for k, cs in cols.items()}
        if not any(r.values()):
            continue
        out = {k: (_num(v) if k in PAPER_NUM else (v or None)) for k, v in r.items()}
        out["outcome"] = PAPER_RESULT.get((r["outcome"] or "").lower().replace(" ", "_").replace("-", "_"))
        if out["clv"] is None and out["clv_pct"] is not None:
            out["clv"] = out["clv_pct"] / 100
        st = (r["status"] or "").lower()
        out["status"] = st if st in PAPER_STATUS_CN else ("active" if (out["stake"] or 0) > 0 else st or "active")
        if out["pnl"] is None and out["status"] == "active" and out["outcome"] and out["odds_taken"] is not None:
            k, o = out["stake"] or 0, out["odds_taken"]   # 港盘水位，与 settle.py 一致
            out["pnl"] = {"win": k * o, "half_win": k * o / 2, "push": 0.0, "void": 0.0, "half_loss": -k / 2, "loss": -k}[out["outcome"]]
        rows.append(out)
    return rows


def paper_summary(rows: list[dict]) -> dict:
    act = [r for r in rows if r["status"] == "active" and (r.get("stake") or 0) > 0]   # 只统计有效模拟单
    clvs = [r["clv"] for r in act if r.get("clv") is not None]
    settled = [r for r in act if r.get("outcome") and r["outcome"] != "void"]
    cnt = {k: sum(1 for r in settled if r["outcome"] == k) for k in ("win", "half_win", "push", "half_loss", "loss")}
    pnl = sum(r.get("pnl") or 0 for r in settled)
    staked = sum(r.get("stake") or 0 for r in settled)
    return {
        "clv_n": len(clvs), "clv_avg": (sum(clvs) / len(clvs)) if clvs else None,
        "clv_pos_share": (sum(1 for c in clvs if c > 0) / len(clvs)) if clvs else None,
        "line_moved": sum(1 for r in act if r.get("clv") is None and r.get("line_move") not in (None, 0)),
        "trades": len(act), "settled": len(settled), "pending": len(act) - len(settled),
        "won": cnt["win"] + cnt["half_win"], "pushed": cnt["push"], "lost": cnt["loss"] + cnt["half_loss"], **cnt,
        "pnl": pnl if settled else None, "staked": staked, "roi": (pnl / staked) if settled and staked else None,
        "cancelled": sum(1 for r in rows if r["status"] == "cancelled"), "void": sum(1 for r in rows if r["status"] == "void"),
        "watch": sum(1 for r in rows if r["status"] == "watch"),
    }


def _clv_cell(r: dict) -> str:
    if r.get("clv") is not None:
        return f"{r['clv'] * 100:+.2f}%"
    if r.get("line_move") not in (None, 0):
        return f"盘口变动 {fl(r['line_move'])}"
    return NONE


def _pick_cell(r: dict) -> str:
    if not r.get("pick"):
        return NONE
    return f"{e(r['pick'])} {fl(r.get('line'))} @ {water(r.get('odds_taken'))}"


def clv_html(rows: list[dict], sm: dict) -> str:
    note = ("<p class='muted'>CLV（收盘价值）= 下注赔率 ÷ 收盘去水公平赔率 − 1，只在收盘盘口与下注盘口相同时计算；"
            "盘口变动的场次显示「盘口变动」。长期为正说明下单时机优于市场收盘。</p>")
    moved = f"<p class='muted'>另有 {sm['line_moved']} 场收盘盘口已变动，暂不计算 CLV。</p>" if sm["line_moved"] else ""
    if not sm["clv_n"]:
        return f"<div class='empty'>{NONE}</div>" + moved + note
    avg = sm["clv_avg"]
    cls = "pos" if avg > 0 else "neg" if avg < 0 else ""
    return (f"<div class='summary'><div class='kpi'><b class='{cls}'>{avg * 100:+.2f}%</b><span>平均 CLV</span></div>"
            f"<div class='kpi'><b>{pct(sm['clv_pos_share'])}</b><span>正 CLV 占比</span></div>"
            f"<div class='kpi'><b>{sm['clv_n']}</b><span>样本数</span></div></div>" + moved + note)


def _trade_rows(rows: list[dict]) -> list[str]:
    out = []
    for r in rows:
        match = f"{r.get('home') or ''} vs {r.get('away') or ''}" if r.get("home") or r.get("away") else (r.get("match_id") or NONE)
        oc = r.get("outcome") or ""
        res = records.SETTLE_CN.get(oc, NONE) if oc else ("—" if r["status"] == "watch" else "待结算")
        ko = r.get("kickoff_bj") or ""
        ko_s = f"{ko[5:10]} {ko[11:16]}" if len(ko) >= 16 else (r.get("date") or NONE)
        out.append(
            f"<tr class='crow'><td data-label='开赛（北京时间）' data-v='{e(ko or r.get('date') or '')}'>{e(ko_s)}</td>"
            f"<td data-label='状态'><span class='badge ps-{e(r['status'])}'>{PAPER_STATUS_CN.get(r['status'], e(r['status']))}</span></td>"
            f"<td data-label='对阵' class='m-full'>{e(match)}<div class='muted sm'>{e(r.get('league') or '')}</div></td>"
            f"<td data-label='推荐'>{_pick_cell(r)}</td>"
            f"<td data-label='收盘'>{fl(r.get('line_close')) if r.get('line_close') is not None else NONE} @ {water(r.get('odds_close'))}</td>"
            f"<td data-label='CLV' data-v='{'' if r.get('clv') is None else r['clv']}'>{_clv_cell(r)}</td>"
            f"<td data-label='模型概率'>{pct(r.get('model_p'))}</td>"
            f"<td data-label='Brier'>{NONE if r.get('brier') is None else format(r['brier'], '.3f')}</td>"
            f"<td data-label='比分'>{e(r.get('score') or NONE)}</td>"
            f"<td data-label='结算' class='res-{e(oc)}'>{e(res)}</td>"
            f"<td data-label='盈亏' data-v='{'' if r.get('pnl') is None else r['pnl']}'>{units_html(r.get('pnl')) if r['status'] == 'active' else '不计'}</td>"
            f"<td data-label='说明' class='wrap-text note'>{e(r.get('note') or '')}</td></tr>")
    return out


TRADE_HEAD = ["开赛（北京时间）", "状态", "对阵", "推荐", "收盘", "CLV", "模型概率", "Brier", "比分", "结算", "盈亏", "说明"]


def paper_html(rows: list[dict], sm: dict, limit: int = 30) -> str:
    if not rows:
        return f"<div class='empty'>{NONE}</div>"
    roi, pnl = sm["roi"], sm["pnl"]
    kpis = (f"<div class='summary kpi4'><div class='kpi'><b>{sm['trades']}</b><span>有效模拟单（已结算 {sm['settled']}）</span></div>"
            f"<div class='kpi'><b>{sm['won']}/{sm['pushed']}/{sm['lost']}</b><span>胜/走/负</span></div>"
            f"<div class='kpi'><b>{units_html(pnl) if pnl is not None else NONE}</b><span>盈亏（单位）</span></div>"
            f"<div class='kpi'><b class='{'pos' if roi and roi > 0 else 'neg' if roi and roi < 0 else ''}'>{pct(roi)}</b><span>ROI</span></div></div>"
            f"<p class='muted'>已撤销 {sm['cancelled']} 笔 · 作废 {sm['void']} 笔（均不计入盈亏）· 观察 {sm['watch']} 场（只计数）。</p>")

    def key(r):
        return (r.get("kickoff_bj") or r.get("date") or "", r.get("match_id") or "")
    main_rows = sorted([r for r in rows if r["status"] != "watch"], key=key, reverse=True)
    watch = sorted([r for r in rows if r["status"] == "watch"], key=key, reverse=True)
    shown = main_rows[:limit]
    more = f"<p class='muted'>仅显示最近 {limit} 笔，共 {len(main_rows)} 笔。</p>" if len(main_rows) > limit else ""
    body = kpis + "<h3>模拟单与撤销/作废</h3>" + tbl(TRADE_HEAD, _trade_rows(shown), cls="sortable cardify", tid="paper", empty=NONE) + more
    if watch:
        body += (f"<details class='watch'><summary>观察场次 {len(watch)} 场（不下注、不计盈亏，点击展开）</summary>"
                 + tbl(TRADE_HEAD, _trade_rows(watch[:100]), cls="cardify", tid="paper-watch", empty=NONE) + "</details>")
    note = ("<p class='muted'>模拟单为回测审计员按规则记录的纸面下单（每注 1 单位，不涉及真实资金），只统计状态「有效」且注码 &gt; 0 的单；"
            "胜 = 赢 + 赢半，负 = 输 + 输半；ROI = 盈亏 / 已结算注码。</p>")
    return body + note


def paper_section(rows: list[dict]) -> str:
    sm = paper_summary(rows)
    return ("<section id='clv' class='paper'><h2>CLV（收盘价值）</h2>" + lock(clv_html(rows, sm)) + "</section>"
            "<section id='paper-trades' class='paper'><h2>模拟单</h2>" + lock(paper_html(rows, sm)) + "</section>"
            f"<p class='disc-inline'>{e(policy.DISCLAIMER)} —— 先知</p>")


# ---------- 模型实验场（experiment track，不计入正式命中率） ----------

LAB_BANNER = ("<div class='labbanner'><b>🧪 实验，非正式推荐，不计入正式命中率</b>"
              "<span>各模型独立出场次、单独记账（1 单位平注），只用于检验模型；正式推荐只看首页达到发布门槛的亚盘。"
              "战绩直接读 experiment_stats.json（按平均 CLV 排序），不在前端重算。</span></div>")
DEMO_BANNER = "<div class='demobanner'>⚠️ 示例数据 · 仅用于版式预览，不是真实推荐</div>"


def _lab_rows(rows: list[dict]) -> list[str]:
    out = []
    for r in sorted(rows, key=lambda r: (r.get("kickoff_bj") or r.get("kickoff_utc") or "", r["model_id"])):
        ko = r.get("kickoff_bj") or ""
        ko_s = f"{ko[5:10]} {ko[11:16]}" if len(ko) >= 16 else (bjt(r.get("kickoff_utc"), "%m-%d %H:%M") if r.get("kickoff_utc") else NONE)
        res = records.SETTLE_CN.get(r["outcome"], NONE) if r.get("outcome") else "待结算"
        ver = f"<div class='muted sm'>{e(r['model_version'])}</div>" if r.get("model_version") else ""
        tags = model_lab.overlap_tags(r.get("overlap") or "")
        ov = ("<div class='muted sm'>" + " · ".join(e(x) for x in tags) + "</div>") if tags else ""
        out.append(
            f"<tr class='crow'><td data-label='对阵' class='m-full'>{e(r['home'] or NONE)} vs {e(r['away'] or NONE)}"
            + (f"<div class='muted sm'>{e(r['league'])}</div>" if r.get("league") else "") + ov + "</td>"
            f"<td data-label='模型'><code>{e(r['model_id'])}</code>{ver}</td>"
            f"<td data-label='选项'><b>{e(model_lab.selection_text(r))}</b></td>"
            f"<td data-label='盘口' class='num'>{e(model_lab.line_text(r) or ('—' if r['market'] == '1x2' else NONE))}</td>"
            f"<td data-label='水位' class='num'>{e(model_lab.odds_text(r) or NONE)}</td>"
            f"<td data-label='开赛'>{e(ko_s)}</td>"
            f"<td data-label='模型概率'>{vip_inline(pct(r.get('model_p')))}</td>"
            f"<td data-label='结果' class='res-{e(r.get('outcome') or '')}'>{e(res)}</td></tr>")
    return out


LAB_HEAD = ["对阵", "模型", "选项", "盘口", "水位", "开赛（北京时间）", "模型概率", "结果"]


def lab_board_html(board: list[dict]) -> str:
    rows = []
    for a in board:
        settled = a.get("settled")
        picks = a.get("picks")
        clv = a.get("clv_mean")
        clv_n = a.get("clv_n") or 0
        n_cell = model_lab.n_label(settled if settled is not None else picks)
        if settled is not None and picks is not None:
            n_cell = f"{n_cell}<span class='muted sm'> / {picks}</span>"
        cov = model_lab.coverage_text(a)
        rows.append(
            f"<tr><td><code>{e(a['model_id'])}</code></td><td>{model_lab.MARKET_CN.get(a['market'], a['market'])}</td>"
            f"<td data-v='{settled if settled is not None else ''}'>{n_cell}</td>"
            f"<td data-v='{'' if a.get('hit_rate') is None else a['hit_rate']}'>{pct(a.get('hit_rate'))}</td>"
            f"<td data-v='{'' if a.get('roi') is None else a['roi']}' class="
            f"'{'pos' if (a.get('roi') or 0) > 0 else 'neg' if (a.get('roi') or 0) < 0 else ''}'>"
            f"{model_lab.pct(a.get('roi'), True)}</td>"
            f"<td data-v='{'' if clv is None else clv}'>{model_lab.pct(clv, True)}"
            + (f"<span class='muted sm'>（{clv_n}）</span>" if clv_n else "") + "</td>"
            f"<td data-v='{'' if a.get('coverage') is None else a['coverage']}'>{cov}</td></tr>")
    return tbl(["模型", "玩法", "场数", "命中率", "ROI", "CLV", "收盘覆盖"], rows, cls="sortable", tid="labboard", empty=NONE)


def model_lab_html(lab, day: str, demo: bool = False) -> str:
    lab = model_lab.as_lab(lab)
    rows = lab.rows
    today = model_lab.for_date(rows, day)
    groups = model_lab.by_market(today)
    head = (DEMO_BANNER if demo or lab.sample else "") + LAB_BANNER
    n_set = sum(1 for r in rows if r.get("outcome"))
    chips = (f"<div class='hchips labchips'><span class='hchip'>📅 {e(day)}</span><span class='hchip'>🧪 今日 <b>{len(today)}</b> 场</span>"
             + "".join(f"<span class='hchip'>{model_lab.MARKET_CN[m]} <b>{len(rs)}</b></span>" for m, rs in groups)
             + f"<span class='hchip'>📒 累计 <b>{len(rows)}</b> · 已结算 <b>{n_set}</b></span></div>")
    skip = model_lab.skipped_text(lab.skipped)
    if skip:
        chips += f"<p class='muted sm'>{e(skip)}</p>"
    body = head + chips + "<h2>📋 今日实验盘</h2>"
    if not groups:
        body += f"<div class='empty'>{NONE}（今日各模型均未出场次）</div>"
    for m, rs in groups:
        body += (f"<h3>{model_lab.MARKET_CN[m]} · {len(rs)} 场</h3>"
                 + tbl(LAB_HEAD, _lab_rows(rs), cls="sortable cardify labtbl", tid=f"lab-{m}", empty=NONE))
    board = model_lab.board(lab.stats)
    asof = model_lab.stats_time(lab.stats)
    body += ("<h2>📊 模型 × 玩法 战绩</h2>"
             + (f"<p class='muted sm'>统计截至 {e(asof)} 北京时间 · 按平均 CLV 排序</p>" if asof else "")
             + (lock(lab_board_html(board)) if board else f"<div class='empty'>{NONE}（暂无历史记录）</div>")
             + "<p class='muted'>命中率 = (赢 + 0.5×赢半) / (已结算 − 走 − 作废)；ROI = 盈亏 / 同一分母（1 单位平注，报告时刻赔率）；"
               "CLV 为有收盘价场次的均值（括号内为场数），按 CLV 排序、空值排最后；"
               "收盘覆盖优先读 stats 字段，没有则显示「暂无」。实验盘不计入正式命中率。</p>")
    return body + f"<p class='disc-inline'>{e(policy.DISCLAIMER)} —— 先知</p>"


# ---------- 数据源 ----------

def sources_html(st: dict) -> str:
    cards = []
    for k, meta in source_status.SOURCES.items():
        s = (st.get("sources") or {}).get(k, {})
        ok_t, err_t = parse_utc(s.get("last_ok_at")), parse_utc(s.get("last_error_at"))
        if not s:
            dot, label = "none", "暂无运行记录"
        elif err_t and (not ok_t or err_t > ok_t):
            dot, label = "err", "最近一次请求出错"
        else:
            dot, label = ("ok", "正常") if ok_t else ("none", "有调用记录（成功与否未记录）")
        q = s.get("quota") or {}
        if meta["limit"] is None:
            qs = "无额度限制（公开页面数据）"
        elif q.get("remaining") is not None:
            qs = f"剩余 {q['remaining']} / {q.get('limit', meta['limit'])}（每{q.get('period', meta['period'])}）· 记录于 {bjt(q.get('as_of'))}"
        else:
            qs = f"上限 {meta['limit']} 次/{meta['period']} · 剩余暂无记录"
        err = (f"{e(s['last_error'])}（{bjt(s.get('last_error_at'))}）" if s.get("last_error") else "无")
        credit = f"<p class='muted'><b>{CREDIT}</b></p>" if k == "fivedollar" else ""
        cards.append(f"<div class='card src-card'><h3><span class='dot {dot}'></span>{e(meta['name'])} "
                     f"<small>· {label}</small></h3><div class='muted' style='font-size:13px'>{e(meta['role'])}</div>"
                     f"<dl class='kv' style='margin-top:8px'><dt>最近成功</dt><dd>{bjt(s.get('last_ok_at'))}</dd>"
                     f"<dt>最近调用</dt><dd>{bjt(s.get('last_call_at'))}</dd>"
                     f"<dt>最近错误</dt><dd>{err}</dd><dt>额度</dt><dd>{qs}</dd>"
                     f"<dt>官网</dt><dd><a href='{e(meta['url'])}' rel='noopener'>{e(meta['url'])}</a></dd></dl>{credit}</div>")
    upd = f"状态更新于 {bjt(st.get('updated_at'))}。" if st.get("updated_at") else "暂无状态记录。"
    return (f"<p class='muted'>{upd}状态由先知在本机运行数据抓取时自动记录（不含任何密钥）；额度为最近一次调用时的记录值。</p>"
            f"<div class='cards2'>{''.join(cards)}</div>")


# ---------- 规则 ----------

def rules_html(pol: dict, cfg: dict) -> str:
    pr = cfg.get("push_rules") or {}
    dr = (pr.get("daily_report") or {}).get("time", "10:00")
    pm = (pr.get("pre_match_final_check") or {}).get("minutes_before_kickoff", 50)
    bt = (pr.get("post_match_backtest") or {}).get("minutes_after_kickoff", 135)
    tol = float(pol.get("alignment_tolerance", 0.25))
    items = [
        ("命中率优先", "宁可少推，不推不确定的场次。"),
        (policy.publish_rule_title(pol), e(policy.publish_rule_desc(pol))),
        ("每日上限", f"每天最多 {pol['max_picks_per_day']} 场（积累期目标 5–10 场）。"),
        ("深盘", f"|线|≥{float(pol.get('max_abs_line_to_publish', 1.75)):g} 默认进观察名单，不进正式推荐。"
         if pol.get("max_abs_line_to_publish") is not None else "—"),
        ("必须有真实盘口与水位", "没有真实赔率的比赛一律不推荐，绝不编造数据。" if pol.get("require_real_odds") else "—"),
        ("与市场共识对齐", f"推荐盘口与多家公司中位盘相差超过 {tol:g} 降一档信心；超过 {2 * tol:g} 直接撤下。"),
        ("赛前最终确认", f"开赛前 {pm} 分钟复核临场盘口、水位、首发与伤停；信心下降即撤销推荐（结论：保留 / 调整 / 撤销）。"),
        ("赛后回测", f"开赛后约 {bt} 分钟回填 90 分钟比分，亚盘结算并写复盘。"),
        ("命中率公式", "命中率 = (赢 + 0.5×赢半) / (场次 − 走水 − 取消)。赛前撤销记为取消，不计入。"),
        ("资金单位", "每注固定 1 单位；盈亏按港盘水位计算（赢 = +水位，赢半 = +水位/2，输半 = −0.5，输 = −1）。"),
        ("推送时间", f"每日 {e(str(dr))}（北京时间）日报；赛前 {pm} 分钟确认；赛后回测。"),
        ("生效时间", f"本规则自 {e(str(pol.get('effective_from', '')))} 起的报告生效；此前报告保留原样作为历史记录。"),
    ]
    return ("<div class='card'><dl class='kv'>" + "".join(f"<dt>{t}</dt><dd>{d}</dd>" for t, d in items) + "</dl></div>"
            f"<div class='card'><b>{e(policy.DISCLAIMER)}</b><p class='muted'>先知的所有内容均为基于公开数据的分析参考，不保证结果。</p></div>"
            "<p class='muted'>规则来源：仓库 config/settings.yaml 中的 policy 配置。</p>")


# ---------- 构建 ----------

def build(site: Path = SITE, reports: Path = REPORTS, data: Path = DATA, now: datetime | None = None,
          lab: Path | None = None, lab_demo: bool = False,
          review_docs: Path | None = None) -> list[str]:
    pol = policy.load()
    cfg = settings()
    vip = str(cfg.get("vip_password_sha256") or "")
    gen = bjt(now or datetime.now(timezone.utc))
    today = bjt_today(now)
    (site / "reports").mkdir(parents=True, exist_ok=True)
    (site / "assets").mkdir(parents=True, exist_ok=True)
    (site / "data").mkdir(parents=True, exist_ok=True)
    for f in ("app.css", "app.js"):
        shutil.copy(TEMPLATES / "site" / f, site / "assets" / f)
    reps = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(reports.glob("*/*/*.json"))]
    sp = data / "stats.json"
    s = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else None
    if s is not None and "series" not in s and (data / "predictions.csv").exists():  # 旧 stats.json 兼容
        s = compute(list(csv.DictReader((data / "predictions.csv").open(encoding="utf-8"))))
    st = source_status.load_latest(data / "source_status.json")

    def write(name, title, body, cur="", base=""):
        (site / name).write_text(page(title, body, cur, base, gen, vip), encoding="utf-8")

    # 选场漏斗：优先用定时班次落盘的事实文件；没有就即时从本次 reports 自算（不写仓库）。
    funnel_obj = _load_json(data / "selection_funnel.json")
    funnel_source = "data/selection_funnel.json（定时班次落盘）"
    if funnel_obj is None:
        funnel_obj = funnel_mod.build(reports=reports)
        funnel_source = "reports/（站点即时自算，未落盘）"
    (site / "data" / "selection_funnel.json").write_text(
        json.dumps(funnel_obj, ensure_ascii=False, indent=1), encoding="utf-8")
    write("funnel.html", "选场漏斗", funnel_html(funnel_obj, funnel_source), "review.html")

    # 复盘产物：读得到就展示 + 导出，读不到写「暂无」，绝不补造。
    acc = _load_json(data / "accuracy_review.json")
    facts = {name: _load_json(data / name) for name, _ in REVIEW_FACTS}
    eval_obj = facts.get("eval_facts.json")
    clv_obj = facts.get("clv_join_facts.json")
    exported = []
    for name, _ in REVIEW_FACTS:
        obj = facts.get(name)
        if obj is not None:
            (site / "data" / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
            exported.append(name)
    (site / "review").mkdir(parents=True, exist_ok=True)
    doc_links = []
    rd = review_docs or (ROOT / "docs" / "review")
    if rd.exists():
        for f in sorted(rd.glob("*.md"), reverse=True):
            write(f"review/{f.stem}.html", f"复盘 {f.stem}",
                  md_html(f.read_text(encoding="utf-8")), "review.html", "../")
            doc_links.append((f.stem, f.name))
    write("review.html", "复盘", review_html(acc, facts, doc_links, exported, eval_obj, clv_obj), "review.html")

    latest = reps[-1] if reps else None
    n_today = len(policy.published(latest, pol)) if latest else None
    lbl = ("今日推荐" if latest and latest["date"] == today else f"最新推荐（{latest['date'][5:]}）") if latest else "今日推荐"
    bar = summary_bar(n_today, lbl, s, today)
    for r in reps:
        write(f"reports/{r['date']}.html", f"亚盘日报 {r['date']}", report_html(r, pol, s), "", "../")
    write("index.html", "今日推荐", (report_html(latest, pol, s, after_head=bar) if latest else bar + "<div class='empty'>暂无报告</div>")
          + stats_brief(s), "index.html")
    rows = record_rows(reps, pol)
    write("records.html", "推荐记录", records_html(list(reversed(rows)), pol), "records.html")
    paper = load_paper_trades(data / "paper_trades.csv")
    write("stats.html", "战绩", bar + stats_html(s, today) + paper_section(paper), "stats.html")
    write("sources.html", "数据源状态", sources_html(st), "sources.html")
    write("rules.html", "规则说明", rules_html(pol, cfg), "rules.html")
    lab_obj = model_lab.load(lab or data / "experiment_picks.csv",
                             None if lab is None else model_lab.resolve_stats_path(lab))
    # 若 --lab 指向样例 CSV，仍自动找同目录 stats；默认走 data/experiment_*. 
    if lab is not None and lab_obj.stats is None:
        lab_obj = model_lab.load(lab, model_lab.resolve_stats_path(lab))
    demo = lab_demo or lab_obj.sample
    write("model-lab.html", "模型实验场", model_lab_html(lab_obj, latest["date"] if latest else today, demo), "model-lab.html")
    hist = [f"<tr><td><a href='reports/{r['date']}.html'>{r['date']}</a></td><td>{len(policy.published(r, pol))}</td>"
            f"<td>{len(records.published_picks(r, pol))}</td></tr>" for r in reversed(reps)]
    write("history.html", "历史日报", tbl(["日期", "当前有效推荐", "公开推荐（含撤销）"], hist, empty="暂无报告"), "")
    pub_rows = [{k: v for k, v in x.items() if k != "_pick"} for x in rows]
    (site / "data" / "records.json").write_text(json.dumps(pub_rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (site / "data" / "stats.json").write_text(json.dumps(s or {}, ensure_ascii=False, indent=1), encoding="utf-8")
    (site / "data" / "source_status.json").write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    (site / ".nojekyll").write_text("")
    return [r["date"] for r in reps]



# ---------- 复盘页（只展示仓库里真实落盘的事实） ----------

def _load_json(path: Path):
    """读不到 / 解析失败一律返回 None：页面写「暂无」，绝不补造字段。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _inline_md(x: str) -> str:
    s = e(str(x))
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    return s



# ---------- 选场漏斗页（为什么没推 / 为什么 0 场） ----------

def funnel_html(obj: dict, source: str) -> str:
    """只渲染 funnel.build() 已算好的事实；缺值显示「未取得」，不推断。"""
    s = obj.get("summary") or {}
    days = obj.get("days") or []
    labels = obj.get("bucket_labels") or {}
    b = s.get("buckets") or {}

    def v(x):
        return MISSING if x is None or x == MISSING else e(str(x))

    head = ("<div class='card'><b>🔎 选场漏斗 · 只读事实</b>"
            f"<div class='muted'>{e(obj.get('note') or '')}</div>"
            f"<div class='muted'>数据来源：{e(source)} · 生成于 {bjt(obj.get('checked_at'))} · "
            f"报告文件 {v(obj.get('report_files'))} 份</div></div>")
    outside = s.get("max_outside_screen", MISSING)
    kpis = ("<div class='summary'>"
            f"<div class='kpi'><b>{v(s.get('total_picks'))}</b><span>累计发布场次（{v(s.get('days'))} 天）</span></div>"
            f"<div class='kpi'><b>{v(b.get('data_gap'))}</b><span>{e(labels.get('data_gap', '数据缺口'))}（未推）</span></div>"
            f"<div class='kpi'><b>{v(b.get('model_judgment'))}</b><span>{e(labels.get('model_judgment', '模型判断'))}（未推）</span></div>"
            f"<div class='kpi'><b>{v(outside)}</b><span>单日最多未进候选</span></div></div>")

    rows = []
    for d in days:
        up = d.get("upstream") or {}
        bk = d.get("buckets") or {}
        mark = " ⚠️ 0 场" if d.get("zero_pick_day") else ""
        rows.append(
            "<tr>" + "".join(f"<td class='num'>{x}</td>" for x in (
                e(str(d.get('date'))) + mark,
                v(d.get('picks')), v(d.get('watchlist')),
                v(bk.get('data_gap')), v(bk.get('verification')), v(bk.get('model_judgment')),
                v(bk.get('timing')), v(bk.get('unknown')),
                v(up.get('window_fixtures')), v(up.get('screening_cap')), v(up.get('outside_screen')),
                v(d.get('collection_errors')))) + "</tr>")
    day_tbl = ("<h2>📅 按日漏斗</h2>"
               "<p class='muted'>「未进候选」＝ 窗口总数 − 筛查上限，只从报告 not_covered 文本取数；"
               "老报告没记这两个数字就写「未取得」，不拿配置倒推。</p>"
               + tbl(["日期", "发布", "观察", "数据缺口", "盘口核验", "模型判断", "时间窗口", "未分类",
                      "窗口总数", "筛查上限", "未进候选", "采集错误"], rows, cls="sortable cardify",
                     tid="funnel", empty="暂无报告"))

    obs = s.get("observations") or []
    obs_html = ("<h2>🧭 跨天事实</h2>" +
                ("<ul>" + "".join(f"<li>{e(str(x))}</li>" for x in obs) + "</ul>"
                 if obs else "<div class='empty'>暂无跨天事实</div>"))

    gl = s.get("data_gap_by_league") or {}
    gl_rows = [f"<tr><td>{e(k)}</td><td class='num'>{v(n)}</td></tr>" for k, n in gl.items()]
    gap_html = ("<h2>🏆 数据缺口集中在哪些联赛</h2>"
                "<p class='muted'>只统计被判为「数据缺口」的观察条目，按联赛累计；不推断原因。</p>"
                + tbl(["联赛", "场数"], gl_rows, empty="暂无"))

    details = []
    for d in days:
        notes = d.get("collection_notes") or []
        vf = d.get("verification_facts") or []
        cats = d.get("categories") or {}
        items = "".join(f"<li>{e(c)} · <b>{v(n)}</b> 场</li>" for c, n in cats.items())
        facts = "".join(f"<li>{e(str(x))}</li>" for x in vf)
        raw = "".join(f"<li>{e(str(x))}</li>" for x in notes)
        details.append(
            f"<div class='card'><h3>{e(str(d.get('date')))} · 发布 {v(d.get('picks'))} 场</h3>"
            f"<details><summary>淘汰类别 / 盘口核验 / 采集事实原文（逐字）</summary>"
            f"<h4>淘汰类别</h4><ul>{items or '<li>暂无</li>'}</ul>"
            f"<h4>盘口核验明细</h4><ul>{facts or '<li>暂无</li>'}</ul>"
            f"<h4>collection_notes 原文</h4><ul>{raw or '<li>暂无</li>'}</ul>"
            f"<h4>not_covered 原文</h4><p>{e(str(d.get('not_covered_text') or MISSING))}</p>"
            "</details></div>")
    detail_html = "<h2>🔍 逐日下钻</h2><div class='grid2'>" + "".join(details) + "</div>"

    alerts = s.get("alerts") or []
    unver = obj.get("unverified") or []
    clv_note = ("<div class='card'><h3>💹 收盘快照 join 与价格型 CLV</h3>"
                "<p class='muted'>本页解释「为什么没推」；收盘快照与台账的 join 结果、"
                "价格型 CLV 为什么按口径仍是「未取得」，见 "
                "<a href='review.html'>复盘页 · 收盘快照 join 事实</a>。</p></div>")
    tail = (clv_note + "<h2>⚠️ 仍未验证</h2><ul>" + "".join(f"<li>{e(str(x))}</li>" for x in unver) + "</ul>"
            f"<p class='muted'>事实标签：{', '.join(e(str(a)) for a in alerts) or '无'} · "
            "本页不改评分、不改阈值、不发送任何消息。</p>")
    return head + kpis + day_tbl + obs_html + gap_html + detail_html + tail


def md_html(md: str) -> str:
    """复盘 Markdown -> 轻量 HTML（标题/表格/列表/引用/段落）。

    只做结构转换，不改一个字；外链、图片等复杂语法按纯文本转义输出。"""
    lines = md.splitlines()
    out, i = [], 0
    while i < len(lines):
        l = lines[i].rstrip()
        if not l.strip():
            i += 1
            continue
        if l.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|\-]+\|$", lines[i + 1].strip()):
            head = [_inline_md(c.strip()) for c in l.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = [_inline_md(c.strip()) for c in lines[i].strip().strip("|").split("|")]
                rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
                i += 1
            out.append(tbl(head, rows, cls="", empty="暂无"))
            continue
        if l.lstrip().startswith("#"):
            k = min(len(l) - len(l.lstrip("#")) + 1, 5)
            out.append(f"<h{k}>{_inline_md(l.lstrip('# ').strip())}</h{k}>")
            i += 1
            continue
        if l.lstrip().startswith("> "):
            out.append(f"<blockquote>{_inline_md(l.lstrip()[2:])}</blockquote>")
            i += 1
            continue
        if re.match(r"^\s*[-\*] ", l):
            items = []
            while i < len(lines) and re.match(r"^\s*[-\*] ", lines[i]):
                txt = re.sub(r"^\s*[-\*] ", "", lines[i])
                items.append(f"<li>{_inline_md(txt)}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        out.append(f"<p>{_inline_md(l.strip())}</p>")
        i += 1
    return "".join(out)


def _fact_status(obj) -> str:
    if not isinstance(obj, dict):
        return "暂无"
    ev = obj.get("evaluation") or {}
    wf = (obj.get("walk_forward") or {}).get("overall") or {}
    pq = obj.get("prediction_quality") or {}
    ms = obj.get("model_scope") or {}
    for v in (obj.get("sample_status"), obj.get("status"), ev.get("status"),
              wf.get("status"), pq.get("status"), ms.get("status"), obj.get("current_mode")):
        if v:
            return str(v)
    # 累计型事实文件把状态放在最新快照里；缺口型放在 alert 里。
    # 有文件但确实没有状态字段时写「事实文件（无状态字段）」，不写成「暂无」以免误读为未落盘。
    snaps = obj.get("snapshots") or []
    if snaps and isinstance(snaps[-1], dict):
        v = snaps[-1].get("sample_status") or snaps[-1].get("reconciliation")
        if v:
            return str(v)
    alert = obj.get("alert")
    if alert:
        return str(alert).split()[0]
    return "事实文件（无状态字段）"



# ---------- 评估边界 / 联赛事实 / 告警 / CLV join ----------

def clv_join_html(obj) -> str:
    """只展示真实 join 出来的事实；价格型 CLV 为什么仍是「未取得」要写明。"""
    if not obj:
        return "<h2>💹 收盘快照 join 事实</h2><div class='empty'>暂无（待 CLV join 步骤产出）</div>"
    rows = [f"<tr><td>{e(k)}</td><td class='num'>{v}</td></tr>"
            for k, v in (obj.get("price_clv_status_by_row") or {}).items()]
    mids = obj.get("joined_match_ids") or []
    return ("<h2>💹 收盘快照 join 事实</h2>"
            "<div class='summary'>"
            f"<div class='kpi'><b>{e(str(obj.get('close_snapshot_rows_in_repo', MISSING)))}</b><span>仓库内 close 行数</span></div>"
            f"<div class='kpi'><b>{e(str(obj.get('close_snapshot_unique_matches', MISSING)))}</b><span>覆盖唯一场次</span></div>"
            f"<div class='kpi'><b>{e(str(obj.get('joined_unique_match_ids', MISSING)))}</b><span>与台账 join 上的场次</span></div></div>"
            + "<p class='muted'>行数 ≠ 可 join 的场次：只有 match_id 对得上、且视角能唯一学习，"
              "才写 close_line / line_move。价格型 CLV 严格按仓库口径，线差绝不冒充。</p>"
            + tbl(["逐行 CLV 状态（24 行台账）", "行数"], rows, empty="暂无")
            + (f"<p class='muted'>join 上的 match_id：{', '.join(e(str(x)) for x in mids)}</p>" if mids else "")
            + f"<p class='muted'>{e(str(obj.get('note') or ''))}</p>")


def eval_boundary_html(obj) -> str:
    """研究层≠线上层、按联赛事实分桶、结构化告警（notify_only）。"""
    if not obj:
        return "<h2>🧭 评估边界与告警</h2><div class='empty'>暂无（待 eval-facts 步骤产出）</div>"
    ms = obj.get("model_scope") or {}
    lf = obj.get("league_facts") or {}
    facts = "".join(f"<li>{e(str(x))}</li>" for x in (ms.get("research_facts") or []))
    boundary = ("<div class='card'><h3>⚠️ 研究层 ≠ 线上发布层</h3>"
                f"<p>{e(str(ms.get('boundary') or ''))}</p>"
                f"<p class='muted'>{e(str(ms.get('live_publication_note') or ''))}</p>"
                f"<ul>{facts or '<li>暂无研究层事实</li>'}</ul></div>")
    lrows = [f"<tr><td>{e(k)}</td><td class='num'>{v['settled_rows']}</td>"
             f"<td class='num'>{v['decided']}</td><td class='num'>{v['data_gap_withheld']}</td>"
             f"<td class='num'>{e(str(v['hit_rate_conclusion']))}</td>"
             f"<td>{e(str(v['sample_status']))}</td></tr>"
             for k, v in (lf.get("by_league") or {}).items()]
    leagues = ("<h3>🏆 按联赛事实分桶</h3>"
               f"<p class='muted'>总体结论门槛 {e(str(lf.get('min_sample', MISSING)))} 场；"
               f"连续数据缺口 {e(str(lf.get('consecutive_data_gap_days', MISSING)))} 天（只计数，不称漂移）。"
               f"{e(str(lf.get('note') or ''))}</p>"
               + tbl(["联赛", "已结算", "有效", "数据缺口", "命中率结论", "样本状态"], lrows, empty="暂无"))
    arows = [f"<tr><td><code>{e(str(a.get('code')))}</code></td><td>{e(str(a.get('severity')))}</td>"
             f"<td>{e(str(a.get('message')))}</td><td>{e(str(a.get('action')))}</td></tr>"
             for a in (obj.get("alerts") or [])]
    alerts = ("<h3>🔔 结构化告警</h3>"
              f"<p class='muted'>{e(str(obj.get('alert_policy') or ''))}</p>"
              + tbl(["代码", "级别", "事实说明", "动作"], arows, empty="暂无告警"))
    return boundary + "<h2>🧭 评估边界与告警</h2>" + leagues + alerts


def review_html(acc, facts: dict, docs: list, exported: list,
                eval_obj=None, clv_obj=None) -> str:
    """复盘主页：状态卡 + 日/周复盘存档 + 研究/回测事实 + 原始 JSON 下载。"""
    snaps = (acc or {}).get("snapshots") or []
    snap = snaps[-1] if snaps else {}
    sample = str(snap.get("sample_status") or "暂无复盘快照")
    # 关键：样本不足时绝不把任何数字当命中率结论展示，与复盘文档口径一致。
    hr = str(snap.get("hit_rate") or MISSING) if sample == "SUFFICIENT" else MISSING
    decided = snap.get("decided")
    need = snap.get("min_sample") or MIN_SAMPLE
    recon = str(snap.get("reconciliation") or "暂无")
    alerts = snap.get("alerts") or []
    head = ("<div class='card'><b>📌 本页只展示仓库已落盘的事实</b>"
            f"<div class='muted'>{e(REVIEW_DISCLAIMER)}；样本不足写「{MISSING}」，不补造比分、不推断未结算原因、不宣称准确率或 ROI 改善。</div></div>")
    kpis = ("<div class='summary'>"
            f"<div class='kpi'><b>{e(hr)}</b><span>历史命中率结论（门槛 {e(str(need))} 场）</span></div>"
            f"<div class='kpi'><b>{e(str(decided if decided is not None else '暂无'))}</b><span>已结算场次</span></div>"
            f"<div class='kpi'><b>{e(sample)}</b><span>样本状态</span></div></div>")
    detail = ("<div class='card'><h3>最新复盘快照</h3><dl class='kv'>"
              f"<dt>日期</dt><dd>{e(str(snap.get('date') or '暂无'))}</dd>"
              f"<dt>生成时间</dt><dd>{bjt(snap.get('generated_at'))}</dd>"
              f"<dt>run_id</dt><dd>{e(str(snap.get('run_id') or '暂无'))}</dd>"
              f"<dt>对账</dt><dd>{e(recon)}</dd>"
              f"<dt>未结算</dt><dd>{e(str(snap.get('pending', '暂无')))}</dd>"
              f"<dt>赛果未取得</dt><dd>{e(str(snap.get('missing_result', '暂无')))}</dd>"
              "<dt>告警</dt><dd>" + (", ".join(e(str(a)) for a in alerts) or "暂无") + "</dd></dl></div>")
    links = "".join(f"<li><a href='review/{e(stem)}.html'>{e(name)}</a></li>" for stem, name in docs)
    docs_html = ("<h2>📄 日 / 周复盘存档</h2>"
                 + (f"<ul class='src'>{links}</ul>" if links else "<div class='empty'>暂无复盘文档（待 accuracy-review 定时班次产出）</div>"))
    cards = []
    for name, title in REVIEW_FACTS:
        obj = facts.get(name)
        if obj is None:
            cards.append(f"<div class='card'><h3>{e(title)}</h3><p class='muted'>暂无（仓库未落盘）</p></div>")
            continue
        stamp = obj.get("checked_at") or obj.get("updated_at")
        raw = json.dumps(obj, ensure_ascii=False, indent=1)
        cards.append(
            f"<div class='card'><h3>{e(title)}</h3><dl class='kv'>"
            f"<dt>状态</dt><dd>{e(_fact_status(obj))}</dd>"
            f"<dt>生成时间</dt><dd>{bjt(stamp)}</dd>"
            f"<dt>文件</dt><dd><code>data/{e(name)}</code></dd></dl>"
            f"<details><summary>查看原始事实 JSON（逐字，不改写）</summary><pre>{e(raw)}</pre></details></div>")
    facts_html = "<h2>🔬 研究 / 回测 / 数据事实</h2><div class='grid2'>" + "".join(cards) + "</div>"
    dl = "".join(f"<li><a href='data/{e(n)}' rel='noopener'>{e(n)}</a></li>"
                 for n in exported + ["selection_funnel.json"])
    dl_html = ("<h2>⬇️ 原始数据（JSON）</h2>"
               + (f"<ul class='src'>{dl}</ul>" if dl else "<div class='empty'>暂无导出文件</div>")
               + "<p class='muted'>站点生成物不入库；以上文件随 GitHub Pages 一同发布，字段与仓库逐字一致。</p>")
    funnel_link = ("<div class='card'><h3>🔎 选场漏斗：为什么只推这几场 / 为什么 0 场</h3>"
                   "<p class='muted'>按日记录「窗口内多少场 → 多少场没进候选 → 观察名单卡在哪一层 → 最终发布」，"
                   "把数据缺口和模型判断分开。0 场日也照常出表。</p>"
                   "<p><a href='funnel.html'>查看选场漏斗 →</a></p></div>")
    eval_block = eval_boundary_html(eval_obj)
    clv_block = clv_join_html(clv_obj)
    return (head + kpis + detail + funnel_link + docs_html + facts_html + dl_html
            + eval_block + clv_block)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m ah site", description="生成 site/ 静态看板（GitHub Pages）。")
    ap.add_argument("--out", type=Path, default=SITE, help=f"输出目录（默认 {SITE}）")
    ap.add_argument("--reports", type=Path, default=REPORTS, help="日报 JSON 目录（默认仓库 reports/）")
    ap.add_argument("--data", type=Path, default=DATA, help="数据目录（stats.json、paper_trades.csv 等；默认仓库 data/）")
    ap.add_argument("--lab", type=Path, default=None, help="实验盘 picks CSV（默认 <data>/experiment_picks.csv）")
    ap.add_argument("--lab-demo", action="store_true", help="实验场页面加「示例数据」横幅（用样例数据预览时）")
    ap.add_argument("--review-docs", type=Path, default=None, help="复盘 Markdown 目录（默认仓库 docs/review/）")
    a = ap.parse_args(argv)
    print(build(site=a.out, reports=a.reports, data=a.data, lab=a.lab,
                lab_demo=a.lab_demo, review_docs=a.review_docs))


if __name__ == "__main__":
    main()
