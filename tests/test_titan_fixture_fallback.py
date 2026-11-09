"""roles.fixtures 声明的 titan007 备用赛程源必须真正参与赛程发现。"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from ah import daily


class _Off:
    enabled = False


def _row(mid, league, home, away, hhmm="20:00", state="0"):
    return {"match_id": f"titan007-{mid}", "league": league, "home": home,
            "away": away, "kickoff_bjt": hhmm, "state": state}


def test_titan_fixtures_enter_pool_when_primary_sources_empty():
    now = datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc)   # 北京 11-08 09:00
    rows = [_row("1", "英超", "A队", "B队", "22:00"),
            _row("2", "日职联", "C队", "D队", "13:00"),
            _row("3", "某小联赛", "E队", "F队", "12:00"),   # 未配置，不入池
            _row("4", "西甲", "G队", "H队", "23:00", state="-1")]  # 已完场，不入池
    with patch.object(daily.fetch_fixtures, "fetch", return_value=rows):
        selected, errors, total = daily.collect_fixtures(
            _Off(), _Off(), now, hours=36, limit=32, apif=_Off(), papi=_Off(), fd5=_Off())
    ids = {r["match_id"] for r in selected}
    assert "titan007-1" in ids and "titan007-2" in ids
    assert "titan007-3" not in ids and "titan007-4" not in ids
    assert total == 2 and errors == []
    sc = daily.collect_fixtures.source_counts["titan007_fixtures"]
    assert sc["raw"] == 4 and sc["added"] == 2
    assert all(r["league_id"] in {"epl", "j1"} for r in selected)


def test_titan_is_the_only_available_source_when_primaries_are_down():
    """主源全挂、备用源有数据：备用源顶上，不再抛错。"""
    now = datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc)
    with patch.object(daily.fetch_fixtures, "fetch", return_value=[_row("1", "英超", "A", "B")]):
        rows, errors, total = daily.collect_fixtures(
            _Off(), _Off(), now, hours=36, limit=32, apif=_Off(), papi=_Off(), fd5=_Off())
    assert total == 1 and rows[0]["league_id"] == "epl" and errors == []


def test_titan_empty_does_not_mask_all_sources_down():
    """备用源返回 0 行时，仍触发「没有可用的赛程数据源」保护，不静默出 0 场。"""
    now = datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc)
    with patch.object(daily.fetch_fixtures, "fetch", return_value=[]):
        try:
            daily.collect_fixtures(_Off(), _Off(), now, hours=36, limit=32,
                                   apif=_Off(), papi=_Off(), fd5=_Off())
            assert False, "应抛错"
        except RuntimeError as e:
            assert "没有可用的赛程数据源" in str(e)


def test_titan_failure_is_recorded_as_fact_only(monkeypatch):
    now = datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc)
    def boom(): raise ConnectionError("blocked")
    monkeypatch.setattr(daily.fetch_fixtures, "fetch", boom)
    # 用一个 enabled 的假主源让 succeeded>0，避免上面的保护干扰本用例
    class _On:
        enabled = True
        def matches(self, a, b, c): return []
    rows, errors, total = daily.collect_fixtures(
        _Off(), _On(), now, hours=36, limit=32, apif=_Off(), papi=_Off(), fd5=_Off())
    assert rows == [] and total == 0 and errors == []
    assert any("titan007 赛程: ConnectionError" in n for n in daily.collect_fixtures.notes)
