"""事实一致性闸门（fail-closed）。

只做核验，不生成任何数据：
- 推送文本中出现的队名 / 联赛 / 开球时间，必须能在报告 JSON 中逐字回溯；
- 空值必须写成「未取得」，不得出现 None/nan/推测措辞；
- 禁止无证据的送达 / 命中断言；
- 台账中 status=sent 必须带 message_id + sent_at + chat_id。

本模块不参与模型评分、盘口核验或预测生成。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import DATA, REPORTS
from .render_report import report_paths
from . import publish_telegram as pt

MISSING = pt.MISSING
LINE_RE = re.compile(r"^• (.+?)｜(.+?) vs (.+?)｜(.+?)｜状态：(.+?)｜原因：(.+?)$")

# 无证据不得使用的断言词
BANNED_ASSERTIONS = (
    "已成功推送", "推送成功", "已送达", "已验证推送", "推送完成并确认",
    "保证命中", "稳赢", "必定命中", "肯定收到",
)
# 推测性填充措辞
SPECULATIVE = ("预计", "应该是", "大概率是", "推测为", "据经验", "通常为")


def _items(report: dict) -> list[dict]:
    return [*(report.get("picks") or []), *(report.get("watched") or []),
            *(report.get("watchlist") or [])]


def check_fixture_lines(report: dict, lines: list[str]) -> list[str]:
    errors: list[str] = []
    allowed_teams = set()
    allowed_leagues = set()
    allowed_kickoffs = set()
    for w in _items(report):
        for k in ("home", "away"):
            if w.get(k):
                allowed_teams.add(str(w[k]))
        if w.get("league"):
            allowed_leagues.add(str(w["league"]))
        if w.get("kickoff_utc"):
            allowed_kickoffs.add(pt.bj(w["kickoff_utc"]))

    for line in lines:
        m = LINE_RE.match(line)
        if not m:
            errors.append(f"事实清单行不可核验格式：{line[:80]}")
            continue
        league, home, away, ko, _tag, reason = (x.strip() for x in m.groups())
        for label, value, allowed in (("队名", home, allowed_teams),
                                      ("队名", away, allowed_teams),
                                      ("联赛", league, allowed_leagues)):
            if value != MISSING and value not in allowed:
                errors.append(f"出现报告中不存在的{label}：{value}")
        if ko != MISSING:
            ko_value = ko.replace(" 北京时间", "").strip()
            if ko_value not in allowed_kickoffs:
                errors.append(f"出现报告中不存在的开球时间：{ko}")
        if any(x in line for x in ("None", "nan", "NaN", "null")):
            errors.append(f"事实清单行含未处理的空值：{line[:80]}")
        if any(x in reason for x in SPECULATIVE):
            errors.append(f"不推原因含推测措辞：{reason}")
    return errors


def check_text(report: dict, text: str) -> list[str]:
    errors: list[str] = []
    for word in BANNED_ASSERTIONS:
        if word in text:
            errors.append(f"无证据断言词：{word}")
    published = pt.policy.published(report)
    claimed = re.findall(r"推荐\s*(\d+)\s*场", text)
    for n in claimed:
        if int(n) != len(published):
            errors.append(f"文本声称推荐 {n} 场，报告实际可发布 {len(published)} 场")
    return errors


def check_ledger(path: Path | None = None) -> list[str]:
    p = path or (DATA / "telegram_delivery.json")
    errors: list[str] = []
    if not p.exists():
        return []  # 空台账是合法初始状态，不推测
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"台账无法解析：{type(exc).__name__}"]
    for i, row in enumerate(d.get("deliveries") or []):
        if row.get("status") == "sent":
            if not row.get("message_ids"):
                errors.append(f"台账第 {i} 条标记 sent 但没有 message_ids")
            if not row.get("sent_at"):
                errors.append(f"台账第 {i} 条标记 sent 但没有 sent_at")
            if not row.get("chat_id"):
                errors.append(f"台账第 {i} 条标记 sent 但没有 chat_id")
    return errors


def check_all(report: dict, text: str | None = None, ledger: Path | None = None) -> dict:
    lines = pt.fixture_facts_lines(report)
    rendered = text if text is not None else "\n".join(pt.plain(m) for m in pt.daily(report))
    errors = []
    errors += check_fixture_lines(report, lines)
    errors += check_text(report, rendered)
    errors += check_ledger(ledger)
    return {
        "ok": not errors,
        "errors": errors,
        "checked": {
            "report_date": report.get("date"),
            "picks": len(report.get("picks") or []),
            "watchlist": len(report.get("watchlist") or []),
            "fixture_lines": len(lines),
            "evidence": "reports/**.json + publish_telegram.fixture_facts_lines + data/telegram_delivery.json",
        },
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="事实一致性闸门；失败则退出码 1（拒绝发送）")
    ap.add_argument("--report", help="报告 JSON 路径；默认当天 UTC 日报")
    ap.add_argument("--text", help="待核验的推送纯文本文件路径；默认渲染当天日报")
    ap.add_argument("--ledger", help="台账路径；默认 data/telegram_delivery.json")
    args = ap.parse_args(argv)

    if args.report:
        rp = Path(args.report)
    else:
        d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rp, _ = report_paths(d)
    if not rp.exists():
        print(f"GROUNDING_FAIL 报告不存在：{rp}", file=sys.stderr)
        raise SystemExit(1)
    try:
        report = json.loads(rp.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"GROUNDING_FAIL 报告解析失败：{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)

    text = None
    if args.text:
        tp = Path(args.text)
        if not tp.exists():
            print(f"GROUNDING_FAIL 文本不存在：{tp}", file=sys.stderr)
            raise SystemExit(1)
        text = tp.read_text(encoding="utf-8")

    result = check_all(report, text, Path(args.ledger) if args.ledger else None)
    if not result["ok"]:
        print("GROUNDING_FAIL " + "；".join(result["errors"][:8]), file=sys.stderr)
        raise SystemExit(1)
    c = result["checked"]
    print(f"GROUNDING_OK date={c['report_date']} picks={c['picks']} "
          f"watchlist={c['watchlist']} fixture_lines={c['fixture_lines']} "
          f"evidence={c['evidence']}")


if __name__ == "__main__":
    main()
