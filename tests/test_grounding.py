"""防编造 / 防断言闸门测试。"""
import json
from pathlib import Path

from ah import grounding, publish_telegram as pt

REPORT = Path(__file__).resolve().parents[1] / "reports" / "2026" / "11" / "2026-11-06.json"


def test_real_zero_pick_report_passes_gate():
    rep = json.loads(REPORT.read_text(encoding="utf-8"))
    result = grounding.check_all(rep, text="\n".join(pt.plain(m) for m in pt.daily(rep)),
                                 ledger=Path("/nonexistent/ledger.json"))
    assert result["ok"], result["errors"]
    assert result["checked"]["picks"] == 0
    assert result["checked"]["fixture_lines"] == 12


def test_invented_team_is_rejected():
    rep = json.loads(REPORT.read_text(encoding="utf-8"))
    fake = ["• 英超｜虚构队 vs 另一虚构队｜11-06 22:15 北京时间｜状态：未推荐｜原因：盘口跨来源核验未通过"]
    errors = grounding.check_fixture_lines(rep, fake)
    assert any("不存在" in e for e in errors)


def test_speculative_reason_is_rejected():
    rep = json.loads(REPORT.read_text(encoding="utf-8"))
    bad = pt.fixture_facts_lines(rep)[0].replace(
        "盘口跨来源核验未通过", "大概率是盘口没对齐")
    assert any("推测" in e for e in grounding.check_fixture_lines(rep, [bad]))


def test_unevidence_assertion_is_rejected():
    rep = {"date": "2026-11-06", "picks": [], "watched": [], "watchlist": []}
    errors = grounding.check_text(rep, "推荐 0 场\n已成功推送到群")
    assert any("断言" in e for e in errors)


def test_claimed_pick_count_must_match_report():
    rep = {"date": "2026-11-06", "picks": [], "watched": [], "watchlist": []}
    errors = grounding.check_text(rep, "📅 筛选 12 场 · 推荐 3 场")
    assert any("声称推荐" in e for e in errors)


def test_ledger_sent_without_message_ids_is_rejected(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"deliveries": [{"status": "sent", "date": "2026-11-06"}]}),
                 encoding="utf-8")
    errors = grounding.check_ledger(p)
    assert any("message_ids" in e for e in errors)


def test_missing_fields_are_marked_not_invented():
    rep = {"date": "2026-11-06", "picks": [], "watched": [],
           "watchlist": [{"match_id": "x", "home": "主队", "away": "客队",
                          "withheld_reason": None}]}
    line = pt.fixture_facts_lines(rep)[0]
    assert pt.MISSING in line
    result = grounding.check_all(rep, text="推荐 0 场", ledger=Path("/nonexistent/ledger.json"))
    assert result["ok"], result["errors"]
