"""Create and publish a separately recorded, real-time report revision."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import DATA, daily, policy, publish_telegram, render_report, settle


def collect(path: Path, max_fixtures: int = 12) -> dict:
    now = datetime.now(timezone.utc)
    report = daily.build_report(now.date().isoformat(), max_fixtures=max_fixtures)
    official = policy.published(report)
    observations = publish_telegram.observation_blocks(report)
    if not official and not observations:
        raise RuntimeError('本次没有可核验的推荐或模型观察，停止发布')
    existing = {(r.get('date'), r.get('match_id')) for r in settle.read_ledger(DATA / 'predictions.csv')}
    if any((report['date'], p['match_id']) in existing for p in official):
        raise RuntimeError('当天已有相同比赛的正式记录，停止重复发布')
    report['edition'] = 'recheck'
    try:
        relative = path.resolve().relative_to(Path.cwd().resolve()).as_posix()
        repository = os.environ.get('GITHUB_REPOSITORY', 'modouxin-dev/ah-predictions')
        report['revision_url'] = f'https://github.com/{repository}/blob/main/{relative}'
    except ValueError:
        pass
    render_report.validate(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    render_report.append_ledger(report, DATA / 'predictions.csv')
    count = sum(1 for x in report.get('watchlist', []) if x.get('observation'))
    print(f'已保存复核报告：正式推荐 {len(official)} 场，分析观察 {count} 场')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['collect', 'send'])
    parser.add_argument('--path', required=True, type=Path)
    parser.add_argument('--max-fixtures', type=int, default=12, choices=range(1, 13))
    args = parser.parse_args()
    if args.action == 'collect':
        collect(args.path, args.max_fixtures)
    else:
        if not os.environ.get('TELEGRAM_BOT_TOKEN') or not os.environ.get('TELEGRAM_CHAT_ID'):
            raise SystemExit('Telegram 配置缺失，停止发送')
        report = json.loads(args.path.read_text(encoding='utf-8'))
        stats_path = DATA / 'stats.json'
        stats = json.loads(stats_path.read_text(encoding='utf-8')) if stats_path.exists() else None
        messages = publish_telegram.daily(report, stats, os.environ.get('SITE_URL') or publish_telegram.SITE_URL)
        try:
            publish_telegram.send(messages)
        except Exception as exc:
            raise SystemExit(f'Telegram 发送失败：{type(exc).__name__}') from None


if __name__ == '__main__':
    main()
