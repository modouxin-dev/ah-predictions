"""模拟单台账 data/paper_trades.csv（路线图「回测机制」：所有筛选场次都做模拟单，1 单位记账）。

用法：python -m ah.audit.paper_trades [--fetch-scores]

来源：reports/YYYY/MM/YYYY-MM-DD.json 的 picks + watched + watchlist（从 2026-11-04 起）。
幂等：按 (date, match_id) 更新；已有的比分/收盘等回填字段在新值为空时保留。
watched 条目无 match_id 时用 "watch-<home>-<away>" 作为键。

字段约定（先知 2026-11-04 定）：
- status: active(推荐有效) / cancelled(赛前撤销推荐) / void(比赛取消、延期等作废) / watch(观察/未推荐)
- pick_side/line/odds: 所选一方视角；line 负=让球。home_line 为主队视角对照。
- watch 场次方向：路线图规定"按模型首选方向"。模型研究员尚未接入 → pick_side 留空、不结算，
  note 注明 "no_model_direction"；仍保留 home_line 供 CLV/回测对照。
- odds_format: 报告 json 的 odds_format（当前均为 hk 港盘水位，十进制 = 1 + 水位）。
- model_p / brier: 模型研究员接入前留空，展示为「暂无」。
  brier = (model_p - y)^2，y=1 赢盘 / 0 输盘；走盘、赢半、输半暂不计（待与模型研究员约定）。

收盘线：来自数据官 data/odds_snapshots/**/*.json（JSON 列表或 {"snapshots":[...]}），每条需
  match_id, captured_at(ISO, UTC), ah_home_line, odds_home, odds_away[, odds_format]；
  取 captured_at < kickoff_utc 的最后一条。目录不存在/无该场 → 留空，close_note 说明原因。

CLV（路线图「关键公式」）：
    p_fair_pick = (1/o_pick) / (1/o_pick + 1/o_opp)          # 收盘两边十进制赔率，比例法去水
    fair_close_odds = 1 / p_fair_pick
    CLV = o_bet / fair_close_odds - 1 = o_bet * p_fair_pick - 1  # o_bet 为下注时十进制赔率
  仅在"收盘盘口 == 下注盘口"（同一视角）时计算。盘口不同时路线图要求"用模型把收盘价换算到
  我们的盘口"；模型未接入 → CLV 留空，close_note="line_moved_needs_model"，另记录
  line_move = close_line - bet_line（所选一方视角，负=收盘更深，对我们有利）。
  另输出 implied_p_bet = 1/o_bet（含水，单边无法去水，仅供参考）。

比分：只在两个独立来源（API-Football fulltime 与 football-data.org）一致时填入 result（主-客，
90 分钟），不一致或只有一个来源则留空并在 score_note 说明。亚洲赛事 football-data.org 不覆盖 →
需要数据官提供第二来源。
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..settle import profit, settle

ROOT = Path(__file__).resolve().parents[3]
REPORTS = ROOT / "reports"
SNAPSHOTS = ROOT / "data" / "odds_snapshots"
OUT = ROOT / "data" / "paper_trades.csv"
START_DATE = "2026-11-04"

FIELDS = ["date", "match_id", "league", "home", "away", "kickoff_utc", "kickoff_bj", "status", "confidence",
          "pick_side", "pick_team", "line", "home_line", "line_open_home", "odds", "odds_format",
          "close_line", "close_home_line", "close_odds", "close_odds_opp", "close_captured_at", "close_note",
          "line_move", "implied_p_bet", "clv",
          "result", "score_sources", "score_note", "ah_outcome", "units",
          "model_p", "brier", "note", "updated_at",
          # 产品工程师要求的别名列（与上面同源，便于网站直接读取）
          "pick", "odds_taken", "odds_close", "stake", "pnl", "settled_at"]
BJ = timezone(timedelta(hours=8))
BACKFILL = ["close_line", "close_home_line", "close_odds", "close_odds_opp", "close_captured_at",
            "close_note", "line_move", "clv", "result", "score_sources", "score_note", "ah_outcome",
            "units", "model_p", "brier", "settled_at"]


def to_decimal(odds, fmt: str) -> float | None:
    if odds in (None, ""):
        return None
    o = float(odds)
    return o + 1.0 if (fmt or "hk") == "hk" else o


def fair_prob(o_pick: float, o_opp: float) -> float:
    """比例法去水：两边十进制赔率 -> 所选一方公平概率。"""
    a, b = 1 / o_pick, 1 / o_opp
    return a / (a + b)


def clv(o_bet: float, o_close_pick: float, o_close_opp: float) -> float:
    """CLV = o_bet * p_fair_close - 1（同一盘口）。"""
    return o_bet * fair_prob(o_close_pick, o_close_opp) - 1


def brier(p: float, outcome: str) -> float | None:
    y = {"win": 1.0, "loss": 0.0}.get(outcome)
    return None if y is None else round((p - y) ** 2, 4)


def _fmt(x, nd=4) -> str:
    return "" if x is None else (f"{x:g}" if isinstance(x, (int,)) else str(round(float(x), nd)))


def _num(x) -> str:
    return "" if x in (None, "") else f"{float(x):g}"


def rows_from_report(rep: dict) -> list[dict]:
    date, fmt = rep["date"], (rep.get("odds_format") or "hk").lower()
    fmt = "hk" if fmt.startswith("hk") else ("decimal" if fmt.startswith("dec") else fmt)
    out = []
    for p in rep.get("picks") or []:
        line = p.get("line")
        side = p.get("pick_side")
        sign = 1 if side == "home" else -1
        res = p.get("result") or {}
        status = "cancelled" if p.get("cancelled") else ("void" if res.get("settlement") == "void" else "active")
        out.append({"date": date, "match_id": p["match_id"], "league": p.get("league", ""), "home": p.get("home", ""),
                    "away": p.get("away", ""), "kickoff_utc": p.get("kickoff_utc", ""), "status": status,
                    "confidence": p.get("confidence") or "", "pick_side": side, "pick_team": p.get("pick_team", ""),
                    "line": _num(line), "home_line": _num(sign * float(line)) if line is not None else "",
                    "line_open_home": _num(sign * float(p["line_open"])) if p.get("line_open") is not None else "",
                    "odds": _num(p.get("odds")), "odds_format": fmt,
                    "implied_p_bet": _fmt(1 / to_decimal(p["odds"], fmt)) if p.get("odds") else "",
                    "settled_at": to_bj(res["settled_at"]) if status != "active" and res.get("settled_at") else "",
                    "note": ("撤销：" + p.get("cancel_reason", "")) if status == "cancelled" else ""})
    for w in rep.get("watchlist") or []:
        out.append({"date": date, "match_id": w["match_id"], "league": w.get("league", ""), "home": w.get("home", ""),
                    "away": w.get("away", ""), "kickoff_utc": w.get("kickoff_utc", ""), "status": "watch",
                    "confidence": w.get("confidence") or "", "home_line": _num(w.get("ah_home_line")),
                    "line_open_home": _num(w.get("ah_home_line_open")), "odds_format": fmt,
                    "note": f"no_model_direction；{w.get('withheld_category') or ''}：{w.get('withheld_reason') or ''}"})
    for w in rep.get("watched") or []:
        if (w.get("away") or "-").strip() in ("", "-") or w.get("home") == "多场":
            continue  # 覆盖说明类条目（非单场比赛），不记模拟单
        mid = w.get("match_id") or f"watch-{w.get('home', '')}-{w.get('away', '')}"
        out.append({"date": date, "match_id": mid, "league": w.get("league", ""), "home": w.get("home", ""),
                    "away": w.get("away", ""), "kickoff_utc": w.get("kickoff_utc", ""), "status": "watch",
                    "odds_format": fmt,
                    "note": "no_model_direction；报告未给 match_id/开球/盘口；" + (w.get("reason_skipped") or "")})
    return out


def to_bj(ts) -> str:
    """任意 ISO 时间/aware datetime -> 北京时间 ISO 8601（+08:00，秒精度）。"""
    dt = ts if isinstance(ts, datetime) else _parse(ts)
    return dt.astimezone(BJ).replace(microsecond=0).isoformat()


def add_aliases(r: dict) -> None:
    """产品工程师别名列：pick=pick_team, odds_taken=odds, odds_close=close_odds, pnl=units；
    stake：有模拟方向且未撤销/作废为 1，撤销/作废为 0，无方向留空。"""
    r["pick"] = r.get("pick_team") or ({"home": r.get("home"), "away": r.get("away")}.get(r.get("pick_side")) or "")
    r["odds_taken"], r["odds_close"], r["pnl"] = r.get("odds", ""), r.get("close_odds", ""), r.get("units", "")
    if r.get("status") in ("cancelled", "void"):
        r["stake"] = "0"
    else:
        r["stake"] = "1" if r.get("pick_side") in ("home", "away") else ""


def load_snapshots(root: Path = SNAPSHOTS) -> dict[str, list[dict]] | None:
    if not root.exists():
        return None
    by = {}
    for f in sorted(root.rglob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items = d.get("snapshots", []) if isinstance(d, dict) else d
        for s in items or []:
            if isinstance(s, dict) and s.get("match_id"):
                by.setdefault(s["match_id"], []).append(s)
    return by


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def apply_closing(r: dict, snaps: dict | None) -> None:
    if snaps is None:
        r["close_note"] = "data/odds_snapshots/ 不存在（待数据官提供）"
        return
    if not r.get("kickoff_utc"):
        r["close_note"] = "无开球时间，无法匹配快照"
        return
    ko = _parse(r["kickoff_utc"])
    pre = [s for s in snaps.get(r["match_id"], []) if s.get("captured_at") and _parse(s["captured_at"]) < ko]
    if not pre:
        r["close_note"] = "odds_snapshots 中无该场开球前快照"
        return
    s = max(pre, key=lambda s: _parse(s["captured_at"]))
    fmt = s.get("odds_format", "hk")
    hl = float(s["ah_home_line"])
    r["close_home_line"], r["close_captured_at"] = _num(hl), to_bj(s["captured_at"])
    side = r.get("pick_side")
    if side not in ("home", "away"):
        r["close_note"] = "无模拟方向，仅记录主队视角收盘线"
        return
    oh, oa = to_decimal(s.get("odds_home"), fmt), to_decimal(s.get("odds_away"), fmt)
    cl = hl if side == "home" else -hl
    op, oo = (oh, oa) if side == "home" else (oa, oh)
    r["close_line"] = _num(cl)
    r["close_odds"] = _fmt(op - 1 if r.get("odds_format") == "hk" and op else op)
    r["close_odds_opp"] = _fmt(oo - 1 if r.get("odds_format") == "hk" and oo else oo)
    bet = float(r["line"])
    r["line_move"] = _num(cl - bet)
    ob = to_decimal(r.get("odds"), r.get("odds_format"))
    if cl != bet:
        r["close_note"], r["clv"] = "line_moved_needs_model", ""
    elif ob and op and oo:
        r["clv"], r["close_note"] = _fmt(clv(ob, op, oo)), ""
    else:
        r["close_note"] = "缺少下注或收盘水位"


def apply_settlement(r: dict) -> None:
    if r.get("status") in ("cancelled", "void"):
        r["ah_outcome"], r["units"] = "void", "0"
        return
    if not r.get("result") or r.get("pick_side") not in ("home", "away"):
        return
    h, a = (int(x) for x in r["result"].split("-"))
    pg, og = (h, a) if r["pick_side"] == "home" else (a, h)
    out = settle(pg, og, float(r["line"]))
    r["ah_outcome"] = out
    o = r.get("odds")
    r["units"] = _fmt(profit(out, float(o) if o else None, odds_format=r.get("odds_format") or "hk"))
    if r.get("model_p"):
        r["brier"] = _fmt(brier(float(r["model_p"]), out))


def two_source_score(r: dict, api_client, fd_client) -> tuple[str | None, str, str]:
    """返回 (result 或 None, sources, note)。两源一致才返回比分。"""
    from ..match import find_fixture, parse_dt
    from ..settle import _fd_score
    a = None
    try:
        mid = r["match_id"]
        if mid.startswith("apif-"):
            fid = int(mid.split("-", 1)[1])
        else:
            ko = parse_dt(r["kickoff_utc"])
            fx = find_fixture(api_client.fixtures_by_date(ko.strftime("%Y-%m-%d")), r["home"], r["away"], ko)
            fid = fx["fixture"]["id"] if fx else None
        res = api_client.fixture_result(fid) if fid else None
        if res and res.get("finished") and res.get("home_goals") is not None:
            a = (res["home_goals"], res["away_goals"])
    except Exception as e:  # noqa: BLE001
        return None, "", f"API-Football 出错：{type(e).__name__}"
    b = _fd_score(fd_client, r)
    if a and b and a == b:
        return f"{a[0]}-{a[1]}", "api-football+football-data.org", ""
    if a and b:
        return None, "", f"两源不一致 API-Football {a} vs football-data.org {b}"
    have = "API-Football" if a else ("football-data.org" if b else "无")
    return None, "", f"仅有来源：{have}，需第二来源"


def build(reports_root: Path = REPORTS, out: Path = OUT, snapshots_root: Path = SNAPSHOTS,
          score_fetcher=None, now: datetime | None = None, start: str = START_DATE) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    existing = {}
    if out.exists():
        for r in csv.DictReader(out.open(encoding="utf-8")):
            existing[(r["date"], r["match_id"])] = r
    snaps = load_snapshots(snapshots_root)
    rows = {}
    for f in sorted(reports_root.rglob("????-??-??.json")):
        if f.stem < start:
            continue
        rep = json.loads(f.read_text(encoding="utf-8"))
        for r in rows_from_report(rep):
            rows[(r["date"], r["match_id"])] = r
    real = [r for r in rows.values() if not r["match_id"].startswith("watch-")]
    for key in [k for k in rows if k[1].startswith("watch-")]:
        w = rows[key]
        if any(x["home"].startswith(w["home"]) and x["away"].startswith(w["away"]) for x in real if w["home"]):
            del rows[key]  # 同一场已在其他报告中以带 match_id 的条目出现，避免重复记账
    result = []
    for key, r in rows.items():
        row = {k: "" for k in FIELDS}
        old = existing.get(key, {})
        for k in BACKFILL:
            if old.get(k):
                row[k] = old[k]
        row.update({k: v for k, v in r.items() if v not in (None,)})
        if not row.get("close_line") and not row.get("close_home_line"):
            apply_closing(row, snaps)
        if (score_fetcher and not row["result"] and row["kickoff_utc"]
                and row["status"] != "void"
                and (now - _parse(row["kickoff_utc"])).total_seconds() >= 110 * 60):
            res, src, note = score_fetcher(row)
            row["result"], row["score_sources"], row["score_note"] = res or "", src, note
        elif not row["result"] and not row["score_note"]:
            row["score_note"] = "赛果未出/待回填"
        apply_settlement(row)
        if row["ah_outcome"] and not row["settled_at"]:
            row["settled_at"] = to_bj(now)
        row["updated_at"] = to_bj(now)
        row["kickoff_bj"] = to_bj(row["kickoff_utc"]) if row["kickoff_utc"] else ""
        add_aliases(row)
        result.append(row)
    result.sort(key=lambda x: (x["date"], x["kickoff_utc"], x["match_id"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(result)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="生成/更新模拟单台账 data/paper_trades.csv")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--fetch-scores", action="store_true", help="用 API-Football + football-data.org 两源回填比分")
    a = ap.parse_args()
    fetcher = None
    if a.fetch_scores:
        from ..api_football import Client
        from ..football_data import Client as FD
        api, fd = Client(), FD()
        fetcher = lambda r: two_source_score(r, api, fd)  # noqa: E731
    rows = build(out=Path(a.out), score_fetcher=fetcher)
    from collections import Counter
    print(f"paper_trades: {len(rows)} rows", dict(Counter(r["status"] for r in rows)))


if __name__ == "__main__":
    main()
