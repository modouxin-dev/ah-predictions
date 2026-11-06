import json
from pathlib import Path

import pytest

from ah import recheck


def report():
    return {'date': '2026-11-06', 'generated_at': '2026-11-06T08:00:00Z', 'picks': [], 'watched': [],
            'watchlist': [{'home': 'Everton', 'away': 'Coventry', 'league': '英超',
                           'kickoff_utc': '2026-11-06T20:00:00Z', 'withheld_reason': '评分不足',
                           'observation': {'pick_team': 'Everton', 'line': -0.75, 'line_open': -0.5,
                                           'odds': 0.91, 'score': 3.9, 'home_form': 'WWWWW',
                                           'away_form': 'DDDDD', 'bookmaker': 'pinnacle',
                                           'source': 'oddspapi', 'price_ts': '2026-11-06T08:00:00Z'}}]}


def test_recheck_stores_real_observation_separately_without_creating_pick(tmp_path, monkeypatch):
    monkeypatch.setattr(recheck, 'DATA', tmp_path)
    captured = []
    def build(*_args, **kwargs):
        captured.append(kwargs['snapshot_dir'])
        return report()
    monkeypatch.setattr(recheck.daily, 'build_report', build)
    path = tmp_path / 'reports' / 'recheck.json'
    recheck.collect(path)
    saved = json.loads(path.read_text())
    assert saved['edition'] == 'recheck' and not saved['picks']
    assert saved['watchlist'][0]['observation']['score'] == 3.9
    assert not recheck.settle.read_ledger(tmp_path / 'predictions.csv')
    assert captured == [tmp_path / 'snapshots' / 'odds' / 'rechecks' / 'recheck']


def test_recheck_saves_facts_only_report_when_no_observations(tmp_path, monkeypatch):
    """0 场推荐且无观察时不再中断：保存只含赛程事实的报告（见 AGENTS.md）。

    旧契约是 raise「停止发布」；新契约是如实发布，不生成任何预测。
    """
    from ah import publish_telegram as pt

    monkeypatch.setattr(recheck, 'DATA', tmp_path)
    monkeypatch.setattr(recheck.daily, 'build_report',
                        lambda *_args, **_kwargs: {
                            **report(),
                            'watchlist': [{'match_id': 'm1', 'home': 'Everton', 'away': 'Coventry',
                                           'league': '英超', 'kickoff_utc': '2026-11-06T20:00:00Z',
                                           'withheld_reason': '盘口跨来源核验未通过'}]})
    path = tmp_path / 'recheck.json'
    recheck.collect(path)
    saved = json.loads(path.read_text(encoding='utf-8'))
    assert saved['edition'] == 'recheck'
    assert saved['picks'] == []
    assert '不生成任何预测' in saved['analysis_note']
    lines = pt.fixture_facts_lines(saved)
    assert len(lines) == 1
    assert 'Everton vs Coventry' in lines[0]
    assert '非推荐' in lines[0] or '未推荐' in lines[0]
