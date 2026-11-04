"""predictions.csv v1(15 列) -> v2 迁移（幂等，可重复运行）。

    python -m ah.migrate_csv            # 就地迁移 data/predictions.csv
    python -m ah.migrate_csv --check    # 只打印将要做的改动，不写文件（有改动时退出码 1）

v3 新增列 track/model_id/model_version/market/overlap（旧行 official/ah/空），见 docs/experiment_protocol.md。
v2 新增列（见 settle.LEDGER_FIELDS / docs/predictions_csv.md）：
  odds_format     hk | decimal（旧行回填 hk）
  odds_estimated  true | false（无 odds_if_known、按 0.9 水位估算 -> true）
  status          active | void | cancelled（旧 confidence=cancelled -> cancelled）
  final_line      撤销/调整时的盘口（所选一方视角，负=让球），可空
  final_odds      撤销/调整时所选一方的水位（同 odds_format），可空

迁移步骤（每一步对已迁移的行都是 no-op）：
  1. 补齐新列默认值（settle.normalize_row）。
  2. status=cancelled 且 confidence=cancelled 的旧行：按报告 JSON 中该 pick 的 confidence 恢复原信心。
  3. status=cancelled/调整 且 final_line 为空：按报告 JSON 的 prematch/final_check 回填 final_line/final_odds。
  4. 报告 JSON 有 odds_format 时用它校正 odds_format（只在该行仍为默认 hk 时）。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

from . import DATA, REPORTS
from .settle import check_odds, norm_odds_format, read_ledger, write_ledger

log = logging.getLogger("ah.migrate_csv")

_LINE_RE = re.compile(r"^\s*([^\s+\-\d（(]+)?\s*([+-]?\d+(?:\.\d+)?)")


def _names(team: str) -> list[str]:
    """'鹿岛鹿角 Kashima Antlers' -> ['鹿岛鹿角', 'kashima antlers', 'kashima', ...]。"""
    t = (team or "").strip()
    out = [t.lower()]
    cjk = re.match(r"^([\u4e00-\u9fff·]+)", t)
    if cjk:
        out.append(cjk.group(1))
    latin = re.sub(r"[\u4e00-\u9fff·]+", " ", t).strip().lower()
    if latin:
        out += [latin, latin.split()[0]]
    return [x for x in out if x]


def _team_matches(token: str, team: str) -> bool:
    tok = token.strip().lower()
    if not tok:
        return False
    return any(tok in n or (len(n) >= 2 and n in tok) for n in _names(team))


def parse_current_line(cur, p: dict) -> float | None:
    """把 final_check/prematch 的 current_line 转成所选一方视角的数值盘口。
    数值 -> 视为已是所选一方视角；字符串如 '鹿岛 -0.25（武里南受让平半）' -> 按前缀队名判断方向。
    无法确定方向时返回 None（不猜）。"""
    if cur is None or cur == "":
        return None
    if isinstance(cur, (int, float)):
        return float(cur)
    m = _LINE_RE.match(str(cur))
    if not m:
        return None
    team, val = (m.group(1) or ""), float(m.group(2))
    side = p.get("pick_side")
    opp = {"home": "away", "away": "home"}.get(side)
    if not team:
        return None
    if _team_matches(team, p.get("pick_team") or p.get(side or "", "")):
        return val
    if opp and _team_matches(team, p.get(opp, "")):
        return -val if val else 0.0
    return None


def final_from_pick(p: dict) -> tuple[float | None, float | None]:
    """撤销/调整时的 (final_line, final_odds)，所选一方视角。"""
    pm = p.get("prematch") or {}
    fc = p.get("final_check") or {}
    if pm.get("decision") == "调整" and (pm.get("new_line") is not None or pm.get("new_odds") is not None):
        return (None if pm.get("new_line") is None else float(pm["new_line"]),
                None if pm.get("new_odds") is None else float(pm["new_odds"]))
    cur = pm.get("current_line", fc.get("current_line"))
    line = parse_current_line(cur, p)
    odds = None
    co = pm.get("current_odds") or fc.get("current_odds")
    if isinstance(co, dict) and p.get("pick_side") in co:
        odds = co[p["pick_side"]]
    elif isinstance(co, (int, float)):
        odds = co
    if pm.get("new_odds") is not None:
        odds = pm["new_odds"]
    return line, (None if odds is None else float(odds))


def _fmt(x) -> str:
    return "" if x is None else f"{float(x):g}"


def _load(date: str, reports: Path, cache: dict) -> dict | None:
    if date not in cache:
        y, m, _ = date.split("-")
        f = reports / y / m / f"{date}.json"
        try:
            cache[date] = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache[date] = None
    return cache[date]


def migrate_rows(rows: list[dict], reports: Path = REPORTS, legacy_format: bool = False) -> list[str]:
    """就地迁移已 normalize 的行，返回改动说明。幂等。
    legacy_format=True（原文件没有 odds_format 列）时，用报告 JSON 顶层 odds_format 回填（无则 hk）。"""
    from .records import is_cancelled, prematch_of
    changes, cache = [], {}
    for r in rows:
        if r.get("track") != "official":  # 实验盘行不按正式报告迁移
            continue
        tag = f"{r.get('date')} {r.get('match_id')}"
        rep = _load(r.get("date", ""), reports, cache) if r.get("date") else None
        p = next((x for x in (rep or {}).get("picks", []) if x.get("match_id") == r.get("match_id")), None)
        if legacy_format and rep and rep.get("odds_format"):
            try:
                fmt = norm_odds_format(rep["odds_format"])
            except ValueError:
                fmt = None
            if fmt and fmt != r["odds_format"]:
                changes.append(f"{tag}: odds_format {r['odds_format']} -> {fmt}（报告 odds_format）")
                r["odds_format"] = fmt
        if p is None:
            continue
        if is_cancelled(p) and r["status"] != "cancelled":
            changes.append(f"{tag}: status {r['status']} -> cancelled（报告已撤销）")
            r["status"] = "cancelled"
        if r["status"] == "cancelled" and str(r.get("confidence", "")).lower() == "cancelled":
            orig = p.get("confidence")
            if orig and orig != "cancelled":
                changes.append(f"{tag}: confidence cancelled -> {orig}（报告 picks[].confidence）")
                r["confidence"] = orig
        pm = prematch_of(p) or {}
        if (r["status"] == "cancelled" or pm.get("decision") == "调整") and not r.get("final_line") and not r.get("final_odds"):
            fl, fo = final_from_pick(p)
            if fl is not None or fo is not None:
                r["final_line"], r["final_odds"] = _fmt(fl), _fmt(fo)
                changes.append(f"{tag}: final_line={r['final_line']} final_odds={r['final_odds']}（报告 prematch/final_check）")
    n_exp = sum(1 for r in rows if r.get("track") != "official")
    if n_exp:
        log.warning("predictions.csv 中有 %d 行 track!=official；实验盘应写 data/experiment_picks.csv"
                    "（正式统计会忽略这些行）", n_exp)
        changes.append(f"警告：{n_exp} 行 track!=official（未改动，正式统计已忽略）")
    for r in rows:
        if r.get("odds_if_known"):
            check_odds(r["odds_if_known"], r["odds_format"], context=f"{r.get('date')} {r.get('match_id')}")
    return changes


def migrate_csv(path: Path = DATA / "predictions.csv", reports: Path = REPORTS, dry_run: bool = False) -> list[str]:
    """迁移 predictions.csv；dry_run=True 时不写文件。返回改动说明（空 = 已是最新）。"""
    import csv
    import tempfile
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return []
    raw = path.read_bytes()
    header = next(csv.reader([raw.decode("utf-8").splitlines()[0]]))
    rows = read_ledger(path)
    changes = migrate_rows(rows, reports, legacy_format="odds_format" not in header)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / "p.csv"
        write_ledger(tmp, rows)
        new = tmp.read_bytes()
    if new != raw and not changes:
        changes.append("补齐新列默认值（v2 odds_format…final_odds；v3 track=official, market=ah 等）")
    elif new != raw and ("status" not in header or "track" not in header):
        changes.insert(0, "表头升级（v2: odds_format/odds_estimated/status/final_line/final_odds；"
                          "v3: track/model_id/model_version/market/overlap）")
    if not dry_run and new != raw:
        path.write_bytes(new)
    return changes


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m ah.migrate_csv", description=__doc__.splitlines()[0])
    ap.add_argument("--csv", default=str(DATA / "predictions.csv"))
    ap.add_argument("--reports", default=str(REPORTS))
    ap.add_argument("--check", action="store_true", help="只检查，不写文件；有待迁移改动时退出码 1")
    a = ap.parse_args(argv)
    ch = migrate_csv(Path(a.csv), Path(a.reports), dry_run=a.check)
    for c in ch:
        print(f"[migrate] {c}")
    print(f"[migrate] {'待迁移' if a.check else '已迁移'} {len(ch)} 项" if ch else "[migrate] 已是最新格式，无改动")
    if a.check and ch:
        sys.exit(1)


if __name__ == "__main__":
    main()
