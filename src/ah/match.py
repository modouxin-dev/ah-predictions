"""API-Football 比赛 <-> titan007/报告比赛 的映射。

规则：开球时间相差 <= 15 分钟，且主客队名都匹配（先查 config/team_aliases.yaml，
再做模糊匹配：去重音/大小写/通用词后 token 包含，或 difflib 相似度 >= 0.75）。
匹配失败时把两边队名补进 team_aliases.yaml。
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import lru_cache

import yaml

from . import CONFIG

TOLERANCE = timedelta(minutes=15)
STOPWORDS = {"fc", "cf", "sc", "afc", "ac", "as", "ssc", "cd", "kv", "fk", "sk", "club", "de", "the", "calcio"}
BJT = timezone(timedelta(hours=8))


@lru_cache(maxsize=1)
def load_aliases(path: str | None = None) -> dict[str, str]:
    """返回 {规范化别名: 规范化 API 名}。"""
    p = path or str(CONFIG / "team_aliases.yaml")
    try:
        data = yaml.safe_load(open(p, encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    out = {}
    for canon, alist in (data.get("aliases") or {}).items():
        c = norm(canon)
        out[c] = c
        for a in alist or []:
            out[norm(a)] = c
    return out


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def _latin(s: str) -> bool:
    return all(ord(ch) < 0x2E80 for ch in s)  # 不含中日韩字符时才能安全转 ascii


def tokens(name: str) -> set[str]:
    return {t for t in norm(name).split() if t not in STOPWORDS and len(t) > 1}


def _variants(name: str) -> list[str]:
    """报告中常见 '武里南联 Buriram United'：拆出中文段与拉丁段分别尝试。"""
    n = str(name).strip()
    parts = [n]
    cjk = "".join(ch for ch in n if not _latin(ch)).strip()
    lat = "".join(ch if _latin(ch) else " " for ch in n).strip()
    for x in (cjk, lat):
        if x and x != n:
            parts.append(x)
    return parts


def team_similarity(a: str, b: str, aliases: dict | None = None) -> float:
    aliases = load_aliases() if aliases is None else aliases
    best = 0.0
    for x in _variants(a):
        for y in _variants(b):
            nx, ny = norm(x), norm(y)
            if not nx or not ny:
                continue
            if nx == ny or (aliases.get(nx) and aliases.get(nx) == aliases.get(ny)) \
                    or aliases.get(nx) == ny or aliases.get(ny) == nx:
                return 1.0
            tx, ty = tokens(x), tokens(y)
            if tx and ty and (tx <= ty or ty <= tx):
                best = max(best, 0.9)
            best = max(best, SequenceMatcher(None, nx, ny).ratio())
    return best


def parse_dt(s) -> datetime:
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    s = str(s).replace("Z", "+00:00")
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def titan_kickoff_utc(hhmm: str, date_bjt: str) -> datetime:
    """titan007 bfdata 只给北京时间 HH:MM；配合北京时间日期转 UTC。"""
    return datetime.fromisoformat(f"{date_bjt}T{hhmm}:00").replace(tzinfo=BJT).astimezone(timezone.utc)


def match_score(api_fx: dict, home: str, away: str, kickoff, aliases=None, threshold: float = 0.75) -> float:
    """0 = 不匹配；否则返回 (主相似度+客相似度)/2。api_fx 为 API-Football fixture 原始对象。"""
    ko = parse_dt(api_fx["fixture"]["date"])
    if abs(ko - parse_dt(kickoff)) > TOLERANCE:
        return 0.0
    hs = team_similarity(api_fx["teams"]["home"]["name"], home, aliases)
    as_ = team_similarity(api_fx["teams"]["away"]["name"], away, aliases)
    if hs < threshold or as_ < threshold:
        return 0.0
    return (hs + as_) / 2


def find_fixture(api_fixtures: list, home: str, away: str, kickoff, aliases=None) -> dict | None:
    best, best_s = None, 0.0
    for f in api_fixtures or []:
        s = match_score(f, home, away, kickoff, aliases)
        if s > best_s:
            best, best_s = f, s
    return best


def map_titan(api_fixtures: list, titan_matches: list, date_bjt: str, aliases=None) -> dict[str, int]:
    """{titan007 match_id: api fixture id}。跨午夜的比赛同时尝试前/后一天。"""
    out = {}
    for m in titan_matches:
        if not m.get("kickoff_bjt"):
            continue
        base = titan_kickoff_utc(m["kickoff_bjt"], date_bjt)
        for shift in (0, -1, 1):
            f = find_fixture(api_fixtures, m["home"], m["away"], base + timedelta(days=shift), aliases)
            if f:
                out[m["match_id"]] = f["fixture"]["id"]
                break
    return out
