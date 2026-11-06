# 因子证据合同（observation 阶段）：只记录来源与覆盖，不参与 score。
# 依据：深度研究报告第一/三节——声明有权重 != 数据充足；缺失用 None/未取得，不填 0。
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

FACTORS = [
    ('line_movement', '初盘→即时盘口变化', 'report.line / report.line_open', 'scoring'),
    ('form', '近期状态', 'football-data.org / 5DollarFootballAPI / form_history', 'scoring'),
    ('h2h', '交锋记录', 'football-data.org（可选）', 'scoring'),
    ('home_away', '主客场分离战绩', '未取得（同源主客统计未接入）', 'missing'),
    ('injuries', '伤停 / 首发', '未取得（API-Football coverage 未逐场取证）', 'missing'),
    ('xg', 'xG 攻防质量', '未取得（无授权接口）', 'missing'),
    ('motivation', '战意 / 排名', '未取得（积分榜未接入评分）', 'missing'),
    ('line_value', '盘口价值（去水 edge）', 'report.market_consensus（observation，未参与评分）', 'observation'),
]


def audit(report: dict) -> dict:
    # 逐场统计：哪些因子有真实值、哪些标未取得；不填 0、不推断、不生成理由。
    picks = report.get('picks') or []
    per = {}
    for key, label, source, status in FACTORS:
        per[key] = {
            'label': label, 'source': source, 'status': status,
            'mode': 'scoring' if status == 'scoring' else 'observation',
            'picks_with_value': 0, 'picks_total': len(picks),
        }
    for p in picks:
        if p.get('line') is not None and p.get('line_open') is not None:
            per['line_movement']['picks_with_value'] += 1
        if any('近期赛果' in str(r) for r in (p.get('reasons') or [])):
            per['form']['picks_with_value'] += 1
        if any('交锋' in str(r) for r in (p.get('reasons') or [])):
            per['h2h']['picks_with_value'] += 1
        if p.get('market_consensus'):
            per['line_value']['picks_with_value'] += 1
    return {
        'checked_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'report_date': report.get('date'),
        'candidate_picks': len(picks),
        'note': '规则档位，未经独立回测；observation 因子不参与 score；missing 因子不填 0',
        'factors': per,
        'evidence_hash': hashlib.sha256(
            json.dumps(picks, sort_keys=True, ensure_ascii=False, default=str).encode('utf-8')
        ).hexdigest()[:16],
    }
