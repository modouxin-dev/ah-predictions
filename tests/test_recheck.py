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
    monkeypatch.setattr(recheck.daily, 'build_report', lambda *_args, **_kwargs: report())
    path = tmp_path / 'reports' / 'recheck.json'
    recheck.collect(path)
    saved = json.loads(path.read_text())
    assert saved['edition'] == 'recheck' and not saved['picks']
    assert saved['watchlist'][0]['observation']['score'] == 3.9
    assert not recheck.settle.read_ledger(tmp_path / 'predictions.csv')


def test_recheck_does_not_save_or_publish_empty_analysis(tmp_path, monkeypatch):
    monkeypatch.setattr(recheck, 'DATA', tmp_path)
    monkeypatch.setattr(recheck.daily, 'build_report',
                        lambda *_args, **_kwargs: {**report(), 'watchlist': []})
    path = tmp_path / 'recheck.json'
    with pytest.raises(RuntimeError, match='停止发布'):
        recheck.collect(path)
    assert not path.exists()
