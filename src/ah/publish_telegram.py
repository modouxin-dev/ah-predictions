"""发送当日推荐摘要到 Telegram。需要环境变量 TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID；未设置则跳过。"""
from __future__ import annotations

import argparse
import json
import os


def _default_chat():
    try:
        import yaml
        from pathlib import Path
        cfg = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "settings.yaml").read_text())
        return str((cfg.get("telegram") or {}).get("chat_id") or "") or None
    except Exception:
        return None
import sys

import requests

from . import policy
from .render_report import report_paths

CONF = {"high": "高", "medium": "中", "low": "低"}


def summary(rep: dict, site_url: str | None = None, stats: dict | None = None) -> str:
    lines = [f"【先知】⚽ 亚盘日报 {rep['date']}", ""]
    picks = policy.published(rep)
    if not picks:
        lines.append("今日无高信心推荐（宁缺毋滥）。")
    for i, p in enumerate(picks, 1):
        team = p.get("pick_team", p[p["pick_side"]])
        odds = f" @{p['odds']}" if p.get("odds") is not None else ""
        lines.append(f"{i}. {p['home']} vs {p['away']}（{p['kickoff_utc'][11:16]} UTC）")
        lines.append(f"   ➜ {team} {p['line']:+g}{odds} · 信心{CONF[p['confidence']]}")
    lines.append(f"\n观察未推：{len(rep['watched'])} 场")
    if site_url:
        lines.append(f"完整报告：{site_url}")
    if stats is not None:
        from .stats import summary_line
        lines.append(summary_line(stats))
    lines.append(policy.DISCLAIMER)
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("date")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    rep = json.loads(report_paths(a.date)[0].read_text(encoding="utf-8"))
    from . import DATA
    sp = DATA / "stats.json"
    st = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else None
    text = summary(rep, os.environ.get("SITE_URL"), st)
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID") or _default_chat()
    if a.dry_run or not (token and chat):
        print(text)
        if not a.dry_run:
            print("[skip] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 未设置", file=sys.stderr)
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=20)
    r.raise_for_status()
    print("sent")


if __name__ == "__main__":
    main()
