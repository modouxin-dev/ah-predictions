# 展示层中文映射：只改显示，不改数据、不生成任何预测。
# 证据口径见 docs/grounding-policy.md：精确键匹配，未收录保留英文原名，绝不造词。
from __future__ import annotations

import re

import yaml

from . import CONFIG

_CACHE = {}


def _load():
    if _CACHE:
        return _CACHE
    teams, cn2en, leagues = {}, {}, {}
    try:
        a = yaml.safe_load((CONFIG / 'team_aliases.yaml').read_text(encoding='utf-8')) or {}
        # 复用既有 aliases：别名列表里第一个含中文的即视为可核对译名
        for en, vals in (a.get('aliases') or {}).items():
            for v in (vals or []):
                if re.search(r'[\u4e00-\u9fff]', str(v)):
                    teams[str(en)] = str(v)
                    cn2en.setdefault(str(v), str(en))
                    break
        for en, z in (a.get('display_zh') or {}).items():
            teams[str(en)] = str(z)
            cn2en.setdefault(str(z), str(en))
    except (OSError, yaml.YAMLError):
        pass
    try:
        lg = yaml.safe_load((CONFIG / 'leagues.yaml').read_text(encoding='utf-8')) or {}
        for x in lg.get('leagues') or []:
            cn = str(x.get('cn') or '')
            if not cn:
                continue
            for k in (x.get('name'), x.get('cn'), x.get('titan007'), x.get('id')):
                if k:
                    leagues[str(k)] = cn
    except (OSError, yaml.YAMLError):
        pass
    _CACHE.update({'teams': teams, 'cn2en': cn2en, 'leagues': leagues})
    return _CACHE


def team(name):
    # 精确匹配；未收录保留英文原名，不音译、不意译、不按联赛语言生成。
    n = str(name or '').strip()
    return _load()['teams'].get(n, n)


def original(name):
    # 中文显示名反查英文原文，供 grounding 逐字回溯。
    d = _load()
    n = str(name or '').strip()
    return d['cn2en'].get(n, n)


def replace_known(text):
    # 展示层：把自由文本（推荐理由/风险）里出现的英文原名替换成中文。
    # 按长度降序，避免短键先命中造成子串误伤；未收录的原名原样保留。
    s = str(text or '')
    teams = _load()['teams']
    for en in sorted(teams, key=len, reverse=True):
        if en and en in s:
            s = s.replace(en, teams[en])
    return s


def league(name):
    # 报告里的联赛名常已是中文（意甲/英冠），原样保留；英文名查 leagues.yaml 的 cn。
    n = str(name or '').strip()
    if not n or re.search(r'[\u4e00-\u9fff]', n):
        return n
    return _load()['leagues'].get(n, n)


_LINE_CN = {
    0.0: '平手', 0.25: '平半', 0.5: '半球', 0.75: '半一', 1.0: '一球',
    1.25: '一球半', 1.5: '球半', 1.75: '球半两球', 2.0: '两球',
}


def line_cn(x):
    # 盘口数字保留（可回溯），仅追加受控中文术语；不推断让球方向。
    if x is None:
        return '未取得'
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    if v == 0:
        return '0（平手）'
    s = f'{v:+.2f}'.rstrip('0').rstrip('.')
    label = _LINE_CN.get(round(abs(v), 2))
    return f'{s}（{label}）' if label else s
