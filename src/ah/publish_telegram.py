"""先知 Telegram 推送（HTML parse_mode）：日报 / 赛前确认 / 赛后回测。

用法（需 TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID；未设置或 --dry-run 只打印）::

  python -m ah telegram YYYY-MM-DD [--dry-run]                        # 日报（兼容旧用法）
  python -m ah telegram daily YYYY-MM-DD [--dry-run]
  python -m ah telegram prematch YYYY-MM-DD MATCH_ID [--dry-run]      # 读 pick.prematch（或旧 final_check）
  python -m ah telegram backtest YYYY-MM-DD MATCH_ID [--dry-run]      # 读 pick.result
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


def split(blocks: list[str], limit: int = LIMIT) -> list[str]:
    """按块拼接，不超过 limit；单块过长时按行切。"""
    msgs, cur = [], ""
    pieces = []
    for b in blocks:
        if len(b) <= limit:
            pieces.append(b)
        else:
            chunk = ""
            for line in b.split("\n"):
                while len(line) > limit:
                    if chunk:
                        pieces.append(chunk); chunk = ""  # noqa: E702
                    pieces.append(line[:limit]); line = line[limit:]  # noqa: E702
                if chunk and len(chunk) + 1 + len(line) > limit:
                    pieces.append(chunk); chunk = ""  # noqa: E702
                chunk = f"{chunk}\n{line}" if chunk else line
            if chunk:
                pieces.append(chunk)
    for b in pieces:
        if cur and len(cur) + 2 + len(b) > limit:
            msgs.append(cur); cur = ""  # noqa: E702
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


def verdict(p: dict) -> str:
    conf = CONF.get(p.get("confidence"), str(p.get("confidence")))
    odds = f" @{p['odds']}" if p.get("odds") is not None else ""
    return f"🎯 <b>推荐 {e(_team(p))} {ln(p['line'])}{odds} · {CONF_ICON.get(conf, '')}{e(conf)}</b>"


def venue(p: dict) -> str | None:
    v = p.get("venue") or (p.get("fixture") or {}).get("venue")
    if isinstance(v, dict):
        v = "，".join(x for x in (v.get("name"), v.get("city")) if x)
    return v or None


def compact_block(p: dict, stats: dict | None = None, extra: list[str] | None = None) -> str:
    conf = CONF.get(p.get("confidence"), str(p.get("confidence")))
    lines = [f"🏆 {e(p.get('league', ''))} ｜ 🕐 {bj(p.get('kickoff_utc'))}",
             f"<b>{e(p['home'])} vs {e(p['away'])}</b>", verdict(p), *(extra or []),
             pre_table(["机构", "盘口", "水位"], comparison_rows(p))]
    dv = deviation(p)
    if dv:
        lines.append(f"📊 {dv}")
    if venue(p):
        lines.append(f"🏟 {e(venue(p))}")
    probs = p.get("model_probs")
    if probs:
        lines.append("🧮 模型概率 " + " ".join(f"{e(k)} {float(v):.0%}" for k, v in probs.items()))
    if p.get("reasons"):
        lines += ["✅ <b>为什么推</b>", *[f"• {e(short(r))}" for r in p["reasons"][:3]]]
    if p.get("risks"):
        lines.append("⚠️ " + e("；".join(short(r, 30) for r in p["risks"][:2])))
    s = stats or {}
    lg = (s.get("by_league") or {}).get(p.get("league"))
    cf = (s.get("by_confidence") or {}).get(conf)
    lines.append(f"📈 本联赛 {record_of(lg)} ｜ {e(conf)}信心 {record_of(cf)}")
    if p.get("sources"):
        lines.append("🔗 " + links(p["sources"][:4]))
    return "\n".join(lines)


def league_short(k: str) -> str:
    m = re.search(r"（([^ （）·]+)", k)
    return m.group(1) if m else k.split("（")[0]


def stats_table(stats: dict | None, top: int = 3) -> str:
    s = stats or {}
    o = s.get("overall") or {}
    if not o.get("decided"):
        return "📈 <b>历史战绩</b>：暂无已结算推荐"

    def row(name, d):
        return [name[:10], d.get("picks", 0), f"{d.get('win', 0)}+{d.get('half_win', 0)}" if d.get("half_win") else d.get("win", 0),
                d.get("push", 0), d.get("loss", 0) + d.get("half_loss", 0), f"{d.get('hit_rate', 0):.0%}", f"{d.get('units', 0):+.1f}"]
    rows = [row("总体", o)]
    hi = (s.get("by_confidence") or {}).get("高")
    if hi and hi.get("picks"):
        rows.append(row("高信心", hi))
    bl = sorted((s.get("by_league") or {}).items(), key=lambda kv: -kv[1].get("picks", 0))[:top]
    rows += [row(league_short(k), v) for k, v in bl]
    return "📈 <b>历史战绩</b>\n" + pre_table(["", "场次", "赢", "走", "输", "命中", "盈亏"], rows)


def tail(stats: dict | None, site_url: str = SITE_URL) -> str:
    return (stats_table(stats) + f'\n🌐 <a href="{html.escape(site_url, quote=True)}">完整看板 · 先知</a>\n\n'
            + "\n".join(footer()))


def date_line(rep: dict, n_pub: int) -> str:
    d = datetime.strptime(rep["date"], "%Y-%m-%d")
    n_all = len(rep.get("picks") or []) + len(rep.get("watched") or []) + len(rep.get("watchlist") or [])
    return f"📅 {d:%m-%d} 周{WEEK[d.weekday()]} · 筛选 {n_all} 场 · 推荐 {n_pub} 场"


def daily(rep: dict, stats: dict | None = None, site_url: str = SITE_URL) -> list[str]:
    picks = policy.published(rep)
    blocks = [f"<b>【先知】⚽ 亚盘日报 {e(rep['date'])}</b>\n{date_line(rep, len(picks))}"]
    if not picks:
        none = "今日无高信心推荐（宁缺毋滥）"
        if rep.get("no_pick_reason"):
            none += "\n📌 " + e(rep["no_pick_reason"])
        blocks.append(none)
    blocks += [DIV + "\n" + compact_block(p, stats) for p in picks]
    blocks.append(DIV + "\n" + tail(stats, site_url))
    return split(blocks)


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
    dec = DECISION.get(pm.get("decision"), e(pm.get("decision") or "—"))
    q = dict(p)
    if pm.get("new_line") is not None:
        q["line"], q["odds"] = pm["new_line"], pm.get("new_odds")
    if pm.get("confidence"):
        q["confidence"] = pm["confidence"]
    when = f" · {bj(pm['checked_at'])} 北京时间" if pm.get("checked_at") else ""
    head = f"<b>【先知】🔎 赛前确认</b>\n决定：<b>{dec}</b>{when}"
    info = [f"📊 临场：{e(pm['current_line'])}"] if pm.get("current_line") else []
    info.append(f"📌 {e(pm.get('reason') or '—')}")
    if "撤销" in dec:
        body = (f"🏆 {e(p.get('league', ''))} ｜ 🕐 {bj(p.get('kickoff_utc'))}\n<b>{e(p['home'])} vs {e(p['away'])}</b>\n"
                f"🎯 原推荐 <s>{e(_team(p))} {ln(p['line'])}" + (f" @{p['odds']}" if p.get('odds') is not None else "")
                + "</s>\n🔥 已撤销，不计入命中率\n" + "\n".join(info))
    else:
        body = compact_block({**q, "reasons": [], "risks": []}, stats, extra=None) + "\n" + "\n".join(info)
    if pm.get("sources"):
        body += "\n🔗 " + links(pm["sources"])
    return split([head, DIV + "\n" + body, DIV + "\n" + "\n".join(footer())])


def backtest(p: dict, res: dict | None = None, stats: dict | None = None) -> list[str]:
    res = res or p.get("result") or {}
    s = res.get("settlement", "")
    cn = SETTLE_CN.get(s, res.get("settlement_cn") or s)
    units = res.get("units")
    u = f"{float(units):+.2f}u" if units is not None else "—"
    line = res.get("line", p["line"]); odds = res.get("odds", p.get("odds"))  # noqa: E702
    body = [f"🏆 {e(p.get('league', ''))} ｜ 🕐 {bj(p.get('kickoff_utc'))}",
            f"<b>{e(p['home'])} {e(res.get('score') or '—')} {e(p['away'])}</b>",
            f"🎯 推荐 {e(_team(p))} {ln(line)}" + (f" @{odds}" if odds is not None else ""),
            f"{SETTLE_ICON.get(s, '')} 结算 <b>{e(cn)}</b> · {u}",
            f"📌 {e(res.get('review') or '—')}"]
    if res.get("sources"):
        body.append("🔗 " + links(res["sources"]))
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
    ap.add_argument("kind", choices=["daily", "prematch", "backtest"])
    ap.add_argument("date")
    ap.add_argument("match_id", nargs="?")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    rep = json.loads(report_paths(a.date)[0].read_text(encoding="utf-8"))
    if a.kind == "daily":
        msgs = daily(rep, _stats(), os.environ.get("SITE_URL") or SITE_URL)
    else:
        if not a.match_id:
            ap.error("prematch/backtest 需要 MATCH_ID")
        p = _pick(rep, a.match_id)
        msgs = prematch(p) if a.kind == "prematch" else backtest(p, stats=_stats())
    send(msgs, a.dry_run)


if __name__ == "__main__":
    main()
