"""0 场推荐时，推送必须列出报告中真实存在的赛程，且不得生成任何预测。"""
import json
from pathlib import Path

from ah import publish_telegram as pt

REPORT = Path(__file__).resolve().parents[1] / "reports" / "2026" / "11" / "2026-11-06.json"


def _plain_text(rep):
    return "\n".join(pt.plain(m) for m in pt.daily(rep))


def test_zero_pick_report_shows_real_fixtures():
    rep = json.loads(REPORT.read_text(encoding="utf-8"))
    text = _plain_text(rep)
    assert "推荐 0 场" in text
    assert "赛程事实清单" in text
    assert "Al Arabi Doha vs Ahli Doha" in text          # 报告 watchlist 中真实存在
    assert "Everton FC vs Coventry City FC" in text      # 报告 watchlist 中真实存在
    assert "非推荐、不含预测结论" in text


def test_facts_block_never_invents_picks():
    rep = json.loads(REPORT.read_text(encoding="utf-8"))
    text = _plain_text(rep)
    assert "推荐 1 场" not in text
    assert "信心等级" not in text       # 0 场时不得出现推荐详情
    assert "虚构" not in text


def test_facts_lines_are_traceable_to_report():
    rep = json.loads(REPORT.read_text(encoding="utf-8"))
    lines = pt.fixture_facts_lines(rep)
    assert len(lines) == 12
    teams = {str(w.get(k)) for w in (rep["picks"] + rep["watched"] + rep["watchlist"])
             for k in ("home", "away") if w.get(k)}
    for line in lines:
        home, away = line.split("｜")[1].replace(" vs ", "\x00").split("\x00")
        assert home in teams and away in teams


def test_dry_run_does_not_require_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert pt.send(["hello"], dry=True) == []
