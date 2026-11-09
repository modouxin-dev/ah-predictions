"""间歇期赛事扩展清单（DATA SOURCE 授权）的边界测试。

核心：只让真实比赛「被看见」，绝不放宽推荐口径、绝不编造盘口/状态。
"""
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from ah import CONFIG, daily
from ah.daily import collect_fixtures


class _Off:
    enabled = False


class _On:
    enabled = True
    def matches(self, a, b, c): return []


@pytest.fixture(autouse=True)
def _titan_on(monkeypatch):
    monkeypatch.setenv("AH_TITAN_FIXTURES", "1")


def _titan(mid, league, home, away, hhmm="20:00", state="0"):
    return {"match_id": f"titan007-{mid}", "league": league, "home": home,
            "away": away, "kickoff_bjt": hhmm, "state": state}


def _infer(mid, league, home, away, ko):
    return {"id": mid, "status": "scheduled", "league": {"name": league},
            "home_team": {"name": home}, "away_team": {"name": away}, "scheduled_at": ko}


def test_config_never_invents_provider_ids():
    """不知道的 league id 就留空，绝不编造 apif / football-data code。"""
    o = yaml.safe_load((CONFIG / "interim_leagues.yaml").read_text(encoding="utf-8"))
    entries = o["leagues"]
    assert len(entries) >= 5
    for e in entries:
        assert e["id"].startswith("interim_")
        assert "apif" not in e and "fd" not in e and "season" not in e
        assert e["tier"] == 3
        assert e.get("titan007") or e.get("infer")


def test_titan_interim_match_enters_watchlist_only(monkeypatch):
    now = datetime(2026, 11, 9, 4, 0, tzinfo=timezone.utc)   # 北京 12:00
    rows = [_titan("1", "国际友谊", "土耳其", "比利时", "21:00"),
            _titan("2", "英足总杯", "伯顿", "诺茨郡", "22:00"),
            _titan("3", "乌拉后备", "X队", "Y队", "20:00")]  # 后备队，明确排除
    monkeypatch.setattr(daily.fetch_fixtures, "fetch", lambda: rows)
    selected, errors, total = collect_fixtures(
        _Off(), _On(), now, hours=36, limit=32, apif=_Off(), papi=_Off(), fd5=_Off())
    by_home = {r["home"]: r for r in selected}
    assert "土耳其" in by_home and "伯顿" in by_home
    assert "X队" not in by_home
    for r in selected:
        assert r["list"] == "watchlist"          # 缺盘口/状态，绝不直接进推荐
        assert r["tier"] == 3
        assert not any(k in r for k in ("line", "odds", "price_ts", "home_water"))
    assert total == 2 and errors == []


def test_infer_interim_name_enters_pool(monkeypatch):
    now = datetime(2026, 11, 9, 4, 0, tzinfo=timezone.utc)
    ko = (now + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(daily.fetch_fixtures, "fetch", lambda: [])

    class Infer:
        enabled = True
        def events(self, day): return [_infer("77", "EngFLT", "A队", "B队", ko)] if day == now.date().isoformat() else []

    selected, errors, total = collect_fixtures(
        Infer(), _On(), now, hours=36, limit=32, apif=_Off(), papi=_Off(), fd5=_Off())
    assert total == 1
    assert selected[0]["league_id"] == "interim_infer_engflt"
    assert selected[0]["list"] == "watchlist"


def test_same_match_from_two_sources_is_not_duplicated(monkeypatch):
    """不同源用不同联赛 id 报同一场球：全局去重只留一条。"""
    now = datetime(2026, 11, 9, 4, 0, tzinfo=timezone.utc)
    ko_dt = now + timedelta(hours=5)
    ko = ko_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    bj_hhmm = (ko_dt + timedelta(hours=8)).strftime("%H:%M")
    monkeypatch.setattr(daily.fetch_fixtures, "fetch",
                        lambda: [_titan("1", "国际友谊", "A队", "B队", bj_hhmm)])

    class Infer:
        enabled = True
        def events(self, day): return [_infer("88", "Four Nations", "A队", "B队", ko)]

    selected, errors, total = collect_fixtures(
        Infer(), _On(), now, hours=36, limit=32, apif=_Off(), papi=_Off(), fd5=_Off())
    assert total == 1 and errors == []
    assert len([r for r in selected if r["home"] == "A队"]) == 1
