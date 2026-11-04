"""用 API-Football 为候选比赛生成 analyze 输入（伤停、交锋、状态、多公司亚盘）。

产出 dict 可直接放进报告研究笔记；suggested_features 给 analyze.Candidate.features 作参考值
（仍需研究代理结合新闻复核，尤其是伤停的重要性无法只靠人数判断）。
"""
from __future__ import annotations

import json
from pathlib import Path

from .api_football import (ApiFootballError, Client, form_from_fixtures, main_ah_line,
                           summarize_h2h, summarize_injuries)


def local_fixture_history(cache_dir: Path) -> list:
    """读取本地缓存的所有 /fixtures?date 响应（用于计算近期状态）。"""
    out = []
    for p in sorted((Path(cache_dir) / "fixtures").glob("*.json")):
        try:
            out.extend(json.loads(p.read_text(encoding="utf-8"))["data"].get("response") or [])
        except (ValueError, KeyError):
            continue
    return out


def injury_feature(inj: dict, home: str, away: str, pick_side: str) -> float:
    """缺阵(Missing Fixture)记 1、存疑(Questionable)记 0.5；对手缺得越多越正。每 2 人 1 分，封顶 ±2。"""
    def load(team):
        return sum(1 if p["type"] == "Missing Fixture" else 0.5 for p in inj.get(team, []))
    mine, opp = (load(home), load(away)) if pick_side == "home" else (load(away), load(home))
    return max(-2.0, min(2.0, (opp - mine) / 2))


def h2h_feature(h: dict) -> float:
    if h["played"] < 3:
        return 0.0
    return max(-2.0, min(2.0, (h["w"] - h["l"]) / h["played"] * 2))


def form_feature(mine: str, opp: str) -> float:
    pts = lambda s: sum({"W": 3, "D": 1}.get(c, 0) for c in s) / max(len(s), 1)
    if len(mine) < 3 or len(opp) < 3:
        return 0.0
    return max(-2.0, min(2.0, pts(mine) - pts(opp)))


def enrich(fx: dict, pick_side: str = "home", client: Client | None = None, injuries_covered: bool = True,
           want_odds: bool = True, want_lineups: bool = False) -> dict:
    """fx 为 fetch_fixtures.normalize_api 的输出。每项失败都不影响其他项（记录到 errors）。"""
    c = client or Client()
    fid, hid, aid = fx["api_fixture_id"], fx["home_id"], fx["away_id"]
    out: dict = {"api_fixture_id": fid, "errors": {}, "suggested_features": {}}
    pick_id = hid if pick_side == "home" else aid

    def safe(name, fn):
        try:
            return fn()
        except ApiFootballError as e:
            out["errors"][name] = str(e)
            return None

    if injuries_covered:
        raw = safe("injuries", lambda: c.injuries(fid))
        if raw is not None:
            inj = summarize_injuries(raw)
            out["injuries"] = inj
            out["suggested_features"]["injuries"] = injury_feature(inj, fx["home"], fx["away"], pick_side)
    else:
        out["injuries"] = None  # 该联赛无伤停覆盖 → 走新闻预览
    raw = safe("h2h", lambda: c.h2h(hid, aid, last=10))
    if raw is not None:
        h = summarize_h2h(raw, pick_id)
        out["h2h"] = h
        out["suggested_features"]["h2h"] = h2h_feature(h)
    hist = local_fixture_history(c.cache_dir)
    fh = form_from_fixtures(hist, hid, before=fx["kickoff_utc"].replace("Z", "+00:00"))
    fa = form_from_fixtures(hist, aid, before=fx["kickoff_utc"].replace("Z", "+00:00"))
    out["form"] = {"home": fh, "away": fa, "note": "由本地累积的每日赛程计算，样本随运行天数增加"}
    out["suggested_features"]["form"] = form_feature(fh, fa) if pick_side == "home" else form_feature(fa, fh)
    if want_odds:
        raw = safe("odds", lambda: c.odds(fid))
        if raw is not None:
            out["ah_market"] = main_ah_line(raw)
    if want_lineups:
        raw = safe("lineups", lambda: c.lineups(fid))
        if raw is not None:
            out["lineups"] = [{"team": l["team"]["name"], "formation": l.get("formation"),
                               "startXI": [p["player"]["name"] for p in l.get("startXI", [])]} for l in raw]
    return out
