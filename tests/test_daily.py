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


def test_backup_form_is_attributed_to_actual_provider():
    observation = {}
    fallback = {**FORM, 'source': '5DollarFootballAPI'}
    pick, why = daily.assess(fixture(), quotes(), fallback, CFG, NOW, observation=observation)
    assert not why and 'https://5dollarfootballapi.com/' in pick['sources']
    assert 'https://www.football-data.org/' not in pick['sources']
    assert 'Football data by 5DollarFootballAPI' in pick['reasons']
    assert observation['form_source'] == '5DollarFootballAPI'


@pytest.mark.parametrize('source,url', [('api_football', 'https://www.api-football.com/'),
                                        ('infersports', 'https://infersports.dev/')])
def test_asian_form_sources_are_not_misattributed(source, url):
    pick, why = daily.assess(fixture(), quotes(), {**FORM, 'source': source}, CFG, NOW)
    assert not why and url in pick['sources']
    assert 'https://www.football-data.org/' not in pick['sources']


def test_unknown_form_source_is_not_presented_as_verified():
    pick, why = daily.assess(fixture(), quotes(), {**FORM, 'source': 'unknown'}, CFG, NOW)
    assert pick is None and why == '近期状态来源未识别'


def test_unverified_stale_or_missing_fundamentals_are_not_recommended():
    rows = quotes()
    for row in rows:
        row['verification'] = 'single'
    assert daily.assess(fixture(), rows, FORM, CFG, NOW)[0] is None
    assert daily.assess(fixture(), quotes(), None, CFG, NOW)[0] is None
    assert daily.assess(fixture(), quotes(), FORM, CFG, NOW + timedelta(hours=3))[0] is None
    assert daily.assess(fixture(), quotes()[:-1], FORM, CFG, NOW)[0] is None


def test_low_score_is_observation_not_official_pick():
    rows = quotes()
    rows[-1]['ah_home_line'] = '-0.5'
    observation = {}
    pick, why = daily.assess(fixture(), rows,
                             {'form': {'home': 'WWWWW', 'away': 'DDDDD'}},
                             CFG, NOW, observation=observation)
    assert pick is None and why == '基本面与盘口评分不足'
    assert observation['pick_team'] == 'Everton'
    assert observation['model_confidence'] == 'low'
    assert observation['score'] == pytest.approx(3.9)
    assert observation['home_form'] == 'WWWWW' and observation['line_open'] == -0.5
    for row in rows:
        row['verification'] = 'single'
    blocked = {}
    assert daily.assess(fixture(), rows, FORM, CFG, NOW, observation=blocked)[0] is None
    assert not blocked


def test_age_downgrades_confidence_without_bypassing_other_checks():
    aged = NOW + timedelta(minutes=31)
    pick, reason = daily.assess(fixture(), quotes(), FORM, CFG, aged)
    assert not reason and pick['confidence'] == 'low'
    assert any('降一级' in risk for risk in pick['risks'])
    report = {'date': '2026-11-06', 'picks': [pick], 'watchlist': []}
    daily.policy.apply(report)
    assert not report['picks'] and len(report['watchlist']) == 1

    single_source = [row for row in quotes() if row['source'] != 'odds_api']
    assert daily.assess(fixture(), single_source, FORM, CFG, aged)[0] is None
    assert daily.assess(fixture(), quotes(), FORM, CFG, NOW + timedelta(hours=3))[0] is None


def test_match_started_during_collection_is_not_recommended():
    after_kickoff = datetime(2026, 11, 6, 20, 1, tzinfo=UTC)
    pick, reason = daily.assess(fixture(), quotes(after_kickoff), FORM, CFG, after_kickoff)
    assert pick is None and reason == '比赛已开始'


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


def test_collection_limit_preserves_matches_with_available_form_data():
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
            return [{'id': 'earlier', 'status': 'scheduled', 'league': {'name': 'QatarSL'},
                     'scheduled_at': '2026-11-06T14:00:00Z',
                     'home_team': {'name': 'Team A'}, 'away_team': {'name': 'Team B'}}]
    rows, errors, total = daily.collect_fixtures(Infer(), FD(), NOW, limit=1)
    assert not errors and total == 2
    assert rows[0]['match_id'] == 'fd-123'


