"""Create and publish a separately recorded, real-time report revision."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import DATA, daily, policy, publish_telegram, render_report, settle


def collect(path: Path, max_fixtures: int | None = None) -> dict:
    now = datetime.now(timezone.utc)
    snapshot_dir = DATA / 'snapshots' / 'odds' / 'rechecks' / path.stem
    report = daily.build_report(now.date().isoformat(), max_fixtures=max_fixtures, snapshot_dir=snapshot_dir)
    official = policy.published(report)
    observations = publish_telegram.observation_blocks(report)
    if not official and not observations:
        # 不再中断：0 场推荐仍如实发布「赛程事实清单」（非推荐、不含预测结论）。
        # 不改动 policy / observation_blocks / 评分与核验算法（见 AGENTS.md 冻结清单）。
        report['analysis_note'] = (
            '本次无可发布的正式推荐或模型观察；消息仅列本次已采集的赛程事实与不推原因，'
            '不生成任何预测、盘口或倾向。'
        )
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
    count = sum(1 for x in report.get('watchlist', []) if x.get('observation'))
    print(f'已保存复核报告：正式推荐 {len(official)} 场，分析观察 {count} 场')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['collect', 'send', 'record'])
    parser.add_argument('--path', required=True, type=Path)
    parser.add_argument('--max-fixtures', type=int, choices=range(1, 61))
    args = parser.parse_args()
    if args.action == 'collect':
        collect(args.path, args.max_fixtures)
    elif args.action == 'record':
        report = json.loads(args.path.read_text(encoding='utf-8'))
        count = render_report.append_ledger(report, DATA / 'predictions.csv')
        print(f'发送后已记录正式推荐 {count} 场')
    else:
        from . import delivery, grounding
        if not os.environ.get('TELEGRAM_BOT_TOKEN'):
            raise SystemExit('TELEGRAM_BOT_TOKEN 未配置，拒绝静默跳过')
        try:
            chat, source, _facts = delivery.resolve_chat()
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from None
        report = json.loads(args.path.read_text(encoding='utf-8'))
        gate = grounding.check_all(report)
        if not gate['ok']:
            raise SystemExit('事实闸门未通过，拒绝发送：' + '；'.join(gate['errors'][:5]))
        sha = delivery.report_fingerprint(report)
        key = 'recheck:' + args.path.stem
        ok, reason = delivery.should_send(key, sha, chat)
        if not ok:
            print(f'[recheck send] 台账已有送达记录，跳过重复发送：{reason}')
            return
        stats_path = DATA / 'stats.json'
        stats = json.loads(stats_path.read_text(encoding='utf-8')) if stats_path.exists() else None
        messages = publish_telegram.daily(report, stats, os.environ.get('SITE_URL') or publish_telegram.SITE_URL)
        ids = publish_telegram.send(messages)
        delivery.record_sent(key, sha, chat, ids,
                             run_id=os.environ.get('GITHUB_RUN_ID'), report_path=str(args.path))
        print(f'DELIVERED recheck message_ids={ids} chat={chat} chat_source={source} evidence=sendMessage')


if __name__ == '__main__':
    main()
