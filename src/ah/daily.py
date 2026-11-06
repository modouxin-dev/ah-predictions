"""Build a daily report from live fixtures, verified odds and available fundamentals.

Never overwrite an existing report. Missing data stays on the watchlist; the
heuristic only publishes after the repository's cross-source checks pass.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import yaml

from . import CONFIG
from . import analyze, api_football, consensus, enrich, fetch_fixtures, fetch_odds, fivedollar, football_data, infersports, odds_api, odds_snapshots, oddspapi, policy
from .match import BJT, parse_dt, titan_kickoff_utc
from .render_report import report_paths, validate

UTC = timezone.utc
SOURCE_URLS = {
    'odds_api': 'https://the-odds-api.com/',
    'oddspapi': 'https://oddspapi.io/',
    'infersports': 'https://infersports.dev/',
}


def _settings():
    return yaml.safe_load((CONFIG / 'settings.yaml').read_text(encoding='utf-8')) or {}


def _leagues():
    return yaml.safe_load((CONFIG / 'leagues.yaml').read_text(encoding='utf-8'))['leagues']


def collect_fixtures(infs, fd, now, hours=36, limit=8, apif=None, papi=None):
    """Return upcoming configured fixtures and source errors, without inventing data."""
    leagues = _leagues()
    by_id = {entry['id']: entry for entry in leagues}
    config = yaml.safe_load((CONFIG / 'sources.yaml').read_text(encoding='utf-8')) or {}
    infer_codes = (config.get('infersports') or {}).get('leagues') or {}
    infer_ids = {str(v).casefold(): k for k, v in infer_codes.items() if k in by_id}
    fd_ids = {value: key for key, value in football_data.CODE_BY_LEAGUE.items() if key in by_id}
    end = now + timedelta(hours=hours)
    rows, errors, succeeded = [], [], 0

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
                errors.append('OddsPapi 赛程: ' + type(exc).__name__)
    if not succeeded:
        raise RuntimeError('没有可用的赛程数据源')
    rows.sort(key=lambda row: (row['kickoff_utc'], row['tier'], row['match_id']))
    return rows[:limit], errors, len(rows)


def assess(match, rows, fundamentals, cfg, now):
    """Return a supported pick, or a reason for withholding the match."""
    mid = match['match_id']
    verification = odds_snapshots.evaluate_cross_verification(rows, mid, 'report', cfg, now=now)
    mains = [r for r in rows if r.get('match_id') == mid and r.get('stage') == 'report'
             and str(r.get('is_main')) == '1' and r.get('verification') == 'verified']
    if not verification.get('passed') or not mains:
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
            reasons=[f"同机构初盘 {sign * line_open:+g} → 当前 {sign * line:+g}，盘口已通过多源核验",
                     f"近期赛果（最近在前）：{match['home']} {forms['home']}；{match['away']} {forms['away']}"],
            risks=['尚未自动核实伤停及首发；规则评分未经独立回测，赛前应再次核查'],
            sources=list(dict.fromkeys(['https://www.football-data.org/'] +
                                      [SOURCE_URLS[r['source']] for r in mains if r.get('source') in SOURCE_URLS]))))
    picks = analyze.select(candidates, _settings())
    if not picks:
        return None, '基本面与盘口评分不足'
    picked = picks[0]
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


def build_report(report_date, now=None, max_fixtures=8):
    now = now or datetime.now(UTC)
    infs, fd, oa, papi, apif = infersports.Client(), football_data.Client(), odds_api.OddsAPI(regions='eu'), oddspapi.Client(), api_football.Client()
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
                                                       limit=max_fixtures, apif=apif, papi=papi)
    errors.extend(fixture_errors)
    try:
        titan = collect_titan(matches) if matches else {}
    except Exception as exc:
        titan = {}
        errors.append('球探: ' + type(exc).__name__)
    snapshotter = odds_snapshots.Snapshotter(report_date, papi=papi, oddsapi=oa, apif=apif, infs=infs,
                                             sources=('oddspapi', 'odds_api', 'infersports', 'api_football'),
                                             papi_groups=(('pinnacle', 'sbobet', 'singbet'),),
                                             now=lambda: datetime.now(UTC), log=lambda _message: None)
    for match in matches:
        if match['match_id'].startswith('oddspapi-'):
            snapshotter.map.setdefault(match['match_id'], {})['oddspapi'] = match['match_id'].split('-', 1)[1]
    snapshotter.run(matches, ['open', 'report'])
    rows = odds_snapshots.load_rows(snapshotter.p['csv'])
    cfg = odds_snapshots.load_cross_verification()
    report = {'date': report_date, 'generated_at': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
              'edition': 'automatic', 'odds_format': 'HK', 'picks': [], 'watched': [], 'watchlist': [],
              'odds_source_note': '赛程、盘口与近期状态自动采集；推荐需经盘口多源核验。',
              'not_covered': f'筛查上限 {max_fixtures} 场；窗口内共发现 {total} 场。尚未自动采集伤停或首发，亚洲赛事基本面仍需人工核查。',
              'collection_errors': errors, 'collection_notes': notes}
    for match in matches:
        mid = match['match_id']
        check = odds_snapshots.evaluate_cross_verification(rows, mid, 'report', cfg, now=datetime.now(UTC))
        has_verified = any(r.get('match_id') == mid and r.get('stage') == 'report' and r.get('verification') == 'verified'
                           and str(r.get('is_main')) == '1' for r in rows)
        fundamental = None
        code = football_data.CODE_BY_LEAGUE.get(match['league_id'])
        if check.get('passed') and has_verified and code and fd.enabled:
            try:
                fundamental = football_data.fundamentals(fd, code, match['home'], match['away'], match['kickoff_utc'], 'home')
            except Exception as exc:
                errors.append('基本面: ' + type(exc).__name__)
        pick, why = assess(match, rows, fundamental, cfg, datetime.now(UTC))
        if pick:
            report['picks'].append(pick)
        else:
            item = {**match, 'confidence': None, 'withheld_category': why, 'withheld_reason': why}
            if mid in titan:
                item['titan007_observation'] = titan[mid]
            report['watchlist'].append(item)
    policy.apply(report)
    if report['picks']:
        # 5Dollar is supplementary only; lack of data never creates a pick.
        consensus.attach(report, fetch=lambda _sport: {}, fd5=fivedollar.Client())
        policy.apply(report)
    if notes or errors:
        report['not_covered'] += ' 数据源状态：' + '；'.join(notes + errors) + '。'
    validate(report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='采集赛程和盘口，生成日报（不会覆盖现有报告）')
    parser.add_argument('--date', default=datetime.now(UTC).strftime('%Y-%m-%d'))
    parser.add_argument('--max-fixtures', type=int, default=8, choices=range(1, 13))
    parser.add_argument('--preview', action='store_true', help='采集并验证，但不写入日报')
    args = parser.parse_args(argv)
    date.fromisoformat(args.date)
    path, _ = report_paths(args.date)
    if path.exists() and not args.preview:
        print('日报已存在，保持原有内容', file=sys.stderr)
        return
    if date.fromisoformat(args.date) != datetime.now(UTC).date():
        parser.error('只能为当前 UTC 日期采集实时数据')
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
    with path.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(f"日报已生成：观察 {len(report['watchlist'])} 场，推荐 {len(report['picks'])} 场")


if __name__ == '__main__':
    main()
