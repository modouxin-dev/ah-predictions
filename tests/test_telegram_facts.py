"""推送必须列出报告中真实存在的赛程，且不得生成任何编造内容。

不硬编码场次数：日报每天 --refresh 重采，picks / watchlist 会变化。
"""
import json
import re
from pathlib import Path

from ah import publish_telegram as pt

REPORT = Path(__file__).resolve().parents[1] / "reports" / "2026" / "11" / "2026-11-06.json"


def _plain_text(rep):
    return "\n".join(pt.plain(m) for m in pt.daily(rep))


def _items(rep):
    return list(rep.get("picks") or []) + list(rep.get("watched") or []) + list(rep.get("watchlist") or [])


def test_report_with_or_without_picks_shows_real_fixtures():
    rep = json.loads(REPORT.read_text(encoding="utf-8"))
    text = _plain_text(rep)
    assert "赛程事实清单" in text
    assert "非推荐、不含预测结论" in text
    # 清单里至少出现一条报告 JSON 中真实存在的对阵
    shown = [w for w in _items(rep)
             if f"{w.get('home')} vs {w.get('away')}" in text]
    assert shown, "推送正文没有展示任何报告里真实存在的对阵"


def test_claimed_pick_count_matches_real_publishable_picks():
    rep = json.loads(REPORT.read_text(encoding="utf-8"))
    text = _plain_text(rep)
    real = len(pt.policy.published(rep))
    for n in re.findall(r"推荐\s*(\d+)\s*场", text):
        assert int(n) == real, f"文本声称 {n} 场，实际可发布 {real} 场"
    assert "虚构" not in text
    if real == 0:
        assert "信心等级" not in text      # 0 场时不得出现推荐详情


def test_facts_lines_are_traceable_to_report():
    rep = json.loads(REPORT.read_text(encoding="utf-8"))
    lines = pt.fixture_facts_lines(rep)
    assert lines and len(lines) <= min(20, len(_items(rep)))
    teams = {str(w.get(k)) for w in _items(rep)
             for k in ("home", "away") if w.get(k)}
    for line in lines:
        home, away = line.split("｜")[1].replace(" vs ", "\x00").split("\x00")
        assert home in teams and away in teams


def test_dry_run_does_not_require_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert pt.send(["hello"], dry=True) == []
