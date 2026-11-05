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
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import CONFIG, DATA, REPORTS, SITE, TEMPLATES, model_lab, policy, records, source_status
from .stats import compute, summary_line, window

BJT = ZoneInfo("Asia/Shanghai")
CONF = {"high": "高", "medium": "中", "low": "低", "高": "高", "中": "中", "低": "低", "cancelled": "已撤销"}
NAV = [("index.html", "今日"), ("records.html", "推荐记录"), ("stats.html", "战绩"), ("sources.html", "数据源"),
       ("rules.html", "规则"), ("model-lab.html", "实验场")]
LOCK = "🔒 VIP 可见，输入密码解锁"
NONE = "暂无"
CREDIT = "Football data by 5DollarFootballAPI"
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
    nav = "".join(f"<a href='{base}{h}'{' class=\"cur\"' if h == cur else ''}>{t}</a>" for h, t in NAV)
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
    hit = f"{o['hit_rate']:.1%}" if o.get("decided") else "暂无"
    hit_sub = f"总体命中率（{o['decided']}场）" if o.get("decided") else "总体命中率（暂无已结算）"
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
    st = records.status(p)
    team = p.get("pick_team") or p.get(p.get("pick_side", ""), "")
    conf = CONF.get(p.get("confidence", ""), p.get("confidence", ""))
    odds = f" @{water(p.get('odds'))}" if p.get("odds") is not None else ""
    src = "".join(f"<a class='srcl' href='{e(u)}' rel='noopener'>{e(pt.link_label(u))}</a>"
                  for u in p.get("sources", []) if str(u).startswith("http"))
    detail = ("<div class='pick-body'><div>" + compare_table(p) + hit_chips(p, stats) +
              "<h3>✅ 为什么推</h3><ol class='reasons'>" + "".join(f"<li>{e(x)}</li>" for x in p.get("reasons", [])) + "</ol>"
              "<h3>⚠️ 风险在哪</h3><ul class='risks'>" + "".join(f"<li>{e(x)}</li>" for x in p.get("risks", [])) + "</ul>"
              f"<h3>🔗 数据来源</h3><div class='srcs'>{src or NONE}</div>"
              f"</div><div style='display:grid;gap:10px;align-content:start'>{prematch_panel(p, pol)}{result_panel(p)}</div></div>")
    return (f"<article class='card pick'><div class='meta'>🏆 {e(p.get('league', ''))} · 🕐 {bjt(p.get('kickoff_utc'))}"
            f"<span class='badge st-{st}'>{st}</span></div>"
            f"<div class='pick-head'><span class='teams'>{e(p['home'])} vs {e(p['away'])}</span></div>"
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
    return (f"<div class='rhead'><div class='rdate'>📅 {e(r['date'])}<small>{ed}</small></div>"
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
    return ("<div class='nopick'><div class='np-ico'>🛡️</div><div><div class='np-title'>今日无高信心推荐</div>"
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
    pol = pol or policy.load()
    live = policy.published(r, pol)
    shown = [p for p in records.published_picks(r, pol)]
    out = [report_head(r, pol, len(live), len(shown)), after_head]
    if not live:
        out.append(no_pick_html(r))
    out.extend(pick_card(p, pol, stats) for p in shown if p in live)
    gone = [p for p in shown if p not in live]
    if gone:
        out.append("<h2>↩️ 已撤销</h2>" + "".join(pick_card(p, pol, stats) for p in gone))
    out.append(watchlist_html(r))
    w = r.get("watched") or []
    if w:
        rows = [f"<tr><td>{e(x.get('league', ''))}</td><td>{e(x.get('home', ''))} vs {e(x.get('away', ''))}</td>"
                f"<td class='wrap-text'>{e(x.get('reason_skipped', ''))}</td></tr>" for x in w]
        out.append("<h2>👀 观察但不推荐</h2>" + lock(tbl(["赛事", "对阵", "原因"], rows, cls="", empty="暂无观察场次")))
    return "".join(out)


def stats_brief(s: dict | None) -> str:
    """首页底部统计表：总体 / 分联赛 / 分信心命中率（VIP，与战绩页一致）。"""
    if not s:
        return ""
    def rows(d):
        return [f"<tr><td>{e(k)}</td><td class='num'>{v['picks']}</td><td class='num'>{v['decided']}</td>"
                f"<td class='num'>{pct(v['hit_rate']) if v['decided'] else NONE}</td><td class='num'>{units_html(v['units'])}</td></tr>"
                for k, v in d.items()]
    head = ["分组", "场次", "有效", "命中率", "盈亏"]
    body = ("<div class='grid3'>"
            + "<div><h3>总体</h3>" + tbl(head, rows({"全部": s.get("overall", {})}) if s.get("overall") else [], cls="", empty="暂无已结算推荐") + "</div>"
            + "<div><h3>分联赛</h3>" + tbl(head, rows(s.get("by_league", {})), cls="", empty="暂无已结算推荐") + "</div>"
            + "<div><h3>分信心</h3>" + tbl(head, rows(s.get("by_confidence", {})), cls="", empty="暂无已结算推荐") + "</div></div>")
    return (f"<h2>📊 战绩概览</h2><p class='muted'>{e(summary_line(s))} · 完整图表见 <a href='stats.html'>战绩页</a></p>"
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
        h7.append(window(series, x["date"], 7)["hit_rate"])
        h30.append(window(series, x["date"], 30)["hit_rate"])
        notes.append(f"{x['match']} {records.SETTLE_CN.get(x['outcome'], x['outcome'])} {x['units']:+.2f}")
    return {"labels": labels, "cum": cum, "hit7": h7, "hit30": h30, "notes": notes}


def stats_html(s: dict | None, today: str) -> str:
    if not s:
        return "<div class='empty'>暂无统计数据</div>"
    pub = f"<div class='card'><b>{e(summary_line(s))}</b><div class='muted'>统计更新于 {bjt(s.get('updated_at'))}</div></div>"
    w7, w30 = window(s.get("series", []), today, 7), window(s.get("series", []), today, 30)

    def kpi(w, t):
        return (f"<div class='kpi'><b>{pct(w['hit_rate']) if w['decided'] else '暂无'}</b><span>{t}命中率"
                f"（{w['decided']}场）</span></div>")

    def table(title, d):
        rows = [f"<tr><td>{e(k)}</td><td data-v='{v['picks']}'>{v['picks']}</td><td data-v='{v['decided']}'>{v['decided']}</td>"
                f"<td>{v['win']}/{v['half_win']}/{v.get('push', 0)}/{v['half_loss']}/{v['loss']}/{v.get('void', 0)}</td>"
                f"<td data-v='{v['hit_rate'] if v['decided'] else -1}'>{pct(v['hit_rate']) if v['decided'] else NONE}</td>"
                f"<td data-v='{v['units']}'>{units_html(v['units'])}</td><td data-v='{v['roi']}'>{pct(v['roi']) if v['decided'] else NONE}</td></tr>"
                for k, v in d.items()]
        return f"<div><h2>{title}</h2>" + tbl(["分组", "场次", "有效场", "赢/赢半/走/输半/输/取消", "命中率", "盈亏(单位)", "ROI"],
                                              rows, empty="暂无已结算推荐") + "</div>"
    has = bool(s.get("series"))
    cd = chart_data(s)
    charts = ("<div class='grid2'><div class='card'><h3>累计盈亏（单位，每注 1 单位）</h3>"
              + ("<div class='chart' id='chart-units'></div>" if has else "<div class='empty'>暂无已结算推荐，结算后自动生成曲线</div>")
              + "</div><div class='card'><h3>滚动命中率（近7天 / 近30天）</h3>"
              + ("<div class='chart' id='chart-hit'></div>" if has else "<div class='empty'>暂无已结算推荐，结算后自动生成曲线</div>")
              + "</div></div>"
              "<script id='chart-data' type='application/json'>" + json.dumps(cd, ensure_ascii=False).replace("</", "<\\/") + "</script>")
    tables = ("<div class='grid2'>" + table("总体", {"全部": s["overall"]}) + table("按联赛", s.get("by_league", {}))
              + table("按信心等级", s.get("by_confidence", {})) + table("按盘口深度", s.get("by_depth", {}))
              + table("上盘 / 下盘", s.get("by_role", {})) + table("按具体盘口", s.get("by_line", {})) + "</div>")
    note = ("<p class='muted'>命中率 = (赢 + 0.5×赢半) / (场次 − 走水 − 取消)；盈亏按每注 1 单位、港盘水位计算；"
            "ROI = 盈亏 / 有效场次。赛前撤销的推荐记为「取消」，不计入命中率。</p>")
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
              "<span>各模型独立出场次、单独记账（1 单位平注），只用于检验模型；正式推荐只看首页的高信心亚盘。"
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
        ("只推高信心", f"只公开发布信心「{e(str(pol['min_confidence_to_publish']))}」的推荐；中/低信心只作内部观察，不推送、不展示为推荐。"),
        ("每日上限", f"每天最多 {pol['max_picks_per_day']} 场。"),
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
          lab: Path | None = None, lab_demo: bool = False) -> list[str]:
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


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m ah site", description="生成 site/ 静态看板（GitHub Pages）。")
    ap.add_argument("--out", type=Path, default=SITE, help=f"输出目录（默认 {SITE}）")
    ap.add_argument("--reports", type=Path, default=REPORTS, help="日报 JSON 目录（默认仓库 reports/）")
    ap.add_argument("--data", type=Path, default=DATA, help="数据目录（stats.json、paper_trades.csv 等；默认仓库 data/）")
    ap.add_argument("--lab", type=Path, default=None, help="实验盘 picks CSV（默认 <data>/experiment_picks.csv）")
    ap.add_argument("--lab-demo", action="store_true", help="实验场页面加「示例数据」横幅（用样例数据预览时）")
    a = ap.parse_args(argv)
    print(build(site=a.out, reports=a.reports, data=a.data, lab=a.lab, lab_demo=a.lab_demo))


if __name__ == "__main__":
    main()
