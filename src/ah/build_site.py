"""生成 site/ 静态看板（GitHub Pages）。

页面：index(今日) / records(推荐记录) / stats(战绩) / sources(数据源) / rules(规则) / history(按日报告)
以及 reports/<date>.html。所有时间显示为北京时间（Asia/Shanghai）。
同时导出 site/data/*.json；排序/筛选/图表/VIP 门禁由 site/assets/app.js 在浏览器完成（无外部依赖）。
VIP 门禁仅前端隐藏：页面 HTML 与仓库数据仍然公开。
"""
from __future__ import annotations

import csv
import html
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import CONFIG, DATA, REPORTS, SITE, TEMPLATES, policy, records, source_status
from .stats import compute, summary_line, window

BJT = ZoneInfo("Asia/Shanghai")
CONF = {"high": "高", "medium": "中", "low": "低", "高": "高", "中": "中", "低": "低", "cancelled": "已撤销"}
NAV = [("index.html", "今日"), ("records.html", "推荐记录"), ("stats.html", "战绩"), ("sources.html", "数据源"),
       ("rules.html", "规则")]
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
              "<h3>✅ 为什么推</h3><ol>" + "".join(f"<li>{e(x)}</li>" for x in p.get("reasons", [])) + "</ol>"
              "<h3>⚠️ 风险</h3><ul>" + "".join(f"<li>{e(x)}</li>" for x in p.get("risks", [])) + "</ul>"
              f"<h3>🔗 来源</h3><div class='srcs'>{src}</div>"
              f"</div><div style='display:grid;gap:10px;align-content:start'>{prematch_panel(p, pol)}{result_panel(p)}</div></div>")
    return (f"<article class='card pick'><div class='meta'>🏆 {e(p.get('league', ''))} · 🕐 {bjt(p.get('kickoff_utc'))}"
            f"<span class='badge st-{st}'>{st}</span></div>"
            f"<div class='pick-head'><span class='teams'>{e(p['home'])} vs {e(p['away'])}</span></div>"
            f"<div class='verdict'><span class='v-ico'>🎯</span><span class='v-main'>推荐 {e(str(team))} {fl(p.get('line'))}{odds}</span>"
            f"<span class='conf conf-{e(conf)}'>{pt.CONF_ICON.get(conf, '')} {e(conf)}信心</span></div>"
            + lock(detail) + "</article>")


def report_html(r: dict, pol: dict | None = None, stats: dict | None = None) -> str:
    pol = pol or policy.load()
    out = [f"<p class='muted'>报告日期 {e(r['date'])} · 生成于 {bjt(r.get('generated_at'))} · {e(r.get('odds_format', ''))}</p>"]
    live = policy.published(r, pol)
    shown = [p for p in records.published_picks(r, pol)]
    if not live:
        out.append("<div class='empty'>今日无高信心推荐（宁缺毋滥）。</div>")
    out.extend(pick_card(p, pol, stats) for p in shown if p in live)
    gone = [p for p in shown if p not in live]
    if gone:
        out.append("<h2>已撤销</h2>" + "".join(pick_card(p, pol, stats) for p in gone))
    w = r.get("watched") or []
    rows = [f"<tr><td>{e(x.get('league', ''))}</td><td>{e(x.get('home', ''))} vs {e(x.get('away', ''))}</td>"
            f"<td class='wrap-text'>{e(x.get('reason_skipped', ''))}</td></tr>" for x in w]
    out.append("<h2>观察但不推荐</h2>" + lock(tbl(["赛事", "对阵", "原因"], rows, cls="", empty="暂无观察场次")))
    return "".join(out)


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

def build(site: Path = SITE, reports: Path = REPORTS, data: Path = DATA, now: datetime | None = None) -> list[str]:
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
    write("index.html", "今日推荐", bar + (report_html(latest, pol, s) if latest else "<div class='empty'>暂无报告</div>"), "index.html")
    rows = record_rows(reps, pol)
    write("records.html", "推荐记录", records_html(list(reversed(rows)), pol), "records.html")
    write("stats.html", "战绩", bar + stats_html(s, today), "stats.html")
    write("sources.html", "数据源状态", sources_html(st), "sources.html")
    write("rules.html", "规则说明", rules_html(pol, cfg), "rules.html")
    hist = [f"<tr><td><a href='reports/{r['date']}.html'>{r['date']}</a></td><td>{len(policy.published(r, pol))}</td>"
            f"<td>{len(records.published_picks(r, pol))}</td></tr>" for r in reversed(reps)]
    write("history.html", "历史日报", tbl(["日期", "当前有效推荐", "公开推荐（含撤销）"], hist, empty="暂无报告"), "")
    pub_rows = [{k: v for k, v in x.items() if k != "_pick"} for x in rows]
    (site / "data" / "records.json").write_text(json.dumps(pub_rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (site / "data" / "stats.json").write_text(json.dumps(s or {}, ensure_ascii=False, indent=1), encoding="utf-8")
    (site / "data" / "source_status.json").write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    (site / ".nojekyll").write_text("")
    return [r["date"] for r in reps]


if __name__ == "__main__":
    print("built site for", build())