def test_provider_errors_show_status_without_leaking_request_details():
    assert daily._collection_error('OddsPapi 赛程', daily.oddspapi.OddsPapiError(
        'OddsPapi fixtures HTTP 403')) == 'OddsPapi 赛程: OddsPapi fixtures HTTP 403'
    assert daily._collection_error('OddsPapi 赛程', RuntimeError(
        'https://example.invalid/?apiKey=SECRET')) == 'OddsPapi 赛程: RuntimeError'


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


def test_odds_papi_maps_premier_league_for_cross_check(tmp_path):
    class Papi:
        def __init__(self):
            self.calls = []
        def remaining(self):
            return 100
        def fixtures(self, tournament, _start, _end):
            self.calls.append(tournament)
            return [{'fixtureId': 'id123', 'participant1Name': 'Everton', 'participant2Name': 'Coventry',
                     'startTime': '2026-11-06T20:00:00Z'}]
    papi = Papi()
    snap = daily.odds_snapshots.Snapshotter('2026-11-06', papi=papi, snap_dir=tmp_path,
                                           ah_markets={}, now=lambda: NOW)
    snap._map_oddspapi([fixture()])
    assert papi.calls == [17]
    assert snap.map['m1']['oddspapi'] == 'id123'


def test_asian_leagues_keep_reserved_slots_against_european_crowding():
    H = ['Arsenal', 'Chelsea', 'Everton', 'Fulham', 'Brentford', 'Wolves']
    A = ['Liverpool', 'Newcastle', 'Burnley', 'Brighton', 'Southampton', 'Leicester']
    class FD:
        enabled = True
        def matches(self, *args):
            return [{'id': i, 'competition': {'code': 'PL'}, 'status': 'TIMED',
                     'utcDate': f'2026-11-06T{18 + i % 4}:00:00Z',
                     'homeTeam': {'name': H[i]}, 'awayTeam': {'name': A[i]}}
                    for i in range(6)]
    class Infer:
        enabled = True
        def events(self, day):
            if day != '2026-11-06':
                return []
            return [{'id': 'q1', 'status': 'scheduled', 'league': {'name': 'QatarSL'},
                     'scheduled_at': '2026-11-06T14:00:00Z',
                     'home_team': {'name': 'Team A'}, 'away_team': {'name': 'Team B'}}]
    rows, errors, total = daily.collect_fixtures(Infer(), FD(), NOW, limit=3)
    assert not errors and total == 7 and len(rows) == 3
    assert any(r['league_id'] == 'qsl' for r in rows)


def test_oddspapi_404_is_empty_window_not_collection_error():
    class Papi:
        enabled = True
        def fixtures(self, tid, start, end):
            raise daily.oddspapi.OddsPapiError('OddsPapi fixtures HTTP 404')
    class Down:
        enabled = False
    rows, errors, total = daily.collect_fixtures(Down(), Down(), NOW, papi=Papi())
    assert rows == [] and errors == [] and total == 0
    assert any('未取得赛程' in n for n in daily.collect_fixtures.notes)


def test_observation_fill_respects_daily_cap_and_never_invents():
    from ah import publish_telegram as tg
    def w(i):
        return {'home': f'H{i}', 'away': f'A{i}', 'league': 'X', 'kickoff_utc': '2026-11-06T12:00:00Z',
                'observation': {'pick_team': f'H{i}', 'line': -0.25, 'line_open': -0.25, 'score': 3.0 + i / 10,
                                'odds': 0.9, 'bookmaker': 'pinnacle', 'source': 'oddspapi',
                                'home_form': 'WWDLL', 'away_form': 'LLDWW', 'price_ts': '2026-11-06T10:00:00Z'}}
    rep = {'picks': [], 'watchlist': [w(i) for i in range(10)]}
    assert tg.observation_blocks(rep)[0].count('⚽') == 7
    assert tg.observation_blocks(rep, 5)[0].count('⚽') == 2
    assert tg.observation_blocks(rep, 7) == []
    assert tg.observation_blocks({'picks': [], 'watchlist': [w(0)]})[0].count('⚽') == 1


def _hist(source, league, home, away, day, hg, ag, eid):
    return {'source': source, 'event_id': eid, 'league_id': league, 'kickoff_utc': f'2026-10-{day:02d}T12:00:00Z',
            'home': home, 'away': away, 'home_goals': hg, 'away_goals': ag}


