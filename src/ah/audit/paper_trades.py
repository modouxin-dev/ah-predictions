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
- model_p / brier: 来自模型研究员 paper_trades_model_p_<时间戳>.csv（目录 --model-dir / env AH_MODEL_P_DIR，
  见 ah.audit.model_p 的校验）。model_version=market-baseline-v0，model_label=基准。无合格模型行 → 留空（展示「暂无」）。
  brier = (model_p − y)²，y 为按模型首选方向的结算得分：赢1/赢半0.75/走0.5/输半0.25/输0（汇总取均值）。
  推荐行方向以先知为准，模型方向另记 model_pick_side/model_line；watch 行用模型首选方向，stake=1。

收盘线：数据官 data/odds_snapshots/<报告日期>.csv（目录 --snapshots / env AH_ODDS_SNAPSHOTS）。
  只用 stage=close、captured_at 与 as_of 早于开球；ah_home_line 主队视角负=主让；odds_format=decimal。
  公司优先 pinnacle > sbobet > singbet > 其他；来源优先 oddspapi > odds_api > api_football；取最后一次抓取。
  close_line/close_home_line 记该公司 is_main=1 的主线；close_odds/CLV 用同一公司同一时刻下注盘口那条线
  （相邻线 is_main=0 也可），没有这条线 → CLV 留空。

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
from ah.degrade import record as _degrade_fact

import argparse
import os
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..settle import profit, settle

ROOT = Path(__file__).resolve().parents[3]
REPORTS = ROOT / "reports"
_SNAP_DEFAULT = next((p for p in (ROOT / "data" / "odds_snapshots", ROOT / "data" / "snapshots" / "odds") if p.exists()),
                     ROOT / "data" / "odds_snapshots")  # 数据官正在把目录迁到 data/snapshots/odds
SNAPSHOTS = Path(os.environ.get("AH_ODDS_SNAPSHOTS", _SNAP_DEFAULT))
MODEL_DIR = Path(os.environ.get("AH_MODEL_P_DIR", ROOT / "research" / "outputs"))
BOOK_PRIORITY = ("pinnacle", "sbobet", "singbet")
SOURCE_PRIORITY = ("oddspapi", "odds_api", "api_football")
OUT = ROOT / "data" / "paper_trades.csv"
START_DATE = "2026-11-04"

FIELDS = ["date", "match_id", "league", "home", "away", "kickoff_utc", "kickoff_bj", "status", "confidence",
          "pick_side", "pick_team", "line", "home_line", "line_open_home", "odds", "odds_format",
          "close_line", "close_home_line", "close_odds", "close_odds_opp", "close_captured_at", "close_bookmaker",
          "close_source", "close_note", "line_move", "implied_p_bet", "clv",
          "result", "score_sources", "score_note", "ah_outcome", "units",
          "model_version", "model_label", "model_pick_side", "model_line", "model_generated_at", "model_y",
          "model_p", "brier", "model_margin", "model_margin_flag", "model_note", "note", "updated_at",
          # 产品工程师要求的别名列（与上面同源，便于网站直接读取）
          "pick", "odds_taken", "odds_close", "stake", "pnl", "settled_at"]
from ..timeutil import BJ
BACKFILL = ["result", "score_sources", "score_note", "settled_at"]  # 收盘/模型每次按最新文件重算


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


SCORE = {"win": 1.0, "half_win": 0.75, "push": 0.5, "half_loss": 0.25, "loss": 0.0}


def brier(p: float, y) -> float | None:
    """(p − y)²；y 可传结算得分或 outcome 字符串（赢1/赢半.75/走.5/输半.25/输0）。"""
    if isinstance(y, str):
        y = SCORE.get(y)
    return None if y is None else round((p - y) ** 2, 6)


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
    """读取数据官 data/odds_snapshots/*.csv（docs/odds_snapshots.md 格式）。
    ah_home_line：主队视角，负=主让（与报告 watchlist 同号）；odds_format=decimal。
    目录不存在返回 None。"""
    root = Path(root)
    if not root.exists():
        return None
    by: dict[str, list[dict]] = {}
    for f in sorted(root.glob("*.csv")):
        for s in csv.DictReader(f.open(encoding="utf-8-sig")):
            if s.get("match_id") and s.get("odds_home") and s.get("odds_away") and s.get("ah_home_line") != "":
                by.setdefault(s["match_id"], []).append(s)
    return by


def _rank(s: dict) -> tuple:
    b, src = (s.get("bookmaker") or "").lower(), (s.get("source") or "").lower()
    return (BOOK_PRIORITY.index(b) if b in BOOK_PRIORITY else len(BOOK_PRIORITY),
            SOURCE_PRIORITY.index(src) if src in SOURCE_PRIORITY else len(SOURCE_PRIORITY))


