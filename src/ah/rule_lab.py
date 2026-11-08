"""MODEL CHANGE: 线上规则档位的 walk-forward 回测与概率校准（研究层，不碰线上模型）。

为什么单独建这个模块
--------------------
`research/backtest.py` 跑的是 Elo / Dixon-Coles / 1X2 / Pinnacle 市场基线，
而每天真正发推荐的是 `analyze.py` / `policy.py` / `config/settings.yaml` 的**规则档位**。
两者不是同一套模型，所以旧回测结论不能迁移到线上。本模块只对线上规则做时序评估。

诚实边界（AGENTS.md 硬底线）
---------------------------
- **不改** `analyze.py` / `policy.py` / `daily.py` / `config/settings.yaml` 的任何一行；
  本模块只新增研究/产出文件，线上推荐逻辑完全不变。
- 线上模型是**规则档位**（confidence=high/medium/low），没有可拟合参数；
  这里的 walk-forward 是「按时间滚动的样本外评估」，**不是重新训练模型**，
  绝不写成「模型训练 / 参数优化 / 准确率提升」。
- 样本门槛沿用 `review.py`：`MIN_SAMPLE=30`、`MIN_BUCKET_SAMPLE=10`；
  不足一律 `INSUFFICIENT_SAMPLE`，命中率 / ROI / 分桶数字留空。
- 官方 `predictions.csv` 没有 `model_p`（校准概率只存在于实验盘），
  故 Brier / 可靠性曲线一律 `NOT_COMPUTED`；档位→实际胜率表只是**描述性统计**，
  明确标注「不是校准概率，不得展示为高胜率」。
- 预测质量（log loss / 校准）与下注规则（ROI / CLV / 回撤）分层报告，不混成一个数。
- 生成后自检：出现「准确率提升 / 模型更准 / ROI 改善 / 已校准 / 保证」等断言直接拒绝输出。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import DATA

MISSING = "未取得"
MIN_SAMPLE = 30            # 与 review.py 一致
MIN_BUCKET_SAMPLE = 10     # 与 review.py 一致
DECIDED = {"win", "half_win", "half_loss", "loss"}
BANNED = ("准确率提升", "模型更准", "ROI 改善", "已校准", "保证准点", "显著提升", "稳定盈利")

NOTE = (
    "线上规则档位的时序样本外评估：无拟合参数，不是重新训练；样本不足写 INSUFFICIENT_SAMPLE；"
    "无 model_p 时 Brier/可靠性曲线 NOT_COMPUTED；预测质量与下注规则分层；"
    "不修改任何线上评分/阈值/核验/展示逻辑，不宣称准确率或投资回报改善。"
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _real(v) -> bool:
    return v is not None and str(v).strip() not in ("", MISSING, "None", "null")


def _f(v):
    try:
        return None if v in (None, "", MISSING) else float(v)
    except (TypeError, ValueError):
        return None


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


def load_pools(samples_dir: Path | None = None) -> list[dict]:
    """读开球前候选池；只抄字段，不生成任何预测结论。"""
    d = Path(samples_dir) if samples_dir else DATA / "samples"
    out = []
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for r in (j.get("rows") or []):
            if isinstance(r, dict) and str(r.get("match_id") or "").strip():
                out.append({"pool_date": str(j.get("date") or p.stem), "source_pool": str(p.name), **r})
    return out


def load_outcomes(ledger: Path | None = None) -> dict[str, dict]:
    """从 predictions.csv 读已结算真值；result 为空或 settle=false 不参与评估。"""
    p = Path(ledger) if ledger else DATA / "predictions.csv"
    out = {}
    if not p.exists():
        return out
    try:
        with p.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    except (OSError, UnicodeError, csv.Error):
        return out
    for r in rows:
        mid = str(r.get("match_id") or "").strip()
        if not mid or str(r.get("settled", "")).lower() != "true":
            continue
        out[mid] = {"result": str(r.get("result") or ""), "ah_outcome": str(r.get("ah_outcome") or ""),
                    "units": _f(r.get("units")), "odds_estimated": str(r.get("odds_estimated") or ""),
                    "confidence": str(r.get("confidence") or ""), "line": _f(r.get("line")),
                    "kickoff_utc": str(r.get("kickoff_utc") or ""), "date": str(r.get("date") or ""),
                    "model_p": _f(r.get("model_p")), "market_p": _f(r.get("market_p"))}
    return out


def join(pool_rows: list[dict], outcomes: dict[str, dict]) -> list[dict]:
    """按 match_id 内连接；按开球时间排序，保证时序不泄漏。"""
    out = []
    for r in pool_rows:
        mid = str(r.get("match_id") or "").strip()
        o = outcomes.get(mid)
        if not o or o["ah_outcome"] not in DECIDED:
            continue
        out.append({"match_id": mid, "pool_date": r.get("pool_date"), "kickoff_utc": o["kickoff_utc"] or r.get("kickoff_utc") or "",
                    "date": o["date"] or r.get("date"), "sample_role": r.get("sample_role") or MISSING,
                    "confidence": o["confidence"] or r.get("confidence") or MISSING,
                    "score": _f(r.get("score")), "line": o["line"] if o["line"] is not None else _f(r.get("line")),
                    "ah_outcome": o["ah_outcome"], "units": o["units"],
                    "model_p": o["model_p"], "market_p": o["market_p"]})
    out.sort(key=lambda x: (x["kickoff_utc"], x["date"], x["match_id"]))
    return out


def _bucket(rows: list[dict]) -> dict:
    decided = [r for r in rows if r["ah_outcome"] in DECIDED]
    n = len(decided)
    hits = sum(1.0 if r["ah_outcome"] == "win" else 0.5 if r["ah_outcome"] == "half_win" else 0.0
               for r in decided)
    units = sum(float(r["units"]) if r["units"] is not None else 0.0 for r in decided)
    out = {"n": n, "hits": round(hits, 4), "units": round(units, 4),
           "status": "SUFFICIENT" if n >= MIN_BUCKET_SAMPLE else "INSUFFICIENT_SAMPLE"}
    if n >= MIN_BUCKET_SAMPLE:
        out["hit_rate"] = round(hits / n, 4)
        out["roi"] = round(units / n, 4)
        z = 1.96
        p = hits / n
        den = 1 + z * z / n
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
        out["wilson_95_width"] = round(2 * half, 4)
    else:
        out["hit_rate"] = MISSING
        out["roi"] = MISSING
        out["wilson_95_width"] = MISSING
    return out


def walk_forward(rows: list[dict]) -> dict:
    """按日期滚动：每个块只用「更早块」作为历史，评估当前块（样本外）。

    线上规则无可拟合参数，所以这里没有 fit()；明确写为「时序样本外评估」而非训练。
    """
    blocks: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        blocks[str(r.get("date") or r.get("pool_date") or MISSING)].append(r)
    keys = sorted(blocks)
    out, hist = [], []
    for k in keys:
        cur = blocks[k]
        b = _bucket(cur)
        out.append({"block": k, "rows": len(cur), "in_sample_rows_before": len(hist),
                    "out_of_sample": b,
                    "note": "本块为样本外；in_sample_rows_before 仅作历史规模说明，不参与拟合"})
        hist.extend(cur)
    overall = _bucket(rows)
    overall["status"] = "SUFFICIENT" if overall["n"] >= MIN_SAMPLE else "INSUFFICIENT_SAMPLE"
    if overall["n"] < MIN_SAMPLE:
        overall["hit_rate"] = MISSING
        overall["roi"] = MISSING
    return {"method": "chronological walk-forward (no fitted parameters; rule tiers are fixed)",
            "blocks": out, "overall": overall,
            "note": "不是模型训练；样本不足的块与总体均写 INSUFFICIENT_SAMPLE，命中率/ROI 留空"}


def tier_calibration(rows: list[dict]) -> dict:
    """信心档位 → 实际胜率的**描述性**统计；明确不是校准概率。"""
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[str(r.get("confidence") or MISSING)].append(r)
    out = {}
    for k, v in sorted(by.items()):
        b = _bucket(v)
        out[k] = {"observed": b, "note": "规则档位→实际胜率是描述性统计，不是校准概率；不得展示为『高胜率』"}
    return {"tiers": out, "status": "DESCRIPTIVE_ONLY",
            "note": "档位不是概率；样本不足的档位写 INSUFFICIENT_SAMPLE"}


def calibration(rows: list[dict]) -> dict:
    """Brier / 可靠性曲线：无 model_p 一律 NOT_COMPUTED，绝不用档位冒充概率。"""
    probs = [r for r in rows if r.get("model_p") is not None]
    base = {"schema": 1, "status": "NOT_COMPUTED",
            "reason": "官方 predictions.csv 无 model_p（校准概率只存在于实验盘）；"
                      "档位 high/medium/low 不是概率，不得代入 Brier 公式",
            "models_with_probability": len(probs),
            "brier": MISSING, "brier_skill_vs_market": MISSING,
            "reliability_bins": [], "log_loss": MISSING,
            "note": "预测质量层与下注规则层分开；无校准概率时不产出可靠性曲线，不补造概率"}
    # 无任何校准概率 -> NOT_COMPUTED（AGENTS.md 口径）；有少量但不足 -> INSUFFICIENT_SAMPLE
    if len(probs) == 0:
        return base
    if len(probs) < MIN_SAMPLE:
        base["status"] = "INSUFFICIENT_SAMPLE"
        base["reason"] += f"；有概率样本仅 {len(probs)} 场 < {MIN_SAMPLE} 场"
        return base
    # 只在真的有足够校准概率时才计算（未来实验盘并入后才会走到这里）
    y = [1.0 if r["ah_outcome"] == "win" else 0.75 if r["ah_outcome"] == "half_win"
         else 0.25 if r["ah_outcome"] == "half_loss" else 0.0 for r in probs]
    brier = sum((float(r["model_p"]) - yi) ** 2 for r, yi in zip(probs, y)) / len(probs)
    bins = []
    for lo in (0.0, 0.2, 0.4, 0.6, 0.8):
        sel = [(float(r["model_p"]), yi) for r, yi in zip(probs, y) if lo <= float(r["model_p"]) < lo + 0.2]
        if sel:
            bins.append({"bin": f"[{lo:.1f},{lo+0.2:.1f})", "n": len(sel),
                         "mean_predicted": round(sum(p for p, _ in sel) / len(sel), 4),
                         "mean_observed": round(sum(yi for _, yi in sel) / len(sel), 4)})
    base.update({"status": "COMPUTED", "brier": round(brier, 4),
                 "reliability_bins": bins,
                 "note": "仅在存在真实 model_p 且样本充足时计算；仍不构成任何准确率或投资回报改善结论"})
    return base


def betting_rule_layer(rows: list[dict]) -> dict:
    """下注规则层：ROI / 回撤 / CLV 可得性，与预测质量层分离。"""
    decided = [r for r in rows if r["ah_outcome"] in DECIDED]
    cum, peak, maxdd = 0.0, 0.0, 0.0
    for r in decided:
        cum += float(r["units"]) if r["units"] is not None else 0.0
        peak = max(peak, cum)
        maxdd = min(maxdd, cum - peak)
    return {"n": len(decided),
            "roi": _bucket(decided).get("roi", MISSING),
            "max_drawdown_units": round(maxdd, 4) if decided else MISSING,
            "clv_available": False,
            "clv_note": "价格型 CLV 需收盘两侧水位去水；官方台账当前未采集（见 clv.py），不补造",
            "note": "与预测质量层分开；ROI/回撤是历史模拟，不是未来保证"}


def build(pool_rows: list[dict] | None = None, outcomes: dict | None = None,
          samples_dir: Path | None = None, ledger: Path | None = None) -> dict:
    pools = pool_rows if pool_rows is not None else load_pools(samples_dir)
    outs = outcomes if outcomes is not None else load_outcomes(ledger)
    rows = join(pools, outs)
    out = {
        "schema": 1, "checked_at": _now(),
        "evidence_grade": "【已验证】本地文件解析事实",
        "model_under_test": "线上规则档位（analyze.py / policy.py / config/settings.yaml），未改动任何一行",
        "live_model_modified": False,
        "pool_rows": len(pools), "outcome_rows": len(outs), "joined_decided_rows": len(rows),
        "thresholds": {"min_sample": MIN_SAMPLE, "min_bucket_sample": MIN_BUCKET_SAMPLE},
        "walk_forward": walk_forward(rows),
        "prediction_quality": calibration(rows),
        "betting_rule": betting_rule_layer(rows),
        "tier_observation": tier_calibration(rows),
        "note": NOTE,
        "unverified": [
            "线上规则的真实独立回测结论（当前样本不足，见 walk_forward.overall.status）",
            "概率校准与可靠性曲线（无 model_p，NOT_COMPUTED）",
            "价格型 CLV（需收盘两侧水位去水，尚未采集）",
            "Telegram 接收端（本命令不发送任何消息）",
        ],
    }
    text = json.dumps(out, ensure_ascii=False)
    for bad in BANNED:
        if bad in text:
            raise RuntimeError(f"生成内容含禁止断言 {bad!r}，拒绝输出（防幻觉闸门）")
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="线上规则 walk-forward 与校准（研究层，不改线上模型）")
    ap.add_argument("--out", default=None)
    ap.add_argument("--samples-dir", default=None)
    ap.add_argument("--ledger", default=None)
    a = ap.parse_args(argv)
    out = build(samples_dir=Path(a.samples_dir) if a.samples_dir else None,
                ledger=Path(a.ledger) if a.ledger else None)
    if a.out:
        _atomic_json(Path(a.out), out)
    wf = out["walk_forward"]["overall"]
    print(f"RULE_LAB joined={out['joined_decided_rows']} overall={wf['status']} "
          f"calibration={out['prediction_quality']['status']} "
          f"live_model_modified={out['live_model_modified']}")
    if wf["status"] == "INSUFFICIENT_SAMPLE":
        print(f"::warning::RULE_LAB_INSUFFICIENT_SAMPLE 已结算 {wf['n']} 场 < {MIN_SAMPLE} 场；不产出命中率/ROI 结论")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
