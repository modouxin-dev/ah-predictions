"""模型实验盘（track=experiment）：A/B/C 等模型在 ah / 1x2 / ou 三种玩法上的独立记账、校验、重叠标注与统计。

- 文件：data/experiment_picks.csv（与 predictions.csv 同一 v3 表头 + 实验盘评估列 EXTRA_FIELDS）。
  实验盘**不写进** predictions.csv，正式战绩、Telegram、网站读取路径完全不受影响；
  即便误写入，stats.compute / records.sync_* 也会按 track 过滤掉。
- 统计：data/experiment_stats.json（按 模型 × 玩法）。无数据字段为 null（前端显示「暂无」）。
- 协议：docs/experiment_protocol.md。

CLI::

    python -m ah.audit.experiment_track validate   # 校验（每模型每玩法每天 ≤3、生成时间早于开球、必填项）
    python -m ah.audit.experiment_track overlap    # 重算 overlap 列
    python -m ah.audit.experiment_track settle     # 结算已填比分的行（只写结算列，不改推荐内容）
    python -m ah.audit.experiment_track stats      # 写 data/experiment_stats.json
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .. import DATA
from ..settle import (LEDGER_FIELDS, SIDES, norm_market, norm_odds_format, normalize_row, read_ledger,
                      settle_row)

log = logging.getLogger("ah.experiment")

EXPERIMENT_FILE = DATA / "experiment_picks.csv"
EXPERIMENT_STATS = DATA / "experiment_stats.json"
DAILY_CAP = 3            # 每模型 × 每玩法 × 每天最多 3 场
MIN_SAMPLE = 100         # 满 100 场（有 CLV 的已结算场次）才评定好坏
# 实验盘额外列（predictions.csv 不需要）：
#   generated_at   推荐生成时间（UTC ISO，必须早于 kickoff_utc）
#   model_p        模型概率：ah/ou = 期望结算得分 E[y]；1x2 = 所选结果概率
#   market_p       同口径的市场基准（market-baseline-v0 去水概率），用于 Brier 对比
#   close_line     收盘盘口（同视角：ah 所选一方、ou 总进球线；1x2 空）
#   close_odds     收盘时所选一方赔率（同 odds_format）
#   close_odds_opp 收盘时其他结果赔率，多个用 ';' 分隔（ah/ou 1 个，1x2 2 个）
#   close_fair_p   已去水的收盘公平概率（有则优先于 close_odds/close_odds_opp）
#   close_captured_at 收盘快照时间
# DATA SOURCE: 2026-11-08 —— close_* 五列已并入 settle.LEDGER_FIELDS（官方台账也要用），
# 这里只保留实验盘独有的 3 列，避免列名重复、避免两套收盘口径。
EXTRA_FIELDS = ["generated_at", "model_p", "market_p"]
EXPERIMENT_FIELDS = LEDGER_FIELDS + EXTRA_FIELDS
PICK_FIELDS = ("date", "match_id", "kickoff_utc", "track", "model_id", "model_version", "market", "pick_side",
               "pick", "line", "odds_if_known", "odds_format", "generated_at", "model_p", "market_p")
SCORE_Y = {"win": 1.0, "half_win": 0.75, "push": 0.5, "half_loss": 0.25, "loss": 0.0}
OUTCOMES = ("win", "half_win", "push", "half_loss", "loss", "void")


# ---------- 读写 ----------

def _f(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def normalize(row: dict) -> dict:
    normalize_row(row)
    for k in EXTRA_FIELDS:
        if row.get(k) is None:
            row[k] = ""
    return row


def read_experiment(path: Path = EXPERIMENT_FILE) -> list[dict]:
    return [normalize(r) for r in read_ledger(Path(path))]


def write_experiment(path: Path, rows: list[dict]) -> None:
    rows = [normalize(r) for r in rows]
    extra = [k for r in rows for k in r if k not in EXPERIMENT_FIELDS and k is not None]
    with Path(path).open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EXPERIMENT_FIELDS + list(dict.fromkeys(extra)))
        w.writeheader()
        w.writerows(rows)


def _key(r: dict) -> tuple:
    return r.get("date", ""), r.get("match_id", ""), r.get("model_id", ""), r.get("market", "")


def _dt(s: str):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


# ---------- 校验 ----------

def check_row(r: dict) -> str | None:
    """单行校验，返回拒绝原因或 None。"""
    if r.get("track") != "experiment":
        return f"track 必须是 experiment（收到 {r.get('track')!r}）"
    if not r.get("model_id"):
        return "缺 model_id"
    try:
        m = norm_market(r.get("market"))
    except ValueError as e:
        return str(e)
    if r.get("pick_side") not in SIDES[m]:
        return f"{m} 的 pick_side 必须是 {'/'.join(SIDES[m])}（收到 {r.get('pick_side')!r}）"
    if m in ("ah", "ou"):
        ln = _f(r.get("line"))
        if ln is None or (ln * 4) % 1:
            return f"非法盘口 {r.get('line')!r}"
        if m == "ou" and ln <= 0:
            return f"大小球线必须 >0（收到 {ln:g}）"
    if _f(r.get("odds_if_known")) is None:
        return "缺报告时刻赔率（实验盘不估算、不补值）"
    try:
        norm_odds_format(r.get("odds_format"))
    except ValueError as e:
        return str(e)
    if not r.get("generated_at"):
        return "缺 generated_at（无法证明生成早于开球）"
    try:
        if _dt(r["generated_at"]) >= _dt(r["kickoff_utc"]):
            return f"generated_at {r['generated_at']} 不早于开球 {r['kickoff_utc']}（防事后修正）"
    except (KeyError, ValueError):
        return "generated_at / kickoff_utc 无法解析"
    for k in ("model_p", "market_p"):
        v = _f(r.get(k))
        if r.get(k) not in (None, "") and (v is None or not 0 <= v <= 1):
            return f"{k} 必须在 [0,1]（收到 {r.get(k)!r}）"
    return None


def validate(rows: list[dict], cap: int = DAILY_CAP) -> tuple[list[dict], list[dict]]:
    """返回 (accepted, rejected)。rejected 每项 {key, reason, row}。
    同一 (date, match_id, model_id, market) 只认第一条；每 (date, model_id, market) 按 generated_at 先后取前 cap 条，
    超出的报警并拒绝计入。"""
    accepted, rejected, seen = [], [], set()
    counts: dict[tuple, int] = defaultdict(int)
    order = sorted(range(len(rows)), key=lambda i: (rows[i].get("generated_at") or "~", i))
    for i in order:
        r = normalize(rows[i])
        why = check_row(r)
        k = _key(r)
        if why is None and k in seen:
            why = "重复记录（同日同场同模型同玩法只认第一条）"
        if why is None:
            ck = (r["date"], r["model_id"], r["market"])
            if counts[ck] >= cap:
                why = f"超出每日上限：{r['model_id']} {r['market']} {r['date']} 已有 {cap} 场"
            else:
                counts[ck] += 1
        if why:
            log.warning("[experiment] 拒绝 %s：%s", "/".join(k), why)
            rejected.append({"key": "/".join(k), "reason": why, "row": r})
        else:
            seen.add(k)
            accepted.append((i, r))
    return [r for _, r in sorted(accepted, key=lambda x: x[0])], rejected


# ---------- 重叠 ----------

def _direction(a: dict, b_side: str) -> str:
    return "same" if a.get("pick_side") == b_side else "opposite"


def compute_overlaps(rows: list[dict], official_rows: list[dict] | None = None) -> list[dict]:
    """就地写 overlap 列：同场（match_id）同玩法的其他模型 `B:same;C:opposite`，与正式推荐重叠时加 `official:same|opposite`。
    1x2 中不同结果一律记 opposite。只看正式推荐 status != cancelled 的行。"""
    by = defaultdict(list)
    for r in rows:
        by[(r.get("match_id"), r.get("market"))].append(r)
    off = defaultdict(list)
    for o in official_rows or []:
        o = normalize_row(dict(o))
        if o.get("track") == "official" and o.get("status") != "cancelled":
            off[(o.get("match_id"), o.get("market"))].append(o)
    for r in rows:
        k = (r.get("match_id"), r.get("market"))
        tags = sorted({f"{x['model_id']}:{_direction(r, x['pick_side'])}" for x in by[k]
                       if x is not r and x.get("model_id") != r.get("model_id")})
        tags += sorted({f"official:{_direction(r, o['pick_side'])}" for o in off[k]})
        r["overlap"] = ";".join(tags)
    return rows


# ---------- 写入（只追加） ----------

def add_picks(new_rows: list[dict], path: Path = EXPERIMENT_FILE, official_path: Path = DATA / "predictions.csv",
              cap: int = DAILY_CAP) -> tuple[list[dict], list[dict]]:
    """追加实验盘推荐（只追加，不覆盖已有行的推荐内容）。超限/不合规的行被拒绝并返回。"""
    path = Path(path)
    old = read_experiment(path) if path.exists() else []
    fresh = []
    for r in new_rows:
        r = normalize({**{k: "" for k in EXPERIMENT_FIELDS}, "settled": "false", **r, "track": "experiment"})
        fresh.append(r)
    old_keys = {_key(r) for r in old}
    acc, rej = validate(old + fresh, cap)
    acc_ids = {id(r) for r in acc}
    keep = [r for r in fresh if id(r) in acc_ids and _key(r) not in old_keys]
    rej = [x for x in rej if any(x["row"] is r for r in fresh)]
    rows = old + keep
    compute_overlaps(rows, read_ledger(official_path) if Path(official_path).exists() else [])
    write_experiment(path, rows)
    return keep, rej


def settle_experiment(path: Path = EXPERIMENT_FILE) -> int:
    """结算已填比分的实验盘行；推荐内容列（PICK_FIELDS）若被改动则报错（防事后修正）。"""
    rows = read_experiment(path)
    n = 0
    for r in rows:
        before = {k: r.get(k) for k in PICK_FIELDS}
        s0 = r.get("settled")
        settle_row(r)
        if {k: r.get(k) for k in PICK_FIELDS} != before:  # pragma: no cover - 防御
            raise RuntimeError(f"结算改动了推荐内容：{_key(r)}")
        n += s0 != r.get("settled")
    if rows:
        write_experiment(path, rows)
    return n


# ---------- 指标 ----------

def to_decimal(odds, fmt: str) -> float:
    o = float(odds)
    return o + 1.0 if norm_odds_format(fmt) == "hk" else o


def close_fair_prob(r: dict) -> float | None:
    """收盘无水公平概率：close_fair_p 优先；否则用 close_odds 与 close_odds_opp（';' 分隔）乘法去水。"""
    p = _f(r.get("close_fair_p"))
    if p is not None:
        return p
    co = _f(r.get("close_odds"))
    opp = [_f(x) for x in str(r.get("close_odds_opp") or "").split(";") if x.strip()]
    if co is None or not opp or any(x is None for x in opp):
        return None
    fmt = r.get("odds_format") or "hk"
    inv = [1 / to_decimal(co, fmt)] + [1 / to_decimal(x, fmt) for x in opp]
    return inv[0] / sum(inv)


def clv(r: dict) -> float | None:
    """CLV = o_bet × p_fair − 1（与 paper_trades 同口径）；只在收盘盘口 = 下注盘口时计算（1x2 无盘口）。"""
    if r.get("market") != "1x2":
        cl, ln = _f(r.get("close_line")), _f(r.get("line"))
        if cl is None or ln is None or cl != ln:
            return None
    p = close_fair_prob(r)
    o = _f(r.get("odds_if_known"))
    if p is None or o is None:
        return None
    return to_decimal(o, r.get("odds_format") or "hk") * p - 1


def line_move(r: dict) -> float | None:
    """盘口方向的收盘差（正 = 拿到了比收盘更好的线）：ah 为 line − close_line；ou over 为 close − line，under 为 line − close。"""
    cl, ln = _f(r.get("close_line")), _f(r.get("line"))
    if r.get("market") == "1x2" or cl is None or ln is None:
        return None
    if r["market"] == "ah":
        return ln - cl
    return cl - ln if r.get("pick_side") == "over" else ln - cl


def outcome_y(r: dict) -> float | None:
    out = r.get("ah_outcome")
    if out not in SCORE_Y:
        return None
    if r.get("market") == "1x2":
        return 1.0 if out == "win" else 0.0
    return SCORE_Y[out]


def brier(p, y) -> float | None:
    return None if p is None or y is None else (p - y) ** 2


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _ci95(xs):
    if len(xs) < 2:
        return None
    m = _mean(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
    h = 1.96 * sd / math.sqrt(len(xs))
    return [round(m - h, 4), round(m + h, 4)]


def _r(x, n=4):
    return None if x is None else round(x, n)


def _bucket_stats(rows: list[dict]) -> dict:
    b = {k: 0 for k in OUTCOMES}
    settled = [r for r in rows if str(r.get("settled")).lower() == "true" and r.get("ah_outcome") in OUTCOMES]
    for r in settled:
        out = "void" if r.get("status") in ("cancelled", "void") else r["ah_outcome"]
        b[out] += 1
    decided = len(settled) - b["push"] - b["void"]
    live = [r for r in settled if r.get("status") not in ("cancelled", "void") and r["ah_outcome"] != "void"]
    units = sum(_f(r.get("units")) or 0.0 for r in live)
    seq = sorted(live, key=lambda r: (r.get("kickoff_utc", ""), r.get("match_id", "")))
    cum = peak = mdd = 0.0
    for r in seq:
        cum += _f(r.get("units")) or 0.0
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    clvs = [c for c in (clv(r) for r in live) if c is not None]
    moves = [m for m in (line_move(r) for r in live) if m is not None]
    pairs = [(_f(r.get("model_p")), _f(r.get("market_p")), outcome_y(r)) for r in live]
    mb = [brier(p, y) for p, _, y in pairs if p is not None and y is not None]
    kb = [brier(q, y) for p, q, y in pairs if p is not None and q is not None and y is not None]
    mb_paired = [brier(p, y) for p, q, y in pairs if p is not None and q is not None and y is not None]
    ci = _ci95(clvs)
    hits = b["win"] + 0.5 * b["half_win"]
    return {
        "picks": len(rows), "settled": len(settled), "pending": len(rows) - len(settled), **b,
        "decided": decided, "hit_rate": _r(hits / decided) if decided else None,
        "units": round(units, 4) if settled else None, "roi": _r(units / decided) if decided else None,
        "max_drawdown": round(mdd, 4) if live else None,
        "clv_n": len(clvs), "clv_mean": _r(_mean(clvs)), "clv_ci95": ci,
        "line_move_n": len(moves), "line_move_mean": _r(_mean(moves)),
        "brier_n": len(mb), "brier": _r(_mean(mb)),
        "market_brier_n": len(kb), "market_brier": _r(_mean(kb)),
        "brier_skill": _r(1 - _mean(mb_paired) / _mean(kb)) if kb and _mean(kb) else None,
        "overlap_models": sum(1 for r in rows if any(not t.startswith("official:") for t in r.get("overlap", "").split(";") if t)),
        "overlap_official": sum(1 for r in rows if "official:" in (r.get("overlap") or "")),
        "significant": bool(len(clvs) >= MIN_SAMPLE and ci is not None and (ci[0] > 0 or ci[1] < 0)),
    }


def compute_experiment(rows: list[dict], official_rows: list[dict] | None = None, cap: int = DAILY_CAP) -> dict:
    """按 模型 × 玩法 统计（拒绝的行不计入，单独列出）。"""
    acc, rej = validate([normalize(dict(r)) for r in rows], cap)
    compute_overlaps(acc, official_rows)
    groups: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in acc:
        groups[r["model_id"]][r["market"]].append(r)
    out = {}
    for mid in sorted(groups):
        out[mid] = {}
        for m in sorted(groups[mid]):
            versions = sorted({r.get("model_version", "") for r in groups[mid][m]} - {""})
            out[mid][m] = {**_bucket_stats(groups[mid][m]), "model_versions": versions}
    return {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "track": "experiment",
        "note": "实验盘单独记账，不计入正式命中率；null = 暂无数据",
        "daily_cap": cap, "min_sample": MIN_SAMPLE,
        "definitions": {
            "hit_rate": "(win + 0.5*half_win)/(settled - push - void)",
            "roi": "units/(settled - push - void)，1 单位平注、报告时刻赔率",
            "clv": "o_bet(十进制) × p_fair(收盘去水) − 1；仅收盘盘口=下注盘口时计算（1x2 不看盘口）",
            "line_move": "所选方向的收盘盘口差，正=拿到比收盘更好的线",
            "brier_ah_ou": "(model_p − y)²，y: 赢1/赢半0.75/走0.5/输半0.25/输0，model_p=期望得分",
            "brier_1x2": "所选结果的二元 Brier：(model_p − 1[所选结果发生])²",
            "brier_skill": "1 − brier/market_brier（同一批有 market_p 的场次；>0 优于市场基准 market-baseline-v0）",
            "max_drawdown": "按开球时间累计盈亏的最大回撤（单位）",
            "significant": f"clv_n ≥ {MIN_SAMPLE} 且 CLV 95% 置信区间不含 0；未达到一律 false",
            "overlap": "同场同玩法的其他模型 model:same|opposite；official:same|opposite = 与正式推荐重叠",
        },
        "by_model_market": out,
        "rejected": [{"key": x["key"], "reason": x["reason"]} for x in rej],
    }


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m ah.audit.experiment_track")
    ap.add_argument("cmd", choices=["validate", "overlap", "settle", "stats"])
    ap.add_argument("--csv", default=str(EXPERIMENT_FILE))
    ap.add_argument("--official", default=str(DATA / "predictions.csv"))
    ap.add_argument("--out", default=str(EXPERIMENT_STATS))
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    p = Path(a.csv)
    rows = read_experiment(p) if p.exists() else []
    official = read_ledger(Path(a.official)) if Path(a.official).exists() else []
    if a.cmd == "validate":
        acc, rej = validate(rows)
        for x in rej:
            print(f"[reject] {x['key']}: {x['reason']}")
        print(f"accepted {len(acc)} / rejected {len(rej)}")
    elif a.cmd == "overlap":
        compute_overlaps(rows, official)
        if rows:
            write_experiment(p, rows)
        print(f"overlap updated for {len(rows)} rows")
    elif a.cmd == "settle":
        print(f"settled {settle_experiment(p) if rows else 0} rows")
    else:
        s = compute_experiment(rows, official)
        Path(a.out).write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({m: {k: v["picks"] for k, v in d.items()} for m, d in s["by_model_market"].items()},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
