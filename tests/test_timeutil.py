"""工程层：所有模块引用同一个北京时区真源。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ah import timeutil
from ah import build_site, match, model_lab, tg_bot
from ah.scrapers import common as scraper_common
from ah.audit import paper_trades


def test_single_source_of_truth():
    assert timeutil.BJ is timeutil.BJT
    assert match.BJT is timeutil.BJT
    assert scraper_common.BJT is timeutil.BJT
    assert model_lab.BJ is timeutil.BJT
    assert tg_bot.BJ is timeutil.BJT
    assert paper_trades.BJ is timeutil.BJT
    assert build_site.BJT is timeutil.BJT


def test_bj_date_crosses_utc_midnight():
    assert timeutil.bj_date(datetime(2026, 11, 7, 16, 30, tzinfo=timezone.utc)) == "2026-11-08"
    assert timeutil.bj_date(datetime(2026, 11, 7, 15, 30, tzinfo=timezone.utc)) == "2026-11-07"


def test_to_bj_and_utc_now_iso():
    dt = datetime(2026, 11, 8, 14, 0, tzinfo=timezone.utc)
    assert timeutil.to_bj(dt) == "11-08 22:00"
    iso = timeutil.utc_now_iso()
    assert iso.endswith("Z") and len(iso) == 20


def test_delivery_bj_date_uses_shared_module(monkeypatch):
    from ah import delivery
    import ah.delivery as d
    fixed = datetime(2026, 11, 6, 23, 5, tzinfo=timezone.utc)

    class FakeDT:
        @staticmethod
        def now(tz=None):
            return fixed
    monkeypatch.setattr(d, "datetime", FakeDT)
    assert delivery._bj_date() == "2026-11-07"
