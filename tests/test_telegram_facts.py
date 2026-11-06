"""只显示推荐 + 中文 + 0 场如实兜底（2026-11-07 契约）。"""
import json
from pathlib import Path

from ah import publish_telegram as pt

REPORT = Path(__file__).resolve().parents[1] / "reports" / "2026" / "11" / "2026-11-07.json"


def _text(rep):
    return "\n".join(pt.plain(m) for m in pt.daily(rep))


def test_with_picks_shows_only_recommendations():
    rep = json.loads(REPORT.read_text(encoding="utf-8"))
    assert rep.get("picks"), "该日报应有正式推荐"
    text = _text(rep)
    assert "🎯 推荐" in text
    assert "规则档位，未经独立回测" in text          # 不得把档位写成准确率
    assert "赛程事实清单" not in text                # 有推荐时不混排赛程清单
    assert "比赛观察分析" not in text
    assert "模型实验盘" not in text
    assert "历史战绩" not in text
    assert "赛前已撤销" not in text


def test_team_names_are_chinese_when_translation_exists():
    rep = json.loads(REPORT.read_text(encoding="utf-8"))
    text = _text(rep)
    assert "卡利亚里" in text and "弗罗西诺内" in text       # 有公开译名依据
    assert "Cagliari Calcio vs Frosinone Calcio" not in text  # 原文不再直接混排
    assert "对阵" in text


def test_zero_pick_still_lists_real_fixtures():
    rep = {"date": "2026-11-07", "picks": [], "watched": [],
           "watchlist": [{"match_id": "x", "home": "A", "away": "B",
                          "league": "英超", "kickoff_utc": "2026-11-07T12:00:00Z",
                          "withheld_reason": "盘口跨来源核验未通过"}]}
    text = _text(rep)
    assert "推荐 0 场" in text
    assert "赛程事实清单" in text and "A vs B" in text and "状态：未推荐" in text
    assert "🎯" not in text                    # 0 场不得出现推荐卡片


def test_dry_run_does_not_require_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert pt.send(["hello"], dry=True) == []
