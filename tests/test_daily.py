from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ah import daily

UTC = timezone.utc
NOW = datetime(2026, 11, 6, 8, tzinfo=UTC)


def fixture():
    return {'match_id': 'm1', 'league_id': 'epl', 'league': '英超', 'home': 'Everton',
            'away': 'Coventry', 'kickoff_utc': '2026-11-06T20:00:00Z'}


def quotes(now=NOW):
    def row(source, book, verification='single', stage='report', line=-0.75):
        return {'match_id': 'm1', 'stage': stage, 'source': source, 'bookmaker': book,
                'is_main': '1', 'verification': verification, 'ah_home_line': str(line),
                'odds_home': '1.91', 'odds_away': '1.91',
                'captured_at': now.isoformat(), 'price_ts': now.isoformat()}
    return [row('oddspapi', 'pinnacle', 'verified'), row('odds_api', 'pinnacle', 'verified'),
            row('odds_api', 'matchbook'), row('infersports', 'crown'),
            row('oddspapi', 'pinnacle', stage='open', line=-0.25)]


CFG = {'min_sources': {'report': 2}, 'min_bookmakers_on_main_line': {'report': 3},
       'min_verified_books': {'report': 1}, 'stale_s': {'report': {'downgrade': 1800, 'withdraw': 7200}}}
FORM = {'form': {'home': 'WWWWW', 'away': 'LLLLL'}}


def test_verified_quote_with_form_can_produce_pick():
    pick, why = daily.assess(fixture(), quotes(), FORM, CFG, NOW)
    assert not why
    assert pick['pick_side'] == 'home'
    assert pick['line'] == -0.75 and pick['line_open'] == -0.25
    assert pick['odds'] == pytest.approx(0.91)
    assert '未经独立回测' in pick['risks'][0]


def test_unverified_stale_or_missing_fundamentals_are_not_recommended():
    rows = quotes()
    for row in rows:
        row['verification'] = 'single'
    assert daily.assess(fixture(), rows, FORM, CFG, NOW)[0] is None
    assert daily.assess(fixture(), quotes(), None, CFG, NOW)[0] is None
    assert daily.assess(fixture(), quotes(), FORM, CFG, NOW + timedelta(hours=3))[0] is None
    assert daily.assess(fixture(), quotes()[:-1], FORM, CFG, NOW)[0] is None


def test_fixtures_are_deduplicated_and_limited_to_upcoming_window():
    class FD:
        enabled = True
        def matches(self, *args):
            return [{'id': 123, 'competition': {'code': 'PL'}, 'status': 'TIMED',
                     'utcDate': '2026-11-06T20:00:00Z',
                     'homeTeam': {'name': 'Everton'}, 'awayTeam': {'name': 'Coventry'}}]
    class Infer:
        enabled = True
        def events(self, day):
            if day != '2026-11-06':
                return []
            return [{'id': 'e1', 'status': 'scheduled', 'league': {'name': 'ENPL'},
                     'scheduled_at': '2026-11-06T20:00:00Z',
                     'home_team': {'name': 'Everton'}, 'away_team': {'name': 'Coventry'}}]
    rows, errors, total = daily.collect_fixtures(Infer(), FD(), NOW)
    assert not errors and total == 1
    assert len(rows) == 1 and rows[0]['match_id'] == 'fd-123'


def test_does_not_replace_existing_report(tmp_path, monkeypatch):
    report = tmp_path / 'existing.json'
    report.write_text('existing', encoding='utf-8')
    monkeypatch.setattr(daily, 'report_paths', lambda _: (report, Path('unused')))
    monkeypatch.setattr(daily, 'build_report', lambda *args, **kwargs: pytest.fail('must not collect'))
    daily.main(['--date', '2026-11-06'])
    assert report.read_text(encoding='utf-8') == 'existing'


def test_all_fixture_sources_fail():
    class Down:
        enabled = True
        def matches(self, *args):
            raise RuntimeError('unavailable')
        def events(self, *args):
            raise RuntimeError('unavailable')
    with pytest.raises(RuntimeError, match='没有可用'):
        daily.collect_fixtures(Down(), Down(), NOW)


def test_snapshot_network_error_does_not_store_credentials(tmp_path):
    class FailingPapi:
        def historical_odds(self, *_args, **_kwargs):
            raise RuntimeError('https://provider.invalid/?apiKey=PRIVATE-KEY')

    snap = daily.odds_snapshots.Snapshotter('2026-11-06', papi=FailingPapi(),
                                           snap_dir=tmp_path, ah_markets={}, now=lambda: NOW)
    snap.map['m1'] = {'oddspapi': 'fixture-id'}
    _, reasons = snap._rows_oddspapi(fixture(), ['report'], NOW, NOW, False)
    assert reasons and all(reason == 'oddspapi:historical_error(RuntimeError)' for reason in reasons)