def test_form_history_prefers_api_football_and_falls_back_to_infersports():
    from ah import form_history as fh
    rows = []
    for i, (h, a, hg, ag) in enumerate([('Al Wakrah', 'X1', 2, 0), ('X2', 'Al Wakrah', 1, 1), ('Al Wakrah', 'X3', 0, 1),
                                        ('Al Shahaniya', 'X4', 3, 0), ('X5', 'Al Shahaniya', 2, 2),
                                        ('Al Shahaniya', 'X6', 1, 0)]):
        rows.append(_hist('infersports', 'qsl', h, a, 10 + i, hg, ag, f'i{i}'))
    out = fh.fundamentals(rows, 'qsl', 'Al Wakrah', 'Al Shahaniya', '2026-11-06T16:30:00Z')
    assert out['source'] == 'infersports'
    assert out['form'] == {'home': 'LDW', 'away': 'WDW'}
    # 更高优先级来源足够时优先使用，且不与低优先级来源混算
    better = [_hist('api_football', 'qsl', 'Al Wakrah', f'Y{i}', 20 + i, 1, 0, f'a{i}') for i in range(3)] + \
             [_hist('api_football', 'qsl', 'Al Shahaniya', f'Z{i}', 20 + i, 0, 1, f'b{i}') for i in range(3)]
    out = fh.fundamentals(rows + better, 'qsl', 'Al Wakrah', 'Al Shahaniya', '2026-11-06T16:30:00Z')
    assert out['source'] == 'api_football' and out['form'] == {'home': 'WWW', 'away': 'LLL'}


def test_form_history_returns_none_when_not_enough_games_and_ignores_future():
    from ah import form_history as fh
    rows = [_hist('infersports', 'qsl', 'Al Wakrah', 'X', 10, 1, 0, 'a'), _hist('infersports', 'qsl', 'Al Wakrah', 'Y', 11, 1, 0, 'b'),
            _hist('infersports', 'qsl', 'Al Wakrah', 'Z', 12, 1, 0, 'c')]
    assert fh.fundamentals(rows, 'qsl', 'Al Wakrah', 'Al Shahaniya', '2026-11-06T16:30:00Z') is None
    assert fh.team_form(rows, 'Al Wakrah', '2026-10-11T12:00:00Z') == 'W'   # 开球时间之后的比赛不计入


def test_infersports_score_extraction_never_guesses():
    from ah import form_history as fh
    assert fh.extract_score({'score': {'home': 2, 'away': 1}}) == (2, 1)
    assert fh.extract_score({'home_score': '0', 'away_score': '0'}) == (0, 0)
    assert fh.extract_score({'status': 'finished'}) is None
    assert fh.extract_score({'score': {'home': None, 'away': 1}}) is None
    assert fh.extract_score({'score': {'home': 1.9, 'away': 1}}) is None
    assert fh.extract_score({'score': {'home': True, 'away': 1}}) is None
    assert fh.is_finished({'status': 'Finished'}) and not fh.is_finished({'status': 'scheduled'})


def test_api_football_form_does_not_treat_extra_time_score_as_regulation():
    from ah import form_history as fh
    class API:
        enabled = True
        def get(self, *_args, **_kwargs):
            def fx(mid, status, goals, fulltime=None):
                return {'fixture': {'id': mid, 'date': '2026-10-12T12:00:00Z', 'status': {'short': status}},
                        'teams': {'home': {'name': 'A'}, 'away': {'name': 'B'}},
                        'goals': goals, 'score': {'fulltime': fulltime}}
            return {'response': [fx(1, 'AET', {'home': 2, 'away': 1}),
                                 fx(2, 'FT', {'home': 2, 'away': 0}),
                                 fx(3, 'PEN', {'home': 3, 'away': 2}, {'home': 1, 'away': 1})]}
    rows, notes = fh.refresh_api_football(API(), {'id': 'j1', 'apif': 98, 'season': 2026}, [])
    assert not notes and [r['event_id'] for r in rows] == ['2', '3']
    assert [(r['home_goals'], r['away_goals']) for r in rows] == [(2, 0), (1, 1)]


def test_infersports_backfill_reports_missing_score_fields(tmp_path):
    from ah import form_history as fh
    class Infer:
        enabled = True
        def events(self, day):
            return [{'id': 'e1', 'status': 'finished', 'league': {'name': 'QatarSL'},
                     'scheduled_at': '2026-11-01T12:00:00Z', 'home_team': {'name': 'A'}, 'away_team': {'name': 'B'}}]
    added, notes = fh.refresh_infersports(Infer(), NOW, {'qatarsl': 'qsl'}, [], max_dates=1,
                                          covered_path=tmp_path / 'covered.json')
    assert added == [] and any('没有可识别的比分字段' in n for n in notes)