def select_close(snaps: list[dict], kickoff: str, bet_home_line: float | None):
    """收盘选取：stage=close、captured_at 与 as_of 均早于开球；公司优先 pinnacle>sbobet>singbet>其他，
    来源优先 oddspapi>odds_api>api_football；取该公司/来源最后一次抓取。
    返回 (main 行 is_main=1, 同一公司/来源/抓取时刻下 ah_home_line == 下注盘口的行 或 None)。"""
    ko = _parse(kickoff)
    c = [s for s in snaps if s.get("stage") == "close" and _parse(s["captured_at"]) < ko
         and (not s.get("as_of") or _parse(s["as_of"]) < ko)]
    mains = [s for s in c if str(s.get("is_main")) in ("1", "true", "True")]
    if not mains:
        return None, None
    best_rank = min(_rank(s) for s in mains)
    main = max((s for s in mains if _rank(s) == best_rank), key=lambda s: _parse(s["captured_at"]))
    same = None
    if bet_home_line is not None:
        same = next((s for s in c if s.get("bookmaker") == main.get("bookmaker") and s.get("source") == main.get("source")
                     and s["captured_at"] == main["captured_at"] and float(s["ah_home_line"]) == bet_home_line), None)
    return main, same


def apply_closing(r: dict, snaps: dict | None) -> None:
    if snaps is None:
        r["close_note"] = "odds_snapshots 目录不存在"
        return
    if not r.get("kickoff_utc"):
        r["close_note"] = "无开球时间，无法匹配快照"
        return
    rows = snaps.get(r["match_id"], [])
    if not rows:
        r["close_note"] = "odds_snapshots 中无该场"
        return
    side = r.get("pick_side")
    bet_hl = float(r["home_line"]) if side in ("home", "away") and r.get("home_line") else None
    main, same = select_close(rows, r["kickoff_utc"], bet_hl)
    if main is None:
        r["close_note"] = "无 stage=close 的开球前快照"
        return
    hl = float(main["ah_home_line"])
    r["close_home_line"], r["close_captured_at"] = _num(hl), to_bj(main["captured_at"])
    r["close_bookmaker"], r["close_source"] = main.get("bookmaker", ""), main.get("source", "")
    if side not in ("home", "away"):
        r["close_note"] = "无模拟方向，仅记录主队视角收盘主线"
        return
    sgn = 1 if side == "home" else -1
    r["close_line"] = _num(sgn * hl)
    r["line_move"] = _num(sgn * hl - float(r["line"]))
    use = same or main
    fmt = use.get("odds_format") or "decimal"
    oh, oa = to_decimal(use["odds_home"], fmt), to_decimal(use["odds_away"], fmt)
    op, oo = (oh, oa) if side == "home" else (oa, oh)
    hk = r.get("odds_format") == "hk"
    r["close_odds"], r["close_odds_opp"] = _fmt(op - 1 if hk else op), _fmt(oo - 1 if hk else oo)
    ob = to_decimal(r.get("odds"), r.get("odds_format"))
    if same is None:
        r["close_note"] = "line_moved_needs_model（收盘快照无下注盘口这条线）"
    elif ob:
        r["clv"], r["close_note"] = _fmt(clv(ob, op, oo)), ("" if hl == bet_hl else "收盘主线不同，CLV 用同公司相邻线（下注盘口）")
    else:
        r["close_note"] = "缺少下注赔率"


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def _model_brier(r: dict) -> None:
    """按模型首选方向结算 → y（赢1/赢半.75/走.5/输半.25/输0），brier=(model_p−y)²。"""
    if not (r.get("model_p") and r.get("result") and r.get("model_pick_side") in ("home", "away")):
        return
    h, a = (int(x) for x in r["result"].split("-"))
    pg, og = (h, a) if r["model_pick_side"] == "home" else (a, h)
    y = SCORE[settle(pg, og, float(r["model_line"]))]
    r["model_y"], r["brier"] = _fmt(y), _fmt(brier(float(r["model_p"]), y))


