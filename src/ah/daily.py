"""Build a daily report from live fixtures, verified odds and available fundamentals.

Never overwrite an existing report. Missing data stays on the watchlist; the
heuristic only publishes after the repository's cross-source checks pass.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import yaml

from . import CONFIG
from . import analyze, api_football, consensus, enrich, fetch_fixtures, form_history, fetch_odds, fivedollar, football_data, infersports, odds_api, odds_snapshots, oddspapi, policy
from .match import BJT, parse_dt, titan_kickoff_utc
from .render_report import report_paths, validate

UTC = timezone.utc
SOURCE_URLS = {
    'odds_api': 'https://the-odds-api.com/',
    'oddspapi': 'https://oddspapi.io/',
    'infersports': 'https://infersports.dev/',
}


def _collection_error(source, exc):
    """Expose provider HTTP status without ever logging request URLs or keys."""
    if isinstance(exc, oddspapi.OddsPapiError):
        message = str(exc)
        if re.fullmatch(r'OddsPapi /?[a-z-]+ HTTP [0-9]{3}', message):
            return f'{source}: {message}'
    return f'{source}: {type(exc).__name__}'


def _usable_verification(check):
    """Allow a freshness downgrade only when all other checks have passed."""
    if check.get('passed'):
        return True
    reasons = check.get('reasons') or []
    return (check.get('freshness') == 'stale_downgrade' and len(reasons) == 1
            and reasons[0].startswith('stale>'))


def _verification_gate(check, cfg, has_verified):
    """返回 (是否可用于推荐, 是否 advisory 降级放行)。

    - 核验通过，或仅新鲜度降级 -> (True, False)
    - cross_verification_draft.advisory_only 开启且存在 verified 主线报价 -> (True, True)
    - 其余 -> (False, False)

    防断言：advisory=True 只代表「有真实报价但核验未完全通过」，绝不等于核验通过；
    调用方必须把信心压到低、显式标注风险，且不得写「已通过多源核验」。
    """
    if _usable_verification(check):
        return True, False
    if (cfg or {}).get('advisory_only') and has_verified:
        return True, True
    return False, False


def _settings():
    return yaml.safe_load((CONFIG / 'settings.yaml').read_text(encoding='utf-8')) or {}


def _leagues():
    return yaml.safe_load((CONFIG / 'leagues.yaml').read_text(encoding='utf-8'))['leagues']


def collect_fixtures(infs, fd, now, hours=36, limit=8, apif=None, papi=None, fd5=None):
    """Return upcoming configured fixtures and source errors, without inventing data."""
    leagues = _leagues()
    by_id = {entry['id']: entry for entry in leagues}
    config = yaml.safe_load((CONFIG / 'sources.yaml').read_text(encoding='utf-8')) or {}
    infer_codes = (config.get('infersports') or {}).get('leagues') or {}
    infer_ids = {str(v).casefold(): k for k, v in infer_codes.items() if k in by_id}
    fd_ids = {value: key for key, value in football_data.CODE_BY_LEAGUE.items() if key in by_id}
    end = now + timedelta(hours=hours)
    rows, errors, succeeded = [], [], 0
    notes = []

    def add(mid, lid, home, away, kickoff):
        if not all((mid, lid, home, away, kickoff)):
            return
        try:
            ko = parse_dt(kickoff)
        except (TypeError, ValueError):
            return
        if not now < ko <= end:
            return
        events = {str(i): {'home': r['home'], 'away': r['away'], 'commence': r['kickoff_utc']}
                  for i, r in enumerate(rows) if r['league_id'] == lid}
        if consensus.find_event(events, home, away, ko):
            return
        league = by_id[lid]
        rows.append({'match_id': mid, 'fixture_id': odds_snapshots.apif_id(mid), 'league_id': lid,
                     'league': league.get('cn') or league['name'], 'home': home, 'away': away,
                     'kickoff_utc': ko.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
                     'tier': league.get('tier', 3), 'list': 'watchlist'})

    if apif and apif.enabled:
        current = now.date()
        while current <= end.date():
            try:
                games = fetch_fixtures.fetch_api(current.isoformat(), client=apif)
                succeeded += 1
                for game in games:
                    if game.get('status') not in ('NS', 'TBD'):
                        continue
                    add(game['match_id'], game.get('league_key'), game.get('home'), game.get('away'), game.get('kickoff_utc'))
            except Exception as exc:
                errors.append('API-Football: ' + type(exc).__name__)
            current += timedelta(days=1)

    if fd and fd.enabled:
        try:
            games = fd.matches(now.date().isoformat(), end.date().isoformat(), ','.join(fd_ids))
            succeeded += 1
            for game in games:
                if game.get('status') not in ('SCHEDULED', 'TIMED'):
                    continue
                add('fd-' + str(game['id']), fd_ids.get((game.get('competition') or {}).get('code')),
                    (game.get('homeTeam') or {}).get('name'), (game.get('awayTeam') or {}).get('name'), game.get('utcDate'))
        except Exception as exc:
            errors.append('football-data: ' + type(exc).__name__)

    if infs and infs.enabled:
        current = now.date()
        while current <= end.date():
            try:
                games = infs.events(current.isoformat())
                succeeded += 1
                for game in games:
                    if game.get('status') not in ('scheduled', 'not_started'):
                        continue
                    add('infs-' + str(game.get('id') or ''), infer_ids.get(str((game.get('league') or {}).get('name', '')).casefold()),
                        (game.get('home_team') or {}).get('name'), (game.get('away_team') or {}).get('name'), game.get('scheduled_at'))
            except Exception as exc:
                errors.append('InferSports: ' + type(exc).__name__)
            current += timedelta(days=1)
    if papi and papi.enabled:
        # InferSports does not cover these priority leagues; respect the OddsPapi reserve.
        for lid in ('acl_elite', 'csl'):
            try:
                games = papi.fixtures(oddspapi.TOURNAMENTS[lid], now, end)
                succeeded += 1
                for game in games:
                    if game.get('fixtureId'):
                        add('oddspapi-' + str(game['fixtureId']), lid, game.get('participant1Name'),
                            game.get('participant2Name'), game.get('startTime'))
            except Exception as exc:
                if 'HTTP 404' in str(exc):
                    # A 404 can mean no matching fixtures; the response body was not
                    # retained, so report only that no fixtures were obtained.
                    succeeded += 1
                    notes.append(f'OddsPapi 赛程 {lid}: 未取得赛程（HTTP 404）')
                    continue
                errors.append(_collection_error(f'OddsPapi 赛程 {lid}', exc))
    if not succeeded:
        raise RuntimeError('没有可用的赛程数据源')
    # Prefer fixtures for which an enabled provider can supply team form.
    # Asian priority slots below still preserve coverage for observation.
    supported_form_leagues = set(football_data.CODE_BY_LEAGUE) if fd and fd.enabled else set()
    if fd5 and fd5.enabled:
        supported_form_leagues.update(fivedollar.LEAGUE_IDS)
    chronological = lambda row: (row['kickoff_utc'], row['tier'], row['match_id'])
    rows.sort(key=lambda row: (row['league_id'] not in supported_form_leagues,
                               *chronological(row)))
    # Reserve slots for Asian tier-1/2 leagues that football-data cannot evaluate, so the
    # European leagues cannot crowd them out of the bounded screening limit.
    asian_ids = {'acl_elite', 'acl_two', 'j1', 'j2', 'k1', 'csl', 'aleague', 'spl', 'qsl'}
    reserved = [r for r in sorted(rows, key=chronological)
                if r['league_id'] in asian_ids and r['tier'] <= 2][:limit // 3]
    taken = {r['match_id'] for r in reserved}
    rest = [r for r in rows if r['match_id'] not in taken][:max(0, limit - len(reserved))]
    selected = sorted(reserved + rest, key=chronological)
    collect_fixtures.notes = notes
    return selected, errors, len(rows)


def assess(match, rows, fundamentals, cfg, now, observation=None):
    """Return a supported pick, or a reason for withholding the match."""
    mid = match['match_id']
    if parse_dt(match['kickoff_utc']) <= now:
        return None, '比赛已开始'
    verification = odds_snapshots.evaluate_cross_verification(rows, mid, 'report', cfg, now=now)
    mains = [r for r in rows if r.get('match_id') == mid and r.get('stage') == 'report'
             and str(r.get('is_main')) == '1' and r.get('verification') == 'verified']
    usable, advisory = _verification_gate(verification, cfg, bool(mains))
    if not usable or not mains:
        return None, '盘口跨来源核验未通过'
    mains.sort(key=lambda r: (r.get('bookmaker') != 'pinnacle', r.get('source') != 'oddspapi'))
    quote = mains[0]
    if not quote.get('price_ts'):
        return None, '盘口缺少报价时间'
    try:
        stamp = parse_dt(quote['price_ts'])
        line_value = float(quote['ah_home_line'])
        prices = [float(quote['odds_home']), float(quote['odds_away'])]
    except (TypeError, ValueError, KeyError):
        return None, '盘口数据无效'
    if (stamp > now + timedelta(minutes=5) or not math.isfinite(line_value)
            or abs(line_value * 4 - round(line_value * 4)) > 1e-8
            or any(not math.isfinite(p) or p <= 1 for p in prices)):
        return None, '盘口数据无效'
    if abs(line_value) >= float(policy.load()['max_abs_line_to_publish']):
        return None, '盘口过深'
    forms = (fundamentals or {}).get('form') or {}
    if min(len(forms.get('home', '')), len(forms.get('away', ''))) < 3:
        return None, '近期状态数据不足'
    opened = [r for r in rows if r.get('match_id') == mid and r.get('stage') == 'open'
              and str(r.get('is_main')) == '1' and r.get('bookmaker') == quote.get('bookmaker')
              and r.get('source') == quote.get('source') and r.get('ah_home_line') not in ('', None)]
    if not opened:
        return None, '缺少同一来源和机构的初盘'
    line, line_open = float(quote['ah_home_line']), float(opened[0]['ah_home_line'])
    if not math.isfinite(line_open) or abs(line_open * 4 - round(line_open * 4)) > 1e-8:
        return None, '初盘数据无效'
    form_source = (fundamentals or {}).get('source') or 'football-data.org'
    form_urls = {'football-data.org': 'https://www.football-data.org/',
                 '5DollarFootballAPI': 'https://5dollarfootballapi.com/',
                 'api_football': 'https://www.api-football.com/',
                 'infersports': 'https://infersports.dev/'}
    form_url = form_urls.get(form_source)
    if not form_url:
        return None, '近期状态来源未识别'
    verify_note = ('盘口已通过多源核验' if not advisory
                   else '盘口跨来源核验未完全通过（advisory），仅作参考')
    candidates = []
    for side in ('home', 'away'):
        sign = 1 if side == 'home' else -1
        mine, opp = (forms['home'], forms['away']) if side == 'home' else (forms['away'], forms['home'])
        features = {'form': enrich.form_feature(mine, opp)}
        h2h = (fundamentals or {}).get('h2h')
        if h2h and h2h.get('played', 0) >= 3:
            h2h_score = enrich.h2h_feature(h2h)
            features['h2h'] = sign * h2h_score
        decimal = float(quote['odds_home'] if side == 'home' else quote['odds_away'])
        if decimal <= 1:
            continue
        candidates.append(analyze.Candidate(
            match_id=mid, league=match['league'], home=match['home'], away=match['away'],
            kickoff_utc=match['kickoff_utc'], pick_side=side, line=sign * line,
            line_open=sign * line_open, odds=round(decimal - 1, 3), features=features,
            reasons=[f"同机构初盘 {sign * line_open:+g} → 当前 {sign * line:+g}，{verify_note}",
                     f"近期赛果（最近在前）：{match['home']} {forms['home']}；{match['away']} {forms['away']}"]
                    + (['Football data by 5DollarFootballAPI'] if form_source == '5DollarFootballAPI' else []),
            risks=['尚未自动核实伤停及首发；规则评分未经独立回测，赛前应再次核查'],
            sources=list(dict.fromkeys([form_url] +
                                      [SOURCE_URLS[r['source']] for r in mains if r.get('source') in SOURCE_URLS]))))
    settings = _settings()
    picks = analyze.select(candidates, settings)
    # Preserve a separately labelled low-confidence model observation for the
    # report. This does not alter official picks, publish rules, or the ledger.
    if observation is not None:
        low_settings = {**settings, 'min_confidence_to_publish': 'low'}
        observed = analyze.select(candidates, low_settings)
        if observed:
            best = observed[0]
            observation.update({
                'pick_side': best['pick_side'], 'pick_team': match[best['pick_side']],
                'line': best['line'], 'line_open': best['line_open'], 'odds': best['odds'],
                'score': best['score'], 'model_confidence': best['confidence'],
                'home_form': forms['home'], 'away_form': forms['away'], 'form_source': form_source,
                'bookmaker': quote['bookmaker'], 'source': quote['source'],
                'price_ts': quote['price_ts'], 'freshness': verification.get('freshness'),
            })
    if not picks:
        return None, '基本面与盘口评分不足'
    picked = picks[0]
    if verification.get('freshness') == 'stale_downgrade':
        confidence = policy.DOWN.get(policy.EN.get(picked.get('confidence', '')))
        if confidence is None:
            return None, '盘口报价过期，降级后不足'
        picked['confidence'] = confidence
        picked['risks'].append('盘口报价超过新鲜度阈值，信心已降一级')
    if advisory:
        # 核验未完全通过：不伪装成高/中信心，强制标低并显式披露。
        # 不在此再做 low→None 降级，否则低信心场次会全部消失，等于把 advisory 关掉。
        picked['verification_advisory'] = True
        picked['confidence'] = 'low'
        picked['risks'].append('盘口跨来源核验未完全通过（advisory），结论仅作分析参考')
    picked['pick_team'] = match[picked['pick_side']]
    excluded = set(cfg.get('exclude_sources') or ['api_football'])
    books = {}
    for row in rows:
        if (row.get('match_id') != mid or row.get('stage') != 'report' or str(row.get('is_main')) != '1'
                or row.get('source') in excluded or not row.get('bookmaker')):
            continue
        try:
            ln, home, away = float(row['ah_home_line']), float(row['odds_home']), float(row['odds_away'])
        except (TypeError, ValueError, KeyError):
            continue
        if not all(map(math.isfinite, (ln, home, away))) or home <= 1 or away <= 1:
            continue
        books.setdefault(row['bookmaker'], {'bookmaker': row['bookmaker'], 'home_line': ln,
                                             'home_hk': home - 1, 'away_hk': away - 1})
    if len(books) < 3:
        return None, '可核验的盘口公司不足'
    market = odds_api.consensus(list(books.values()), pinnacle_main=line)
    sign = 1 if picked['pick_side'] == 'home' else -1
    picked['market_consensus'] = {'source': '多源盘口快照', 'bookmakers': market['bookmakers'],
                                  'agree': market['agree'], 'home_line': market['home_line'],
                                  'pick_line': sign * market['home_line'],
                                  'pick_water': market['home_hk'] if sign == 1 else market['away_hk']}
    return picked, ''


def collect_titan(matches):
    """Collect public fixture/price observations; never treat them as verified odds."""
    fixtures, odds = fetch_fixtures.fetch(), fetch_odds.fetch()
    out = {}
    for match in matches:
        day = parse_dt(match['kickoff_utc']).astimezone(BJT).date().isoformat()
        events = {}
        for fixture in fixtures:
            try:
                kickoff = titan_kickoff_utc(fixture['kickoff_bjt'], day)
            except (KeyError, TypeError, ValueError):
                continue
            events[fixture['match_id']] = {'id': fixture['match_id'], 'home': fixture['home'],
                                           'away': fixture['away'], 'commence': kickoff}
        found = consensus.find_event(events, match['home'], match['away'], match['kickoff_utc'])
        if found and found['id'] in odds:
            price = odds[found['id']]
            if price.get('ah_home_line') is not None:
                out[match['match_id']] = {'source': 'titan007', 'home_line': -price['ah_home_line'],
                                          'home_water': price.get('ah_home_water'), 'away_water': price.get('ah_away_water')}
    return out


def build_report(report_date, now=None, max_fixtures=None, snapshot_dir=None):
    now = now or datetime.now(UTC)
    max_fixtures = max_fixtures if max_fixtures is not None else int(_settings().get('screening_max_fixtures', 24))
    if not 1 <= max_fixtures <= 60:
        raise ValueError('初筛上限必须在 1–60 场之间')
    infs, fd, oa, papi, apif, fd5 = (infersports.Client(), football_data.Client(), odds_api.OddsAPI(regions='eu'),
                                     oddspapi.Client(), api_football.Client(), fivedollar.Client())
    errors = []
    notes = []
    if not apif.enabled:
        notes.append('API-Football 未启用')
    if not fd.enabled:
        notes.append('football-data 未配置')
    if not oa.enabled:
        notes.append('The Odds API 未配置')
    if not papi.enabled:
        notes.append('OddsPapi 未配置')
    if oa.enabled:
        try:
            oa.sports()  # free preflight updates the provider quota, when supplied
        except Exception as exc:
            errors.append('The Odds API: ' + type(exc).__name__)
            oa = None
    if papi.enabled:
        try:
            papi.sync_usage()  # free account endpoint; respect the monthly reserve
        except Exception as exc:
            errors.append('OddsPapi: ' + type(exc).__name__)
            papi = None
    matches, fixture_errors, total = collect_fixtures(infs, fd, now, hours=_settings().get('report_window_hours', 36),
                                                       limit=max_fixtures, apif=apif, papi=papi, fd5=fd5)
    errors.extend(fixture_errors)
    notes.extend(getattr(collect_fixtures, 'notes', []))
    fd5_leagues = dict(fivedollar.LEAGUE_IDS)
    if fd5.enabled and any(m['league_id'] in fivedollar.OPTIONAL_ASIAN_IDS for m in matches):
        try:
            fd5_leagues = fd5.available_leagues()
            added = sorted(set(fd5_leagues) & set(fivedollar.OPTIONAL_ASIAN_IDS))
            if added:
                notes.append('5DollarFootballAPI 亚洲联赛覆盖：' + ', '.join(added))
        except Exception as exc:
            errors.append('5Dollar 联赛覆盖: ' + type(exc).__name__)
    try:
        titan = collect_titan(matches) if matches else {}
    except Exception as exc:
        titan = {}
        errors.append('球探: ' + type(exc).__name__)
    snapshotter = odds_snapshots.Snapshotter(report_date, papi=papi, oddsapi=oa, apif=apif, infs=infs,
                                             sources=('oddspapi', 'odds_api', 'infersports', 'api_football'),
                                             papi_groups=(('pinnacle', 'sbobet', 'singbet'),),
                                             now=lambda: datetime.now(UTC), snap_dir=snapshot_dir,
                                             log=lambda _message: None)
    for match in matches:
        if match['match_id'].startswith('oddspapi-'):
            snapshotter.map.setdefault(match['match_id'], {})['oddspapi'] = match['match_id'].split('-', 1)[1]
    snapshot_result = snapshotter.run(matches, ['open', 'report'])
    # Keep the structured, credential-free source failure codes. Previously
    # snapshot errors were discarded even when a second odds source failed.
    snapshot_issues = Counter(
        reason for phase, reasons in snapshot_result.get('reasons', {}).items()
        if phase.endswith('|report') for reason in set(reasons)
        if ':fixtures_error(' in reason or ':historical_error(' in reason
        or ':no_fixture_match' in reason or ':odds_error(' in reason
    )
    if snapshot_issues:
        notes.append('盘口采集：' + '；'.join(f'{reason} {count}场'
                                          for reason, count in sorted(snapshot_issues.items())))
    rows = odds_snapshots.load_rows(snapshotter.p['csv'])
    cfg = odds_snapshots.load_cross_verification()
    report = {'date': report_date, 'generated_at': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
              'edition': 'automatic', 'odds_format': 'HK', 'picks': [], 'watched': [], 'watchlist': [],
              'odds_source_note': '赛程、盘口与近期状态自动采集；推荐只用真实盘口，核验未完全通过的场次强制低信心并显式标注。',
              'not_covered': f'筛查上限 {max_fixtures} 场；窗口内共发现 {total} 场。尚未自动采集伤停或首发，亚洲赛事基本面仍需人工核查。',
              'collection_errors': errors, 'collection_notes': notes}
    verification_failures = Counter()
    history, apif_done = None, set()
    for match in matches:
        mid = match['match_id']
        check = odds_snapshots.evaluate_cross_verification(rows, mid, 'report', cfg, now=datetime.now(UTC))
        if not _usable_verification(check):
            verification_failures.update(check.get('reasons') or ['no_quotes'])
        has_verified = any(r.get('match_id') == mid and r.get('stage') == 'report' and r.get('verification') == 'verified'
                           and str(r.get('is_main')) == '1' for r in rows)
        usable, advisory = _verification_gate(check, cfg, has_verified)
        fundamental = None
        code = football_data.CODE_BY_LEAGUE.get(match['league_id'])
        if usable and has_verified and code and fd.enabled:
            try:
                fundamental = football_data.fundamentals(fd, code, match['home'], match['away'], match['kickoff_utc'], 'home')
            except Exception as exc:
                errors.append('基本面: ' + type(exc).__name__)
        if (fundamental is None and not code and usable and has_verified):
            # football-data 不覆盖的联赛（亚洲等）：API-Football → InferSports 赛果库，逐级兜底。
            if history is None:
                history = form_history.load()
                added, hist_notes = form_history.refresh_infersports(
                    infs, now, {str(v).casefold(): k for k, v in
                                ((yaml.safe_load((CONFIG / 'sources.yaml').read_text(encoding='utf-8')) or {})
                                 .get('infersports') or {}).get('leagues', {}).items()}, history)
                history += added
                notes += hist_notes
                if added:
                    form_history.save(history)
            league_cfg = next((x for x in _leagues() if x['id'] == match['league_id']), None)
            if league_cfg and match['league_id'] not in apif_done:
                apif_done.add(match['league_id'])
                added, hist_notes = form_history.refresh_api_football(apif, league_cfg, history)
                history += added
                notes += hist_notes
                if added:
                    form_history.save(history)
            fundamental = form_history.fundamentals(history, match['league_id'], match['home'], match['away'],
                                                    match['kickoff_utc'])
            if fundamental is None:
                notes.append(f"近期状态不足：{match['home']} vs {match['away']}（API-Football 与 InferSports 赛果库均不足 3 场）")
        forms = (fundamental or {}).get('form') or {}
        if (usable and has_verified and fd5.enabled
                and match['league_id'] in fd5_leagues
                and min(len(forms.get('home', '')), len(forms.get('away', ''))) < 3):
            try:
                fallback = fivedollar.fundamentals(fd5, match['league_id'], match['home'], match['away'],
                                                  match['kickoff_utc'], datetime.now(UTC),
                                                  league_id=fd5_leagues[match['league_id']])
                fforms = fallback.get('form') or {}
                if min(len(fforms.get('home', '')), len(fforms.get('away', ''))) >= 3:
                    fundamental = fallback
                    notes.append('5DollarFootballAPI 近期赛果补充：' + mid)
            except Exception as exc:
                errors.append('5Dollar 近期赛果: ' + type(exc).__name__)
        observation = {}
        pick, why = assess(match, rows, fundamental, cfg, datetime.now(UTC), observation=observation)
        if pick:
            if observation:
                pick['observation'] = observation
            report['picks'].append(pick)
        else:
            item = {**match, 'confidence': None, 'withheld_category': why, 'withheld_reason': why}
            if observation:
                item['observation'] = observation
            if mid in titan:
                item['titan007_observation'] = titan[mid]
            report['watchlist'].append(item)
    if verification_failures:
        notes.append('盘口核验：' + '；'.join(f'{reason} {count}场'
                                          for reason, count in sorted(verification_failures.items())))
    policy.apply(report)
    if report['picks']:
        # 5Dollar is supplementary only; lack of data never creates a pick.
        consensus.attach(report, fetch=lambda _sport: {}, fd5=fd5)
        policy.apply(report)
    if notes or errors:
        report['not_covered'] += ' 数据源状态：' + '；'.join(notes + errors) + '。'
    validate(report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='采集赛程和盘口，生成日报（默认不覆盖现有报告）')
    parser.add_argument('--date', default=datetime.now(UTC).strftime('%Y-%m-%d'))
    parser.add_argument('--max-fixtures', type=int, choices=range(1, 61))
    parser.add_argument('--preview', action='store_true', help='采集并验证，但不写入日报')
    parser.add_argument('--refresh', action='store_true',
                        help='显式重新采集并覆盖当天报告（编排层开关；不改变评分/核验/阈值算法）')
    args = parser.parse_args(argv)
    date.fromisoformat(args.date)
    path, _ = report_paths(args.date)
    if path.exists() and not args.preview and not args.refresh:
        print('日报已存在，保持原有内容；如需重新采集请显式传 --refresh', file=sys.stderr)
        return
    if args.refresh and path.exists():
        print(f'REPORT_REFRESH_OVERWRITE 显式覆盖已存在报告：{path}', file=sys.stderr)
    # 一天 4 次推送按北京日期归档（北京 07:00 = UTC 前一日 23:00），故当前 UTC 日期或北京日期均可。
    allowed = {datetime.now(UTC).date(), (datetime.now(UTC) + timedelta(hours=8)).date()}
    if date.fromisoformat(args.date) not in allowed:
        parser.error('只能为当前 UTC 日期或当前北京日期采集实时数据')
    report = build_report(args.date, max_fixtures=args.max_fixtures)
    if args.preview:
        print(f"预览：观察 {len(report['watchlist'])} 场，推荐 {len(report['picks'])} 场；采集问题 {len(report['collection_errors'])} 项")
        reasons = Counter(item.get('withheld_category', '未定') for item in report['watchlist'])
        for reason, count in sorted(reasons.items()):
            print(f"未推荐：{reason} {count} 场")
        for error in report['collection_errors']:
            print(f"采集状态：{error}")
        for note in report['collection_notes']:
            print(f"采集提示：{note}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 'w' if args.refresh else 'x'
    with path.open(mode, encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(f"日报已生成：观察 {len(report['watchlist'])} 场，推荐 {len(report['picks'])} 场；"
          f"采集问题 {len(report['collection_errors'])} 项，提示 {len(report['collection_notes'])} 项")


if __name__ == '__main__':
    main()
