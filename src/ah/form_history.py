"""非欧洲联赛（football-data 不覆盖）的近期状态：多来源兜底链。

优先级（逐级兜底，任何一级不可用都不影响下一级，也绝不编造赛果）：
1. API-Football：联赛当季已完场赛程（1 次请求/联赛，受每日额度和停用开关约束）。
2. InferSports 已完赛事件：按 UTC 日期回填（队名与赛程同源，最稳）。只读取明确的比分字段，
   字段缺失则不计入，并在备注里写明原因。
两级结果分来源保存，状态只取「主客队都不少于 3 场」的第一个来源，不混用以免重复计场。
结果库：data/history/results.csv（可入库累积；未入库时每次运行重新回填，受 MAX_FETCH_DATES 限制）。
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone

from . import DATA
from .match import parse_dt, team_similarity

HISTORY = DATA / 'history' / 'results.csv'
COVERED = DATA / 'history' / 'covered_dates.json'
FIELDS = ['source', 'event_id', 'league_id', 'kickoff_utc', 'home', 'away', 'home_goals', 'away_goals']
FINISHED_WORDS = {'finished', 'ft', 'final', 'completed', 'closed', 'ended', 'aet', 'pen'}
MAX_FETCH_DATES = 10
LOOKBACK_DAYS = 21
SIMILARITY = 0.75
UTC = timezone.utc


def _int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def extract_score(event: dict):
    """只接受明确的整数比分；识别不了就返回 None（不推断、不补全）。"""
    candidates = []
    score = event.get('score')
    if isinstance(score, dict):
        candidates += [(score.get('home'), score.get('away')), (score.get('score_home'), score.get('score_away')),
                       (score.get('home_score'), score.get('away_score'))]
    result = event.get('result')
    if isinstance(result, dict):
        candidates += [(result.get('home'), result.get('away')), (result.get('home_score'), result.get('away_score'))]
    candidates.append((event.get('home_score'), event.get('away_score')))
    candidates.append((event.get('score_home'), event.get('score_away')))
    for home, away in candidates:
        h, a = _int(home), _int(away)
        if h is not None and a is not None:
            return h, a
    return None


def is_finished(event: dict) -> bool:
    return str(event.get('status') or '').strip().lower() in FINISHED_WORDS


def load(path=HISTORY) -> list[dict]:
    try:
        with open(path, encoding='utf-8', newline='') as stream:
            return list(csv.DictReader(stream))
    except OSError:
        return []


def save(rows: list[dict], path=HISTORY) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        for row in sorted(rows, key=lambda r: (r['kickoff_utc'], r['source'], r['event_id'])):
            writer.writerow({k: row.get(k, '') for k in FIELDS})


def _load_covered(path=COVERED) -> set:
    try:
        return set(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError):
        return set()


def _save_covered(days: set, path=COVERED) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(days)), encoding='utf-8')


def refresh_infersports(infs, now, league_by_name: dict, rows: list[dict], max_dates=MAX_FETCH_DATES,
                        lookback=LOOKBACK_DAYS, covered_path=COVERED):
    """回填最近若干天的已完赛事件。返回 (新增行, 说明列表)。"""
    notes, added = [], []
    if not (infs and getattr(infs, 'enabled', False)):
        return added, ['InferSports 未启用，跳过赛果回填']
    known = {(r['source'], r['event_id']) for r in rows}
    covered = _load_covered(covered_path)
    today = now.astimezone(UTC).date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(1, lookback + 1)]
    todo = [d for d in days if d not in covered][:max_dates]
    finished_seen = with_score = 0
    for day in todo:
        try:
            events = infs.events(day)
        except Exception as exc:  # noqa: BLE001  单日失败不影响其余日期
            notes.append(f'InferSports 赛果回填 {day} 失败：{type(exc).__name__}')
            continue
        covered.add(day)
        for event in events:
            if not is_finished(event):
                continue
            finished_seen += 1
            league_id = league_by_name.get(str((event.get('league') or {}).get('name', '')).casefold())
            score = extract_score(event)
            if not league_id or score is None:
                continue
            with_score += 1
            key = ('infersports', str(event.get('id')))
            if key in known:
                continue
            known.add(key)
            added.append({'source': 'infersports', 'event_id': str(event.get('id')), 'league_id': league_id,
                          'kickoff_utc': event.get('scheduled_at') or '',
                          'home': (event.get('home_team') or {}).get('name') or '',
                          'away': (event.get('away_team') or {}).get('name') or '',
                          'home_goals': score[0], 'away_goals': score[1]})
    if todo:
        _save_covered(covered, covered_path)
    if finished_seen and not with_score:
        notes.append('InferSports 已完赛事件没有可识别的比分字段，赛果兜底不可用')
    return added, notes


def refresh_api_football(apif, league: dict, rows: list[dict]):
    """API-Football 当季已完场赛程（1 次请求）。返回 (新增行, 说明列表)。"""
    if not (apif and getattr(apif, 'enabled', False)) or not league.get('apif'):
        return [], []
    try:
        data = apif.get('fixtures', {'league': league['apif'], 'season': league.get('season'),
                                     'status': 'FT-AET-PEN'}, ttl_key='fixtures')['response']
    except Exception as exc:  # noqa: BLE001  停用/额度/计划限制 → 交给下一级兜底
        return [], [f"API-Football 赛果（{league.get('cn') or league['id']}）不可用：{type(exc).__name__}"]
    known = {(r['source'], r['event_id']) for r in rows}
    added = []
    for fx in data or []:
        goals = (fx.get('score') or {}).get('fulltime') or fx.get('goals') or {}
        h, a = _int(goals.get('home')), _int(goals.get('away'))
        key = ('api_football', str((fx.get('fixture') or {}).get('id')))
        if h is None or a is None or key in known:
            continue
        known.add(key)
        teams = fx.get('teams') or {}
        added.append({'source': 'api_football', 'event_id': key[1], 'league_id': league['id'],
                      'kickoff_utc': (fx.get('fixture') or {}).get('date') or '',
                      'home': (teams.get('home') or {}).get('name') or '',
                      'away': (teams.get('away') or {}).get('name') or '',
                      'home_goals': h, 'away_goals': a})
    return added, []


def team_form(rows: list[dict], team: str, kickoff, n=5) -> str:
    ko = parse_dt(kickoff)
    mine = []
    for row in rows:
        try:
            when = parse_dt(row['kickoff_utc'])
        except (TypeError, ValueError, KeyError):
            continue
        if when >= ko:
            continue
        if team_similarity(row['home'], team, None) >= SIMILARITY:
            mine.append((when, int(row['home_goals']), int(row['away_goals'])))
        elif team_similarity(row['away'], team, None) >= SIMILARITY:
            mine.append((when, int(row['away_goals']), int(row['home_goals'])))
    mine.sort(key=lambda x: x[0], reverse=True)
    return ''.join('W' if a > b else 'D' if a == b else 'L' for _, a, b in mine[:n])


def fundamentals(rows: list[dict], league_id: str, home: str, away: str, kickoff, min_games=3):
    """按来源优先级取第一个主客队都不少于 min_games 场的来源；都不够则返回 None。"""
    for source in ('api_football', 'infersports'):
        pool = [r for r in rows if r['league_id'] == league_id and r['source'] == source]
        if not pool:
            continue
        form_home, form_away = team_form(pool, home, kickoff), team_form(pool, away, kickoff)
        if min(len(form_home), len(form_away)) >= min_games:
            return {'source': source, 'form': {'home': form_home, 'away': form_away}, 'errors': {}}
    return None
