"""CLV 台账回填（DATA SOURCE:）：只写仓库里真实存在的盘口事实，缺的就写「未取得」。

背景与诚实边界
--------------
`data/predictions.csv` 已有 `final_line` / `final_odds`（收盘线，2/24 行有值），但
`line_open` / `price_ts` 从未回填；`data/snapshots/odds/*.csv` 里当前只有 `open`
与 `report` 两个 stage，**没有任何 `stage=close` 行**（`config/sources.yaml` 与
`odds_snapshots.py` 已定义 close 能力，但没有 workflow 跑它）。

所以本模块：
1. 从报告 JSON 的 picks 回填 `line_open` / `price_ts`（同 `line` 的「所选方视角」）；
2. `close_line` 优先取台账已有的 `final_line`；其次取快照 `stage=close` 行，并用
   「同场 report 阶段快照与报告 line 的映射」自动学习视角换算，学不出就写未取得；
3. `clv = line - close_line`（两者同为所选方视角；正数 = 拿到比收盘更好的线），
   **只有两边都真实存在时才计算**；
4. **口径对齐仓库既有定义**（`src/ah/audit/experiment_track.py` 的 definitions）：
   - `line_move = line - close_line`：所选方向的收盘盘口差，正 = 拿到比收盘更好的线；
   - `clv`（价格型）直接复用 `src/ah/audit/experiment_track.py::clv()`：
     `o_bet(十进制) × p_fair(收盘去水) − 1`，仅收盘盘口=下注盘口时计算；
     `line_move` 复用同模块的 `line_move()`。缺收盘两侧水位就写「未取得」，
     不另立口径、不用线差冒充 CLV；
   - `open_to_report_move = line - line_open` 只是开盘→报告位移，明确不是 CLV。
5. 不补造任何盘口、水位、比分、赛程；不改评分、核验、阈值、展示；不写回报告。

收盘线采集由新增的 `.github/workflows/close-snapshots.yml` 负责，跑起来之后本模块
才能在后续日期上产出非空的 `clv`。在那之前，绝大多数行的 `clv` 就是「未取得」——
这是事实，不是失败。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import DATA, REPORTS, ROOT

MISSING = "未取得"

# 新增列（向后兼容：旧行缺列时补空，不删除任何已有列）
# 收盘列名**复用** experiment_track.py 原有 EXTRA_FIELDS，不另起一套名字。
NEW_COLS = ["line_open", "price_ts", "open_to_report_move", "close_line", "close_odds",
            "close_odds_opp", "close_fair_p", "close_captured_at", "line_move", "clv",
            "clv_basis", "truth_source"]

NOTE = (
    "口径与列名直接复用 src/ah/audit/experiment_track.py 的 line_move() / clv() / "
    "close_* 字段：line_move=所选方向收盘线差（正=拿到更好的线）；"
    "clv=o_bet×p_fair(收盘去水)-1（仅收盘盘口=下注盘口时），需收盘两侧水位去水，"
    "缺数据写『未取得』，不另立口径、不用线差冒充 CLV；"
    "open_to_report_move 只是开盘→报告位移；缺字段一律写『未取得』，"
    "不补造盘口/水位/比分/赛程，不改评分或核验口径。"
)

# 收盘快照的 odds_format 可能与台账不同（快照多为 decimal，台账为 hk）。
# 只做机械格式归一，绝不改数值含义；未知组合一律不混算、写「未取得」。
def _align_odds_format(v, snap_fmt: str, ledger_fmt: str):
    if v is None:
        return None
    s, l = (snap_fmt or "").strip().lower(), (ledger_fmt or "hk").strip().lower()
    if s == l or not s:
        return v
    if s == "decimal" and l == "hk":
        return round(v - 1.0, 6)
    if s == "hk" and l == "decimal":
        return round(v + 1.0, 6)
    return None  # 其它组合不猜测


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _real(v) -> bool:
    return v is not None and str(v).strip() not in ("", MISSING, "None", "null")


def _num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _atomic_write(p: Path, header: list[str], rows: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in header})
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def _atomic_json(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def load_pick_facts(reports_root: Path | None = None) -> dict[str, dict]:
    """从 reports/**/*.json 的 picks 读真实盘口事实，按 match_id 去重（最新报告优先）。"""
    base = Path(reports_root) if reports_root else REPORTS
    out: dict[str, dict] = {}
    if not base.is_dir():
        return out
    for p in sorted(base.rglob("*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for pk in (r.get("picks") or []):
            mid = str(pk.get("match_id") or "").strip()
            if not mid:
                continue
            o = pk.get("observation") if isinstance(pk.get("observation"), dict) else {}
            rec = {
                "date": str(r.get("date") or ""),
                "line_open": pk.get("line_open") if _real(pk.get("line_open")) else o.get("line_open"),
                "price_ts": pk.get("price_ts") if _real(pk.get("price_ts")) else o.get("price_ts"),
                "bookmaker": pk.get("bookmaker") or o.get("bookmaker") or MISSING,
                "source": pk.get("source") or o.get("source") or MISSING,
                "line": pk.get("line"),
                "odds": pk.get("odds") or o.get("odds"),
                "kickoff_utc": pk.get("kickoff_utc") or "",
            }
            # 只保留真实值，不把 None 写成 0
            rec = {k: (v if _real(v) else MISSING) for k, v in rec.items()}
            existing = out.get(mid)
            if existing is None or (rec["date"], rec["kickoff_utc"]) >= (existing["date"], existing["kickoff_utc"]):
                out[mid] = rec
    return out


def load_close_facts(snapshots_dir: Path | None = None) -> dict[str, dict]:
    """从 data/snapshots/odds/*.csv 的 stage=close 行读收盘快照原始值。

    只抄录原始 ah_home_line / odds / captured_at / bookmaker，**不猜视角**；
    视角换算在 compute_clv 里用同场 report 阶段行自动学习。
    """
    d = Path(snapshots_dir) if snapshots_dir else DATA / "snapshots" / "odds"
    by_mid: dict[str, list[dict]] = defaultdict(list)
    if not d.is_dir():
        return {}
    for p in sorted(d.glob("*.csv")):
        try:
            with p.open(encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
        except (OSError, UnicodeError, csv.Error):
            continue
        for r in rows:
            mid = str(r.get("match_id") or "").strip()
            st = str(r.get("stage") or "").strip().lower()
            if not mid or st not in ("close", "report"):
                continue
            by_mid[mid].append({"stage": st, "ah_home_line": _num(r.get("ah_home_line")),
                                "odds_home": _num(r.get("odds_home")), "odds_away": _num(r.get("odds_away")),
                                "captured_at": str(r.get("captured_at") or ""),
                                "price_ts": str(r.get("price_ts") or ""),
                                "bookmaker": str(r.get("bookmaker") or ""),
                                "source": str(r.get("source") or ""),
                                "odds_format": str(r.get("odds_format") or "")})
    out: dict[str, dict] = {}
    for mid, rows in by_mid.items():
        close = [r for r in rows if r["stage"] == "close" and r["ah_home_line"] is not None]
        if not close:
            continue
        close.sort(key=lambda r: r["captured_at"])
        out[mid] = {"close_rows": close, "report_rows": [r for r in rows if r["stage"] == "report"]}
    return out


def learn_view(report_line: float, report_rows: list[dict]) -> int | None:
    """用同场 report 阶段快照自动学习 ah_home_line -> 所选方视角的符号。

    只接受「恰好一种符号能把某条 report 快照对上报告 line」的情况；歧义或对不上
    一律返回 None（宁可写未取得，也不猜）。
    """
    hits: set[int] = set()
    for r in report_rows:
        v = r.get("ah_home_line")
        if v is None:
            continue
        for sign in (1, -1):
            if abs(sign * v - report_line) < 1e-6:
                hits.add(sign)
    return next(iter(hits)) if len(hits) == 1 else None


def compute_clv(row: dict, pick: dict, snap: dict | None) -> dict:
    """收盘字段 + line_move/clv（**直接复用仓库既有函数**，不另立口径）。"""
    from .audit.experiment_track import clv as price_clv, line_move as repo_line_move

    line = _num(row.get("line"))
    base = {k: MISSING for k in ("close_line", "close_odds", "close_odds_opp",
                                 "close_fair_p", "close_captured_at", "line_move", "clv")}
    origin = MISSING
    close_odds_pick = MISSING
    close_odds_opp = MISSING
    # 1) 台账已有 final_line：与 line 同为所选方视角，可直接用
    if _real(row.get("final_line")) and _num(row.get("final_line")) is not None and line is not None:
        base["close_line"] = _num(row["final_line"])
        if _real(row.get("final_odds")):
            close_odds_pick = _num(row["final_odds"])
        origin = "predictions.csv final_line（同视角，未推断）"
    # 2) 快照 stage=close：必须学到唯一视角才换算
    elif snap and line is not None:
        sign = learn_view(line, snap.get("report_rows") or [])
        if sign is not None:
            c = snap["close_rows"][-1]
            side = str(row.get("pick_side") or "").strip().lower()
            base["close_line"] = round(sign * c["ah_home_line"], 4)
            base["close_captured_at"] = c["captured_at"] or MISSING
            pick_o = c["odds_away"] if side == "away" else c["odds_home"]
            opp_o = c["odds_home"] if side == "away" else c["odds_away"]
            close_odds_pick = pick_o if pick_o is not None else MISSING
            close_odds_opp = opp_o if opp_o is not None else MISSING
            origin = f"snapshot stage=close / bookmaker={c['bookmaker'] or MISSING} / 视角由同场 report 行学习"
    # 收盘快照水位格式 -> 台账格式（机械换算，不改数值含义；未知组合不混算）
    ledger_fmt = str(row.get("odds_format") or "hk").strip().lower()
    # 只有「收盘水位确实来自快照」时才做格式归一；台账 final_odds 本身就是台账格式，不转换。
    if snap and snap.get("close_rows") and "snapshot stage=close" in str(origin):
        snap_fmt = str(snap["close_rows"][-1].get("odds_format") or "").strip().lower()
        a_pick = _align_odds_format(
            close_odds_pick if isinstance(close_odds_pick, (int, float)) else None,
            snap_fmt, ledger_fmt)
        a_opp = _align_odds_format(
            close_odds_opp if isinstance(close_odds_opp, (int, float)) else None,
            snap_fmt, ledger_fmt)
        # 0.0 是合法港盘水位，不能用 `or MISSING` 误判成缺值
        close_odds_pick = MISSING if a_pick is None else a_pick
        close_odds_opp = MISSING if a_opp is None else a_opp
    base["close_odds"] = close_odds_pick
    base["close_odds_opp"] = close_odds_opp
    base["close_fair_p"] = MISSING  # 留空，由 close_fair_prob() 用两侧水位去水

    # 复用仓库既有 line_move()/clv()：把所需字段组装成一行传进去，不改其实现
    probe = {"line": row.get("line"), "close_line": base["close_line"],
             "close_odds": close_odds_pick, "close_odds_opp": close_odds_opp,
             "close_fair_p": MISSING, "odds_if_known": row.get("odds_if_known"),
             "odds_format": ledger_fmt, "market": row.get("market") or "ah"}
    lm = repo_line_move(probe)
    cv = price_clv(probe)
    base["line_move"] = round(lm, 4) if lm is not None else MISSING
    base["clv"] = round(cv, 4) if cv is not None else MISSING

    # 如实写明价格型 CLV 为什么是「未取得」，绝不拿线差冒充
    if not _real(origin) and snap:
        why = "有收盘快照但视角未能唯一学习，收盘线未取得（不猜测）"
    elif not _real(origin):
        why = "无收盘快照，收盘线未取得"
    elif cv is None and _real(base["close_line"]) and line is not None and base["close_line"] != line:
        why = (f"收盘盘口 {base['close_line']} ≠ 下注盘口 {line}，按仓库既有 clv() 口径"
               "仅在盘口相同时计算；线差只写 line_move，不当作 CLV")
    elif cv is None:
        why = "缺收盘两侧水位去水或下注赔率，按既有口径不计算"
    else:
        why = "收盘盘口=下注盘口且两侧水位齐全，按既有 clv() 正常计算"
    base["clv_basis"] = (
        f"{origin}；line_move/clv 复用 experiment_track.line_move()/clv()；{why}"
    ) if _real(origin) else f"clv=未取得（{why}）"
    return base


def backfill(ledger: Path | None = None, reports_root: Path | None = None,
             snapshots_dir: Path | None = None, apply: bool = False) -> dict:
    """只补空列、只写真实值；apply=False 时完全不写盘。"""
    p = Path(ledger) if ledger else DATA / "predictions.csv"
    if not p.exists():
        raise RuntimeError(f"台账不存在：{p}")
    with p.open(encoding="utf-8", newline="") as f:
        rd = csv.DictReader(f)
        header = list(rd.fieldnames or [])
        rows = [dict(r) for r in rd]
    for c in NEW_COLS:
        if c not in header:
            header.append(c)
    picks = load_pick_facts(reports_root)
    snaps = load_close_facts(snapshots_dir)
    counters = {k: 0 for k in ("line_open_filled", "price_ts_filled", "close_from_final",
                               "close_from_snapshot", "line_move_computed",
                               "open_to_report_move_computed", "clv_price_computed",
                               "no_close_available")}
    why_rows: list[str] = []
    for r in rows:
        mid = str(r.get("match_id") or "").strip()
        pick = picks.get(mid, {})
        # 只补空，不覆盖已有事实
        for col, key in (("line_open", "line_open"), ("price_ts", "price_ts")):
            if not _real(r.get(col)) and _real(pick.get(key)):
                r[col] = pick[key]
                counters[col + "_filled"] += 1
        # open_to_report_move：开盘→报告位移，明确不是 CLV、不是 line_move
        lo, ln = _num(r.get("line_open")), _num(r.get("line"))
        if lo is not None and ln is not None and not _real(r.get("open_to_report_move")):
            r["open_to_report_move"] = round(ln - lo, 4)
            counters["open_to_report_move_computed"] += 1
        # close / line_move / clv
        calc = compute_clv(r, pick, snaps.get(mid))
        # 缺字段一律写「未取得」，不留空白（AGENTS.md：缺字段不得留白/推断）
        for col in ("close_line", "close_odds", "close_odds_opp", "close_fair_p",
                    "close_captured_at", "line_move", "clv"):
            if not _real(r.get(col)):
                r[col] = calc[col] if _real(calc.get(col)) else MISSING
        # clv_basis 是「本次回填为什么这样算」的说明，每轮如实重算，不沿用旧文本
        r["clv_basis"] = calc["clv_basis"]
        why_rows.append(calc["clv_basis"])
        if _real(calc.get("line_move")):
            counters["line_move_computed"] += 1
            if "final_line" in str(calc.get("clv_basis")):
                counters["close_from_final"] += 1
            else:
                counters["close_from_snapshot"] += 1
        else:
            counters["no_close_available"] += 1
        if _real(calc.get("clv")):
            counters["clv_price_computed"] += 1
        if not _real(r.get("truth_source")):
            r["truth_source"] = MISSING
    # 收盘快照覆盖事实：行数 / 唯一场次 / 与台账交集（只数真实落盘行，不推断）
    close_rows_total = sum(len(v["close_rows"]) for v in snaps.values())
    joined_mids = sorted({str(r.get("match_id") or "").strip() for r in rows
                          if str(r.get("match_id") or "").strip() in snaps})
    price_reasons: dict[str, int] = {}
    for why, r in zip(why_rows, rows):
        if "盘口" in why and "≠" in why:
            k = "收盘盘口≠下注盘口（按口径不算 CLV）"
        elif "视角未能唯一学习" in why:
            k = "有收盘快照但视角未能唯一学习，收盘线未取得"
        elif "无收盘快照" in why:
            k = "无收盘快照"
        elif "缺收盘两侧水位" in why:
            k = "缺收盘两侧水位去水"
        elif _real(r.get("clv")):
            k = "已计算价格型 CLV"
        else:
            k = "无收盘快照"
        price_reasons[k] = price_reasons.get(k, 0) + 1
    join_facts = {
        "schema": 1, "checked_at": _now(),
        "evidence_grade": "【已验证】本地文件解析事实",
        "close_snapshot_rows_in_repo": close_rows_total,
        "close_snapshot_unique_matches": len(snaps),
        "ledger_rows": len(rows),
        "joined_unique_match_ids": len(joined_mids),
        "joined_match_ids": joined_mids,
        "price_clv_status_by_row": price_reasons,
        "note": ("只统计真实 stage=close 行与台账 match_id 的交集；价格型 CLV 严格复用 "
                 "experiment_track.clv()（仅收盘盘口=下注盘口且两侧水位齐全时计算），"
                 "线差单列为 line_move，绝不冒充 CLV；不补造任何盘口或水位。"),
        "unverified": ["收盘价是否即该机构最终收盘价", "收盘快照是否覆盖全部已结算场次"],
    }
    out = {
        "schema": 1, "checked_at": _now(),
        "evidence_grade": "【已验证】本地文件解析事实",
        "ledger": str(p), "rows": len(rows),
        "columns": header, "new_columns": NEW_COLS,
        "pick_facts_matches": sum(1 for r in rows if str(r.get("match_id") or "").strip() in picks),
        "snapshot_close_matches": sum(1 for r in rows if str(r.get("match_id") or "").strip() in snaps),
        "counters": counters,
        "applied": apply,
        "note": NOTE,
        "unverified": [
            "收盘线是否即该机构最终收盘价（不读日志，不推断）",
            "收盘快照是否覆盖全部已结算场次",
            "Telegram 接收端（本命令不发送任何消息）",
        ],
    }
    if apply:
        _atomic_write(p, header, rows)
        _atomic_json(p.parent / "clv_backfill.json", out)
        _atomic_json(p.parent / "clv_join_facts.json", join_facts)
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="CLV 台账回填（默认 dry-run；--apply 才写盘）")
    ap.add_argument("--apply", action="store_true", help="真正写回 predictions.csv")
    ap.add_argument("--ledger", default=None)
    ap.add_argument("--reports", default=None)
    ap.add_argument("--snapshots-dir", default=None)
    a = ap.parse_args(argv)
    out = backfill(Path(a.ledger) if a.ledger else None,
                   Path(a.reports) if a.reports else None,
                   Path(a.snapshots_dir) if a.snapshots_dir else None,
                   apply=a.apply)
    c = out["counters"]
    print(f"CLV_BACKFILL rows={out['rows']} applied={out['applied']} "
          f"line_open={c['line_open_filled']} price_ts={c['price_ts_filled']} "
          f"line_move={c['line_move_computed']} clv_price={c['clv_price_computed']} "
          f"no_close={c['no_close_available']} open_to_report={c['open_to_report_move_computed']}")
    if not a.apply:
        print("  DRY-RUN：未写盘；加 --apply 才回填")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
