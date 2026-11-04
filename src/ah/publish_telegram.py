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


def pick_block(p: dict, i: int | None = None) -> str:
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


def daily(rep: dict, stats: dict | None = None, site_url: str = SITE_URL) -> list[str]:
    picks = policy.published(rep)
    blocks = [f"<b>【先知】⚽ 亚盘日报 {e(rep['date'])}</b>\n{history(stats)}"]
    if not picks:
        blocks.append("今日无高信心推荐（宁缺毋滥）")
    blocks += [pick_block(p, i if len(picks) > 1 else None) for i, p in enumerate(picks, 1)]
    blocks.append(f'🌐 <a href="{html.escape(site_url, quote=True)}">完整看板</a>\n\n' + "\n".join(footer()))
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


def prematch(p: dict, pm: dict | None = None) -> list[str]:
    pm = pm or prematch_of(p)
    dec = DECISION.get(pm.get("decision"), e(pm.get("decision") or "—"))
    line, odds = pm.get("new_line"), pm.get("new_odds")
    if line is None:
        line, odds = p["line"], p.get("odds")
    conf = CONF.get(pm.get("confidence") or p.get("confidence"), "—")
    rec = f"{e(_team(p))} {ln(line)}" + (f" @ {odds}" if odds is not None else "")
    lines = ["<b>【先知】🔎 赛前确认</b>", *match_block(p), "",
             f"决定：<b>{dec}</b>"]
    if "撤销" in dec:
        lines.append(f"🎯 原推荐：<s>{e(_team(p))} {ln(p['line'])}" + (f" @ {p['odds']}" if p.get('odds') is not None else "") + "</s>")
    else:
        lines.append(f"🎯 <b>{rec}</b>")
    if pm.get("current_line"):
        lines.append(f"📊 临场盘口：{e(pm['current_line'])}")
    lines += [f"🔥 信心：{CONF_ICON.get(conf, '')} <b>{e(conf)}</b>" if "撤销" not in dec else "🔥 信心：— （已撤销，不计入命中率）",
              "", f"📌 {e(pm.get('reason') or '—')}"]
    if pm.get("checked_at"):
        lines.append(f"🕐 检查时间 {bj(pm['checked_at'])} 北京时间")
    if pm.get("sources"):
        lines.append("🔗 " + links(pm["sources"]))
    return split(["\n".join(lines), "\n".join(footer())])


def backtest(p: dict, res: dict | None = None, stats: dict | None = None) -> list[str]:
    res = res or p.get("result") or {}
    s = res.get("settlement", "")
    cn = SETTLE_CN.get(s, res.get("settlement_cn") or s)
    units = res.get("units")
    u = f"{float(units):+.2f} 单位" if units is not None else "—"
    line = res.get("line", p["line"]); odds = res.get("odds", p.get("odds"))  # noqa: E702
    lines = ["<b>【先知】🏁 赛后回测</b>", *match_block(p), "",
             f"⚽ 全场比分：<b>{e(res.get('score') or '—')}</b>",
             f"🎯 推荐：{e(_team(p))} {ln(line)}" + (f" @ {odds}" if odds is not None else ""),
             f"{SETTLE_ICON.get(s, '')} 结算：<b>{e(cn)}</b>（{u}）", "",
             f"📌 复盘：{e(res.get('review') or '—')}"]
    if res.get("sources"):
        lines.append("🔗 " + links(res["sources"]))
    lines += ["", history(stats)]
    return split(["\n".join(lines), "\n".join(footer())])


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
    for m in msgs:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": m, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=20)
        r.raise_for_status()
    print(f"sent {len(msgs)}")


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
