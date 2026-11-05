"""先知 Telegram 推送（HTML parse_mode）：日报 / 赛前确认 / 赛后回测。

用法（需 TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID；未设置或 --dry-run 只打印）::

  python -m ah telegram YYYY-MM-DD [--dry-run]                        # 日报（兼容旧用法）
  python -m ah telegram daily YYYY-MM-DD [--dry-run]
  python -m ah telegram prematch YYYY-MM-DD MATCH_ID [--dry-run]      # 读 pick.prematch（或旧 final_check）
  python -m ah telegram backtest YYYY-MM-DD MATCH_ID [--dry-run]      # 读 pick.result
  python -m ah telegram weekly [YYYY-MM-DD] [--lab CSV] [--lab-stats JSON] --dry-run
                                                                  # 每周模型排行样稿（读 experiment_stats.json，按 CLV）

日报可选：--announce-upgrade（开头加「🆕 看板已升级」+ 网站链接）；
--lab CSV（实验盘，默认 data/experiment_picks.csv，不存在则整段省略）；
--lab-stats JSON（默认与 --lab 同目录的 experiment_stats.json / data/experiment_stats.json）。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

from . import policy
from .render_report import report_paths

SITE_URL = "https://modouxin-dev.github.io/ah-predictions/"
LIMIT = 4096
CONF = {"high": "高", "medium": "中", "low": "低", "高": "高", "中": "中", "低": "低"}
CONF_ICON = {"高": "🟢", "中": "🟡", "低": "⚪"}
DECISION = {"保留": "✅ 保留", "keep": "✅ 保留", "调整": "🔄 调整", "adjust": "🔄 调整",
            "撤销": "❌ 撤销", "cancel": "❌ 撤销"}
SETTLE_CN = {"win": "赢", "half_win": "赢半", "push": "走", "half_loss": "输半", "loss": "输", "void": "取消"}
SETTLE_ICON = {"win": "✅", "half_win": "✅", "push": "➖", "half_loss": "❌", "loss": "❌", "void": "⛔"}
LABELS = {"titan007": "球探", "the-odds-api": "The Odds API", "football-data": "football-data",
          "oddspapi": "OddsPapi", "api-football": "API-Football", "5dollarfootballapi": "5DollarFootballAPI"}

e = lambda s: html.escape(str(s), quote=False)  # noqa: E731


def _default_chat():
    try:
        import yaml
        from pathlib import Path
        cfg = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "settings.yaml").read_text())
        return str((cfg.get("telegram") or {}).get("chat_id") or "") or None
    except Exception:
        return None


def bj(utc: str | None, fmt: str = "%m-%d %H:%M") -> str:
    if not utc:
        return "—"
    dt = datetime.fromisoformat(str(utc).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt.astimezone(timezone(timedelta(hours=8)))).strftime(fmt)


def ln(x) -> str:
    return "—" if x is None else f"{float(x):+g}".replace("+0", "0") if float(x) == 0 else f"{float(x):+g}"


def link_label(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    for k, v in LABELS.items():
        if k in host:
            return v
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "net", "org", "gov", "ac"):
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else host or "来源"


def links(urls) -> str:
    out = []
    for u in urls or []:
        u = str(u).split(" ")[0]
        if u.startswith("http"):
            out.append(f'<a href="{html.escape(u, quote=True)}">{e(link_label(u))}</a>')
    return " · ".join(out)


def footer() -> list[str]:
    return [f"<i>{e(policy.DISCLAIMER)}</i>", "—— 先知"]


def history(stats: dict | None) -> str:
    if stats is None:
        return "📈 历史战绩：暂无已结算推荐"
    from .stats import summary_line
    return "📈 " + e(summary_line(stats).lstrip("📊 ").strip())


def _team(p: dict) -> str:
    return p.get("pick_team") or p[p["pick_side"]]


def match_block(p: dict) -> list[str]:
    return [f"⚽ <b>{e(p['home'])} vs {e(p['away'])}</b>",
            f"🏆 {e(p.get('league', ''))} ｜ 🕐 {bj(p.get('kickoff_utc'))} 北京时间"]


def odds_lines(p: dict) -> list[str]:
    out = [f"• 推荐盘：{ln(p['line'])} @ {p['odds']}" if p.get("odds") is not None else f"• 推荐盘：{ln(p['line'])}"]
    if p.get("line_open") is not None:
        out[-1] += f"（初盘 {ln(p['line_open'])}）"
    c = p.get("market_consensus") or {}
    if c.get("pick_line") is not None:
        w = f" @ {c['pick_water']}" if c.get("pick_water") is not None else ""
        out.append(f"• 共识中位：{ln(c['pick_line'])}{w}（{c.get('bookmakers', '?')} 家，{e(c.get('source', ''))}）")
    pin = c.get("pinnacle") or {}
    if pin.get("pick_line") is not None:
        w = f" @ {pin['pick_water']}" if pin.get("pick_water") is not None else ""
        out.append(f"• Pinnacle：{ln(pin['pick_line'])}{w}")
    b = p.get("bet365_movement") or {}
    if b.get("pick_line") is not None:
        ow = f" @ {b['open_pick_water']}" if b.get("open_pick_water") is not None else ""
        cw = f" @ {b['pick_water']}" if b.get("pick_water") is not None else ""
        out.append(f"• Bet365：{ln(b.get('open_pick_line'))}{ow} → {ln(b['pick_line'])}{cw}")
    return [e(x) for x in out]


def pick_block_v1(p: dict, i: int | None = None) -> str:
    conf = CONF.get(p.get("confidence"), str(p.get("confidence")))
    odds = f" @ {p['odds']}" if p.get("odds") is not None else ""
    head = f"<b>#{i}</b> " if i else ""
    lines = [head + match_block(p)[0], match_block(p)[1], "",
             f"🎯 <b>{e(_team(p))} {ln(p['line'])}{odds}</b>", "",
             "📊 <b>盘口</b>", *odds_lines(p), "",
             f"🔥 信心：{CONF_ICON.get(conf, '')} <b>{e(conf)}</b>"]
    if p.get("reasons"):
        lines += ["", "📌 <b>核心理由</b>", *[f"{n}. {e(r)}" for n, r in enumerate(p["reasons"][:5], 1)]]
    if p.get("risks"):
        lines += ["", "⚠️ <b>风险</b>", *[f"• {e(r)}" for r in p["risks"]]]
    if p.get("sources"):
        lines += ["", "🔗 " + links(p["sources"])]
    return "\n".join(lines)


def tlen(s: str) -> int:
    """Telegram 按 UTF-16 码元计长度（emoji 记 2）；这里对 HTML 源文计，比实体解析后的长度更保守。"""
    return len(str(s).encode("utf-16-le")) // 2


_TAG = re.compile(r"<(/?)([a-z][a-z0-9-]*)([^>]*)>")


def _open_tags(chunk: str, carry: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """从 carry（进入 chunk 时已打开的标签）出发扫描 chunk，返回结束时仍未闭合的 [(name, 原开标签)]。"""
    st = list(carry)
    for m in _TAG.finditer(chunk):
        if m.group(1):
            for i in range(len(st) - 1, -1, -1):
                if st[i][0] == m.group(2):
                    del st[i]
                    break
        else:
            st.append((m.group(2), m.group(0)))
    return st


def _safe_cut(s: str, n: int) -> int:
    """在 n 之前找一个不落在标签 <…> 或实体 &…; 中间的切点。"""
    cut = n
    lt, gt = s.rfind("<", 0, cut), s.rfind(">", 0, cut)
    if lt > gt:
        cut = lt
    amp = s.rfind("&", 0, cut)
    if amp != -1 and ";" not in s[amp:cut]:
        cut = amp
    return cut if cut > 0 else n


def _raw_chunks(b: str, room: int) -> list[str]:
    out, chunk = [], ""
    for line in b.split("\n"):
        while tlen(line) > room:
            if chunk:
                out.append(chunk)
                chunk = ""
            cut = _safe_cut(line, room // 2)  # 每字符最多 2 个 UTF-16 码元
            out.append(line[:cut])
            line = line[cut:]
        if chunk and tlen(chunk) + 1 + tlen(line) > room:
            out.append(chunk)
            chunk = ""
        chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        out.append(chunk)
    return out


def split(blocks: list[str], limit: int = LIMIT) -> list[str]:
    """按块拼成多条消息，每条 ≤ limit（UTF-16 计）。块尽量整块放；单块过长时按行拆，
    跨条的 <pre>/<blockquote>/<b>/<a> 等在断点处自动闭合、下一条按原开标签重新打开，保证每条 HTML 独立合法。"""
    room = limit - 200  # 给补齐的闭合/重开标签留余量
    pieces: list[str] = []
    for b in blocks:
        if tlen(b) <= limit:
            pieces.append(b)
            continue
        carry: list[tuple[str, str]] = []
        for raw in _raw_chunks(b, room):
            after = _open_tags(raw, carry)
            pieces.append("".join(t for _, t in carry) + raw + "".join(f"</{n}>" for n, _ in reversed(after)))
            carry = after
    msgs, cur = [], ""
    for b in pieces:
        if cur and tlen(cur) + 2 + tlen(b) > limit:
            msgs.append(cur)
            cur = ""
        cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        msgs.append(cur)
    return msgs


# ---------- 亚盘紧凑版式（截图风格） ----------
DIV = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
WEEK = "一二三四五六日"


def wcol(s: str) -> int:
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, w: int, right: bool = False) -> str:
    s = str(s)
    fill = " " * max(0, w - wcol(s))
    return fill + s if right else s + fill


def pre_table(head: list[str], rows: list[list], right_from: int = 1) -> str:
    cols = [head, *rows]
    ws = [max(wcol(r[i]) for r in cols) for i in range(len(head))]
    fmt = lambda r: " ".join(pad(c, ws[i], i >= right_from) for i, c in enumerate(r)).rstrip()  # noqa: E731
    return "<pre>" + e("\n".join(fmt(r) for r in cols)) + "</pre>"


def short(t: str, n: int = 38) -> str:
    """Telegram 用短句：取第一个分句，过长截断。"""
    t = str(t).strip()
    for sep in ("；", "。", ";"):
        if sep in t and len(t.split(sep)[0]) >= 8:
            t = t.split(sep)[0]
            break
    return t if len(t) <= n else t[: n - 1] + "…"


def w2(x) -> str:
    return "—" if x is None else f"{float(x):.2f}"


def comparison_rows(p: dict) -> list[list[str]]:
    """机构对比（推荐方视角）：只返回有真实数据的行。[机构, 盘口, 水位]"""
    rows = [["本推荐", ln(p["line"]), w2(p.get("odds"))]]
    c = p.get("market_consensus") or {}
    if c.get("pick_line") is not None:
        rows.append([f"共识中位({c.get('bookmakers', '?')}家)", ln(c["pick_line"]), w2(c.get("pick_water"))])
    pin = c.get("pinnacle") or {}
    if pin.get("pick_line") is not None:
        rows.append(["Pinnacle", ln(pin["pick_line"]), w2(pin.get("pick_water"))])
    b = p.get("bet365_movement") or {}
    if b.get("pick_line") is not None:
        rows.append(["Bet365 初→即", f"{ln(b.get('open_pick_line'))}→{ln(b['pick_line'])}",
                     f"{w2(b.get('open_pick_water'))}→{w2(b.get('pick_water'))}"])
    if p.get("line_open") is not None and any("titan007" in str(s) for s in p.get("sources") or []):
        rows.append(["球探 初→即", f"{ln(p['line_open'])}→{ln(p['line'])}", w2(p.get("odds"))])
    return rows


def deviation(p: dict) -> str | None:
    c = p.get("market_consensus") or {}
    if c.get("pick_line") is None:
        return None
    d = float(c["pick_line"]) - float(p["line"])  # 推荐方让球为负：更负=更深
    if d == 0:
        return "与共识一致"
    return f"比共识{'深' if d > 0 else '浅'} {abs(d):g}" + ("（&gt;0.5 应撤下）" if abs(d) > 0.5 else "（&gt;0.25 降一档）" if abs(d) > 0.25 else "")


def record_of(d: dict | None) -> str:
    if not d or not d.get("decided"):
        return "暂无历史"
    return f"{d['picks']}场 {d['hit_rate']:.0%} {d.get('units', 0):+.2f}u"


def venue(p: dict) -> str | None:
    v = p.get("venue") or (p.get("fixture") or {}).get("venue")
    if isinstance(v, dict):
        v = "，".join(x for x in (v.get("name"), v.get("city")) if x)
    return v or None


# ---------- TG 对齐规则（v4） ----------
# 1) 等宽块 <pre> 里只让「数字列」对齐：数字/符号全是 ASCII，放在行首按固定宽度右对齐；
#    含中文的标签（机构名、联赛名）一律放在行尾。Telegram 手机端等宽字体的中文字宽≈1.6–1.7 个
#    英文字宽（不是 2），中文放前面会把后面的列推歪；放行尾就不影响任何一列。
#    表头不进 <pre>，改成块上方的一行说明（同样避免中文表头错位）。
# 2) 分区标题统一为「emoji + <b>标题</b>」，分区之间空一行；推什么 → 为什么推 → 风险在哪 → 数据来源。
# 3) 理由/风险用 <blockquote>；长列表（未推原因、实验盘、数据来源）用 <blockquote expandable>。
# 4) 队名：主客合计显示宽度超过 TEAMS_ONE_LINE 就拆两行（第二行以 vs 开头）；单个队名超过 TEAM_MAX
#    时优先只保留中文名，否则按显示宽度截断加「…」。手机气泡一行约 42 个半角宽度。
TEAM_MAX = 28
TEAMS_ONE_LINE = 34
LEAGUE_MAX = 22


def trunc_w(s: str, w: int) -> str:
    s = str(s)
    if wcol(s) <= w:
        return s
    out = ""
    for c in s:
        if wcol(out + c) > w - 1:
            break
        out += c
    return out.rstrip() + "…"


def team_name(name: str) -> str:
    name = str(name or "").strip()
    if wcol(name) <= TEAM_MAX:
        return name
    head = name.split(" ")[0]
    if head != name and any(wcol(c) == 2 for c in head):
        return head  # 「布拉格斯拉维亚 Slavia Praha」过长时只保留中文名
    return trunc_w(name, TEAM_MAX)


def teams_line(home: str, away: str, mid: str = "vs") -> str:
    h, a = team_name(home), team_name(away)
    if wcol(h) + wcol(a) + wcol(mid) + 2 <= TEAMS_ONE_LINE:
        return f"<b>{e(h)}</b> {e(mid)} <b>{e(a)}</b>"
    return f"<b>{e(h)}</b>\n{e(mid)} <b>{e(a)}</b>"


def league_name(k: str | None) -> str:
    """「UEFA Champions League（欧冠 联赛阶段第4轮）」→「欧冠 联赛阶段第4轮」；没有中文括注就截断原名。"""
    k = str(k or "")
    m = re.search(r"（([^（）]+)）", k)
    return trunc_w(m.group(1) if m else k, LEAGUE_MAX)


def sec(icon: str, title: str, note: str | None = None) -> str:
    """统一分区标题：emoji + 粗体标题 [+ 斜体小注]。"""
    return f"{icon} <b>{e(title)}</b>" + (f" <i>{e(note)}</i>" if note else "")


def quote(lines: list[str], expandable: bool = False) -> str:
    """lines 需已转义。"""
    return f"<blockquote{' expandable' if expandable else ''}>" + "\n".join(lines) + "</blockquote>"


def num_table(rows: list[list[str]]) -> str:
    """<pre> 对齐表：除最后一列外都是 ASCII 数值列（右对齐、列间两个空格），最后一列是标签（可含中文）。"""
    n = len(rows[0]) - 1
    ws = [max(len(str(r[i])) for r in rows) for i in range(n)]
    out = ["  ".join(str(r[i]).rjust(ws[i]) for i in range(n)) + "  " + str(r[n]) for r in rows]
    return "<pre>" + e("\n".join(x.rstrip() for x in out)) + "</pre>"


def fl(x) -> str:
    """盘口统一两位小数（-0.5 → -0.50），保证小数点对齐；平手为 0.00。"""
    if x is None:
        return ""
    v = float(x)
    return "0.00" if v == 0 else f"{v:+.2f}"


def wt(x) -> str:
    return "" if x is None else f"{float(x):.2f}"


def tg_rows(p: dict) -> list[list[str]]:
    """机构对比（推荐方视角）[盘口, 水位, 机构]：只列有真实数据的行，初盘/即时拆成两行（不在等宽块里用箭头）。"""
    rows = [[fl(p["line"]), wt(p.get("odds")), "本推荐"]]
    c = p.get("market_consensus") or {}
    if c.get("pick_line") is not None:
        rows.append([fl(c["pick_line"]), wt(c.get("pick_water")), f"共识中位·{c.get('bookmakers', '?')}家"])
    pin = c.get("pinnacle") or {}
    if pin.get("pick_line") is not None:
        rows.append([fl(pin["pick_line"]), wt(pin.get("pick_water")), "Pinnacle"])
    b = p.get("bet365_movement") or {}
    if b.get("pick_line") is not None:
        if b.get("open_pick_line") is not None:
            rows.append([fl(b["open_pick_line"]), wt(b.get("open_pick_water")), "Bet365 初盘"])
        rows.append([fl(b["pick_line"]), wt(b.get("pick_water")), "Bet365 即时"])
    if p.get("line_open") is not None and any("titan007" in str(s) for s in p.get("sources") or []):
        rows.append([fl(p["line_open"]), "", "球探 初盘"])
    return rows


def verdict(p: dict) -> str:
    conf = CONF.get(p.get("confidence"), str(p.get("confidence")))
    odds = f" @{wt(p['odds'])}" if p.get("odds") is not None else ""
    return f"🎯 <b>推荐 {e(_team(p))} {ln(p['line'])}{odds}</b> ｜ 信心 {CONF_ICON.get(conf, '')}<b>{e(conf)}</b>"


def match_head(p: dict, mid: str = "vs") -> str:
    k = bj(p.get("kickoff_utc"))
    meta = f"🏆 {e(league_name(p.get('league')))}" + (f" · 🕐 {k}" if k != "—" else "")
    return meta + "\n" + teams_line(p["home"], p["away"], mid)


def sources_block(urls, title: str = "数据来源") -> list[str]:
    ls = links(urls)
    return [sec("🔗", title), quote([ls], expandable=True)] if ls else []


def compact_block(p: dict, stats: dict | None = None, extra: list[str] | None = None,
                  show_sources: bool = True) -> str:
    """单场卡片：推什么 → 为什么推 → 风险在哪 → 数据来源。空字段整段省略。"""
    conf = CONF.get(p.get("confidence"), str(p.get("confidence")))
    lines = [match_head(p), verdict(p), *(extra or []),
             sec("🏦", "机构对比", "盘口 · 水位 · 机构"), num_table(tg_rows(p))]
    dv = deviation(p)
    if dv:
        lines.append(f"📊 {dv}")
    if venue(p):
        lines.append(f"🏟 {e(venue(p))}")
    probs = p.get("model_probs")
    if probs:
        lines.append("🧮 模型概率 " + " · ".join(f"{e(k)} {float(v):.0%}" for k, v in probs.items()))
    s = stats or {}
    lg = (s.get("by_league") or {}).get(p.get("league"))
    cf = (s.get("by_confidence") or {}).get(conf)
    lines.append(f"📈 本联赛 {record_of(lg)} ｜ {e(conf)}信心 {record_of(cf)}")
    if p.get("reasons"):
        lines += ["", sec("✅", "为什么推"), quote([f"• {e(short(r, 80))}" for r in p["reasons"][:3]])]
    if p.get("risks"):
        lines += ["", sec("⚠️", "风险在哪"), quote([f"• {e(short(r, 64))}" for r in p["risks"][:2]])]
    if show_sources and p.get("sources"):
        lines += ["", *sources_block(p["sources"][:4])]
    return "\n".join(lines)


def league_short(k: str) -> str:
    m = re.search(r"（([^ （）·]+)", k)
    return m.group(1) if m else k.split("（")[0]


def stats_table(stats: dict | None, top: int = 3) -> str:
    s = stats or {}
    o = s.get("overall") or {}
    if not o.get("decided"):
        return sec("📈", "历史战绩") + "：暂无已结算推荐"

    def row(name, d):
        w = f"{d.get('win', 0)}+{d['half_win']}" if d.get("half_win") else str(d.get("win", 0))
        l_ = f"{d.get('loss', 0)}+{d['half_loss']}" if d.get("half_loss") else str(d.get("loss", 0))
        return [str(d.get("picks", 0)), f"{w}-{d.get('push', 0)}-{l_}", f"{d.get('hit_rate', 0):.0%}" if d.get("decided") else "-",
                f"{d.get('units', 0):+.2f}u", trunc_w(name, 12)]
    rows = [row("总体", o)]
    hi = (s.get("by_confidence") or {}).get("高")
    if hi and hi.get("picks"):
        rows.append(row("高信心", hi))
    bl = sorted((s.get("by_league") or {}).items(), key=lambda kv: -kv[1].get("picks", 0))[:top]
    rows += [row(league_short(k), v) for k, v in bl]
    return sec("📈", "历史战绩", "场 · 赢-走-输 · 命中 · 盈亏") + "\n" + num_table(rows)


def tail(stats: dict | None, site_url: str = SITE_URL) -> str:
    return (stats_table(stats) + f'\n🌐 <a href="{html.escape(site_url, quote=True)}">完整看板 · 先知</a>\n\n'
            + "\n".join(footer()))


def date_line(rep: dict, n_pub: int) -> str:
    d = datetime.strptime(rep["date"], "%Y-%m-%d")
    n_all = len(rep.get("picks") or []) + len(rep.get("watched") or []) + len(rep.get("watchlist") or [])
    return f"📅 {d:%m-%d} 周{WEEK[d.weekday()]} · 筛选 {n_all} 场 · 推荐 {n_pub} 场"


def watch_counts(rep: dict) -> tuple[dict, dict]:
    """按 watchlist 自动统计：(各信心档场数, 各不推类别场数)。只统计，不点名（中/低信心不推送具体场次）。"""
    conf, cat = {}, {}
    for w in rep.get("watchlist") or []:
        c = CONF.get(w.get("confidence") or "", "未定")
        conf[c] = conf.get(c, 0) + 1
        k = w.get("withheld_category")
        if k:
            cat[k] = cat.get(k, 0) + 1
    return conf, cat


def no_pick_lines(rep: dict) -> list[str]:
    lines = ["🛡 <b>今日无高信心推荐（宁缺毋滥）</b>"]
    conf, cat = watch_counts(rep)
    if conf:
        n = sum(conf.values())
        lines.append(f"👀 观察 {n} 场：" + " · ".join(f"{CONF_ICON.get(c, '▫️')}{c} {conf[c]}"
                                                     for c in ("高", "中", "低", "未定") if conf.get(c)))
    if cat:
        rows = sorted(cat.items(), key=lambda kv: -kv[1])
        w = max(len(str(v)) for _, v in rows)
        lines += [sec("📌", "未推原因", "场数 · 类别"),
                  quote([f"<code>{str(v).rjust(w)}</code>  {e(k)}" for k, v in rows], expandable=len(rows) > 3)]
    return lines


ANNOUNCE = "🆕\ufe0f 看板已升级"


def lab_block(rows: list[dict] | None) -> str | None:
    """日报末尾的模型实验盘（不计入正式命中率）。按 亚盘/1X2/大小球 分组，空组省略；全空返回 None。
    整段放进 <blockquote expandable>，默认折叠，不抢正式推荐的视线。"""
    from . import model_lab as ml
    groups = ml.by_market(rows or [])
    if not groups:
        return None
    body = []
    lg = ml.legend(rows or [])
    if lg:
        body.append(f"<i>{e(lg)}</i>")
    for m, rs in groups:
        body.append(f"<b>{ml.MARKET_CN[m]}</b>")
        for r in rs:
            pick = " ".join(x for x in (ml.selection_text(r), ml.line_text(r)) if x)
            odds = f" @{ml.odds_text(r)}" if ml.odds_text(r) else ""
            ov = " · ".join(ml.overlap_tags(r.get("overlap") or ""))
            ov_s = f" <i>{e(ov)}</i>" if ov else ""
            body.append(f"<code>{e(ml.model_code(r['model_id']))}</code> {e(ml.short_team(r['home']))}-{e(ml.short_team(r['away']))}"
                        f"｜<b>{e(pick)}</b>{odds}{ov_s}")
    return sec("🧪", "模型实验盘", "（不计入正式命中率）") + "\n" + quote(body, expandable=True)


def daily(rep: dict, stats: dict | None = None, site_url: str = SITE_URL, lab: list[dict] | None = None,
          announce: bool = False) -> list[str]:
    picks = policy.published(rep)
    head = f"<b>【先知】⚽ 亚盘日报 {e(rep['date'])}</b>\n{date_line(rep, len(picks))}"
    if announce:
        head = (f'<b>{ANNOUNCE}</b> 👉 <a href="{html.escape(SITE_URL, quote=True)}">'
                f'{e(SITE_URL.removeprefix("https://").rstrip("/"))}</a>\n\n' + head)
    blocks = [head]
    if not picks:
        blocks.append("\n".join(no_pick_lines(rep)))
    blocks += [DIV + "\n" + compact_block(p, stats) for p in picks]
    gone = [p for p in rep.get("picks") or [] if p not in picks
            and (p.get("cancelled") or p.get("confidence") == "cancelled")]
    if gone:
        blocks.append(sec("↩️", "赛前已撤销", "（不计入命中率）") + "\n" + "\n".join(
            "• " + teams_line(p["home"], p["away"]).replace("<b>", "<s>").replace("</b>", "</s>").replace("\nvs", "\n   vs")
            + f"\n   原推荐 <s>{e(_team(p))} {ln(p['line'])}</s>" for p in gone))
    lb = lab_block(lab)
    if lb:
        blocks.append(DIV + "\n" + lb)
    blocks.append(DIV + "\n" + tail(stats, site_url))
    return split(blocks)


def weekly(lab=None, end: str | None = None, top: int = 12) -> list[str]:
    """每周模型排行（实验盘）：直接读 experiment_stats.json，按平均 CLV 排序（null 排最后）。
    ROI/命中率为参考列；场数 &lt; 30 标 *；另列收盘覆盖率（stats 无该字段则「暂无」）。"""
    from . import model_lab as ml
    lab = ml.as_lab(lab)
    start, end = ml.week_range(end)
    board = [a for a in ml.board(lab.stats) if (a.get("picks") or 0) > 0]
    asof = ml.stats_time(lab.stats)
    head = (f"<b>【先知】🏆 模型周排行</b>\n📅 {start[5:]} – {end[5:]} 北京时间"
            + (f" · 统计截至 {asof}" if asof else "")
            + "\n🧪 实验盘，非正式推荐，不计入正式命中率\n📊 主指标：平均 CLV（ROI / 命中率仅供参考）")
    if not board:
        body = "暂无实验盘战绩"
    else:
        n = sum(a.get("settled") or 0 for a in board)
        rows_ = []
        for i, a in enumerate(board[:top], 1):
            settled = a.get("settled")
            rows_.append([
                str(i),
                ml.model_code(a["model_id"]),
                ml.MARKET_CN.get(a["market"], a["market"]),
                ml.n_label(settled if settled is not None else a.get("picks")),
                "暂无" if a.get("hit_rate") is None else f"{a['hit_rate']:.0%}",
                "暂无" if a.get("roi") is None else f"{a['roi']:+.0%}",
                "暂无" if a.get("clv_mean") is None else f"{a['clv_mean']:+.1%}",
                ml.coverage_text(a),
            ])
        skip = ml.skipped_text(lab.skipped)
        rej = ml.rejected_total(lab.stats)
        notes = []
        if skip:
            notes.append(skip)
        if rej:
            notes.append(f"拒绝 {rej} 行（配额/校验）")
        body = (f"📊 已结算 {n} 场 · {len(board)} 个模型×玩法\n"
                + (("；".join(notes) + "\n") if notes else "")
                + pre_table(["#", "模型", "玩法", "场", "命中", "ROI", "CLV", "收盘"], rows_, right_from=3)
                + "\n<i>* 样本 &lt; 30 场，排名波动大，仅供参考。按平均 CLV 排序（空值排最后）；"
                  "收盘 = 有 CLV 的已结算场次占比（stats 无该字段则暂无）。</i>")
    return split([head, DIV + "\n" + body, DIV + "\n" + "\n".join(footer())])


def summary(rep: dict, site_url: str | None = None, stats: dict | None = None) -> str:
    """兼容旧接口：日报 HTML（拼成一条）。"""
    return "\n\n".join(daily(rep, stats, site_url or SITE_URL))


def prematch_of(p: dict) -> dict:
    pm = p.get("prematch")
    if pm:
        return pm
    fc = p.get("final_check") or {}
    return {"checked_at": fc.get("checked_at"), "decision": fc.get("decision"), "reason": fc.get("reason"),
            "current_line": fc.get("current_line"), "sources": fc.get("sources", [])}


def prematch(p: dict, pm: dict | None = None, stats: dict | None = None) -> list[str]:
    pm = pm or prematch_of(p)
    dec = DECISION.get(pm.get("decision"), e(pm.get("decision") or "⏳ 暂无（尚未复核）"))
    q = dict(p)
    if pm.get("new_line") is not None:
        q["line"], q["odds"] = pm["new_line"], pm.get("new_odds")
    if pm.get("confidence"):
        q["confidence"] = pm["confidence"]
    when = f"\n🕐 复核 {bj(pm['checked_at'])} 北京时间" if pm.get("checked_at") else ""
    head = f"<b>【先知】🔎 赛前确认</b>\n决定：<b>{dec}</b>{when}"
    info = [f"📊 临场：{e(pm['current_line'])}"] if pm.get("current_line") else []
    if pm.get("reason"):
        info.append(f"📌 {e(pm['reason'])}")
    if "撤销" in dec:
        odds = f" @{wt(p['odds'])}" if p.get("odds") is not None else ""
        body = (match_head(p) + f"\n🎯 原推荐 <s>{e(_team(p))} {ln(p['line'])}{odds}</s>\n⛔ 已撤销，不计入命中率")
    else:
        body = compact_block({**q, "reasons": [], "risks": []}, stats, show_sources=False)
    if info:
        body += "\n\n" + sec("🔎", "临场复核") + "\n" + quote(info)
    srcs = pm.get("sources") or ([] if "撤销" in dec else p.get("sources"))
    if srcs:
        body += "\n\n" + "\n".join(sources_block(srcs[:4]))
    return split([head, DIV + "\n" + body, DIV + "\n" + "\n".join(footer())])


def backtest(p: dict, res: dict | None = None, stats: dict | None = None) -> list[str]:
    res = res or p.get("result") or {}
    s = res.get("settlement", "")
    cn = SETTLE_CN.get(s, res.get("settlement_cn") or s)
    units = res.get("units")
    line = res.get("line", p["line"]); odds = res.get("odds", p.get("odds"))  # noqa: E702
    score = res.get("score")
    body = [match_head(p, mid=score or "vs"),
            f"🎯 推荐 <b>{e(_team(p))} {ln(line)}" + (f" @{wt(odds)}" if odds is not None else "") + "</b>",
            (f"{SETTLE_ICON.get(s, '')} 结算 <b>{e(cn)}</b>" + (f" · <code>{float(units):+.2f}u</code>" if units is not None else ""))
            if s else "⏳ 结算：暂无（赛果未出）"]
    if res.get("review"):
        body += ["", sec("📝", "复盘"), quote([e(res["review"])], expandable=len(str(res["review"])) > 60)]
    if res.get("sources"):
        body += ["", *sources_block(res["sources"])]
    return split(["<b>【先知】🏁 赛后回测</b>", DIV + "\n" + "\n".join(body), DIV + "\n" + tail(stats)])


def pick_block(p: dict, i: int | None = None, stats: dict | None = None) -> str:
    return compact_block(p, stats)


def plain(msg: str) -> str:
    """HTML → 纯文本预览（Telegram 显示的文字）。"""
    return html.unescape(re.sub(r"<[^>]+>", "", msg))


def send(msgs: list[str], dry: bool = False) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID") or _default_chat()
    if dry or not (token and chat):
        print("\n\n-----\n\n".join(msgs))
        if not dry:
            print("[skip] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 未设置", file=sys.stderr)
        return
    ids = []
    for m in msgs:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": m, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=20)
        r.raise_for_status()
        ids.append((r.json().get("result") or {}).get("message_id"))
    print(f"sent {len(msgs)} message_id={ids}")


def _stats():
    from . import DATA
    sp = DATA / "stats.json"
    return json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else None


def _pick(rep: dict, mid: str) -> dict:
    for p in rep.get("picks", []):
        if p.get("match_id") == mid:
            return p
    raise SystemExit(f"{rep['date']} 报告中没有 {mid}")


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and re.match(r"\d{4}-\d{2}-\d{2}$", argv[0]):
        argv.insert(0, "daily")
    ap = argparse.ArgumentParser(prog="python -m ah telegram")
    ap.add_argument("kind", choices=["daily", "prematch", "backtest", "weekly"])
    ap.add_argument("date", nargs="?", help="日报日期；weekly 为周末日期（默认今天，北京时间）")
    ap.add_argument("match_id", nargs="?")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--announce-upgrade", action="store_true", help="日报开头加「🆕 看板已升级」与网站链接")
    ap.add_argument("--lab", help="实验盘 picks CSV（默认 data/experiment_picks.csv，不存在则省略该段）")
    ap.add_argument("--lab-stats", help="实验盘 stats JSON（默认同目录 experiment_stats.json）")
    a = ap.parse_args(argv)
    from . import model_lab
    lab = model_lab.load(a.lab, a.lab_stats)
    if a.kind == "weekly":
        send(weekly(lab, a.date), a.dry_run)
        return
    if not a.date:
        ap.error("需要日期 YYYY-MM-DD")
    rep = json.loads(report_paths(a.date)[0].read_text(encoding="utf-8"))
    if a.kind == "daily":
        msgs = daily(rep, _stats(), os.environ.get("SITE_URL") or SITE_URL,
                     lab=model_lab.for_date(lab.rows, a.date), announce=a.announce_upgrade)
    else:
        if not a.match_id:
            ap.error("prematch/backtest 需要 MATCH_ID")
        p = _pick(rep, a.match_id)
        msgs = prematch(p) if a.kind == "prematch" else backtest(p, stats=_stats())
    send(msgs, a.dry_run)


if __name__ == "__main__":
    main()