def apply_model(r: dict, m: dict | None, reject: str | None) -> None:
    """合并模型行（已通过 model_p.validate_row）。推荐行方向以先知为准，只记模型方向对照；
    watch 行用模型首选方向填 pick_side/line/stake=1，下注价取模型文件的 Pinnacle 报告时价格（decimal）。"""
    if m is None:
        if reject:
            r["model_note"] = reject
        return
    if r.get("home_line") and float(m["home_line"]) != float(r["home_line"]):
        r["model_note"] = f"不并入：模型 home_line {m['home_line']} ≠ 台账 {r['home_line']}"
        return
    r["model_version"], r["model_label"] = m.get("model_version", ""), m.get("baseline_label", "") or "基准"
    r["model_pick_side"], r["model_line"] = m["pick_side"], _num(m["line"])
    r["model_generated_at"], r["model_p"] = to_bj(m["generated_at"]), _fmt(float(m["model_p"]), 6)
    r["model_note"] = f"{m.get('decision', '')}；{m['_file']}"
    r["model_margin"], r["model_margin_flag"] = m.get("margin", "") or "", m.get("margin_flag", "") or ""
    if r["model_margin_flag"] == "high":
        r["model_note"] += "；margin_flag=high（抽水>5%，待数据官核实是否旧价，结论暂不采信）"
    if r.get("status") == "watch" and not r.get("pick_side"):
        r["pick_side"], r["line"] = m["pick_side"], _num(m["line"])
        r["home_line"] = r["home_line"] or _num(m["home_line"])
        r["pick_team"] = r["home"] if m["pick_side"] == "home" else r["away"]
        if m.get("pick_odds_pinnacle"):
            r["odds"], r["odds_format"] = _num(m["pick_odds_pinnacle"]), "decimal"
            r["implied_p_bet"] = _fmt(1 / float(m["pick_odds_pinnacle"]))
        r["note"] = r["note"].replace("no_model_direction；", "model_direction（基准）；")


def apply_settlement(r: dict) -> None:
    if r.get("status") in ("cancelled", "void"):
        r["ah_outcome"], r["units"] = "void", "0"
        _model_brier(r)
        return
    if not r.get("result") or r.get("pick_side") not in ("home", "away"):
        return
    h, a = (int(x) for x in r["result"].split("-"))
    pg, og = (h, a) if r["pick_side"] == "home" else (a, h)
    out = settle(pg, og, float(r["line"]))
    r["ah_outcome"] = out
    o = r.get("odds")
    r["units"] = _fmt(profit(out, float(o) if o else None, odds_format=r.get("odds_format") or "hk"))
    _model_brier(r)


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
        _degrade_fact("paper_trades", "api_football_score", e)
        return None, "", f"API-Football 出错：{type(e).__name__}"
    b = _fd_score(fd_client, r)
    if a and b and a == b:
        return f"{a[0]}-{a[1]}", "api-football+football-data.org", ""
    if a and b:
        return None, "", f"两源不一致 API-Football {a} vs football-data.org {b}"
    have = "API-Football" if a else ("football-data.org" if b else "无")
    return None, "", f"仅有来源：{have}，需第二来源"


def build(reports_root: Path = REPORTS, out: Path = OUT, snapshots_root: Path = SNAPSHOTS,
          score_fetcher=None, now: datetime | None = None, start: str = START_DATE,
          model_dir: Path | None = MODEL_DIR) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    existing = {}
    if out.exists():
        for r in csv.DictReader(out.open(encoding="utf-8")):
            existing[(r["date"], r["match_id"])] = r
    snaps = load_snapshots(snapshots_root)
    from .model_p import load as load_model
    models, checks = load_model(model_dir) if model_dir and Path(model_dir).exists() else ({}, [])
    rejects = {}
    for c in checks:
        if not c["ok"]:
            rejects[(c["date"], c["match_id"])] = f"模型行未并入：{c['reasons']}（{c['file']}）"
    build.model_checks = checks
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
        apply_model(row, models.get(key), rejects.get(key))
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
    ap.add_argument("--snapshots", default=str(SNAPSHOTS), help="odds_snapshots 目录（env AH_ODDS_SNAPSHOTS）")
    ap.add_argument("--model-dir", default=str(MODEL_DIR), help="model_p 文件目录（env AH_MODEL_P_DIR）")
    ap.add_argument("--fetch-scores", action="store_true", help="用 API-Football + football-data.org 两源回填比分")
    a = ap.parse_args()
    fetcher = None
    if a.fetch_scores:
        from ..api_football import Client
        from ..football_data import Client as FD
        api, fd = Client(), FD()
        fetcher = lambda r: two_source_score(r, api, fd)  # noqa: E731
    rows = build(out=Path(a.out), score_fetcher=fetcher, snapshots_root=Path(a.snapshots), model_dir=Path(a.model_dir))
    bad = [c for c in getattr(build, "model_checks", []) if not c["ok"]]
    print(f"model_p 行校验：{len(build.model_checks) - len(bad)} 合格 / {len(bad)} 不合格")
    from collections import Counter
    print(f"paper_trades: {len(rows)} rows", dict(Counter(r["status"] for r in rows)))


if __name__ == "__main__":
    main()
