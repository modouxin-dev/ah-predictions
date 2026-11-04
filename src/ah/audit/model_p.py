"""读取 + 校验模型研究员的 model_p 文件，供 paper_trades 合并。

文件：<dir>/paper_trades_model_p_YYYYMMDDTHHMM+0800.csv（model/v0 research/README.md）。
同一场取"开球前最后一版"：按文件名时间戳排序，只用 generated_at < kickoff 的版本。

逐行校验（任一不过 → 不并入，原因写入 reject 列表）：
- valid_prekickoff 必须为 true；decision 为「数据缺失，不推」→ 不并入（stake/model_p 留空）
- 五类概率 p_win..p_loss 和为 1（容差 1e-6），且各项 ∈ [0,1]
- 整数盘 P(赢半)=P(输半)=0；半球盘 P(走)=P(赢半)=P(输半)=0；四分之一盘 P(走)=0
- model_p = P赢 + 0.75·P赢半 + 0.5·P走 + 0.25·P输半（容差 1e-6）
- generated_at、captured_at 均早于开球
- line 与 home_line 换算一致：home 方向 line == home_line，away 方向 line == -home_line
- （合并时）home_line 与台账 home_line 一致

Brier 口径（与模型研究员约定）：结算得分 y = 赢1/赢半0.75/走0.5/输半0.25/输0；
Brier = mean((model_p − y)²)，只算已结算、按模型首选方向。
"""
from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path

TOL = 1e-6
PROBS = ("p_win", "p_half_win", "p_push", "p_half_loss", "p_loss")
SCORE = {"win": 1.0, "half_win": 0.75, "push": 0.5, "half_loss": 0.25, "loss": 0.0}
NAME_RE = re.compile(r"paper_trades_model_p_(\d{8}T\d{4}[+-]\d{4})\.csv$")


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def file_ts(p: Path) -> datetime | None:
    m = NAME_RE.search(p.name)
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%z") if m else None


def validate_row(r: dict) -> list[str]:
    """返回不合格原因列表（空 = 合格）。"""
    errs = []
    if str(r.get("valid_prekickoff", "")).lower() != "true":
        errs.append("valid_prekickoff=false" + (f"（{r.get('note')}）" if r.get("note") else ""))
        return errs
    if "数据缺失" in (r.get("decision") or ""):
        return ["decision=数据缺失，不推"]
    try:
        p = {k: float(r[k]) for k in PROBS}
        mp, line, hl = float(r["model_p"]), float(r["line"]), float(r["home_line"])
    except (KeyError, TypeError, ValueError):
        return ["概率/盘口字段缺失或非数字"]
    if any(v < -TOL or v > 1 + TOL for v in p.values()):
        errs.append("概率超出[0,1]")
    s = sum(p.values())
    if abs(s - 1) > TOL:
        errs.append(f"五类概率和={s:.8f}≠1")
    q = (line * 4) % 2  # 0 整/半, 1 四分之一
    if line % 1 == 0:
        if abs(p["p_half_win"]) > TOL or abs(p["p_half_loss"]) > TOL:
            errs.append("整数盘 P(赢半)/P(输半)≠0")
    elif line % 0.5 == 0:
        if max(abs(p["p_push"]), abs(p["p_half_win"]), abs(p["p_half_loss"])) > TOL:
            errs.append("半球盘 P(走/赢半/输半)≠0")
    elif q == 1 and abs(p["p_push"]) > TOL:
        errs.append("四分之一盘 P(走)≠0")
    exp = p["p_win"] + 0.75 * p["p_half_win"] + 0.5 * p["p_push"] + 0.25 * p["p_half_loss"]
    if abs(exp - mp) > TOL:
        errs.append(f"model_p={mp}≠期望得分{exp:.6f}")
    side = r.get("pick_side")
    if side not in ("home", "away") or (line != (hl if side == "home" else -hl)):
        errs.append(f"line/home_line 换算不一致（{side} {line} vs home {hl}）")
    ko = r.get("kickoff_utc")
    if not ko:
        errs.append("无开球时间")
    else:
        for k in ("generated_at", "captured_at"):
            if not r.get(k):
                errs.append(f"缺 {k}")
            elif _dt(r[k]) >= _dt(ko):
                errs.append(f"{k} 不早于开球")
    return errs


def key_of(r: dict) -> tuple:
    return (r.get("date", ""), r.get("match_id", ""))


def load(model_dir: Path) -> tuple[dict, list[dict]]:
    """返回 ({(date, match_id): 合格行（开球前最后一版）}, 校验记录[{file, date, match_id, ok, reasons}])。"""
    files = sorted((p for p in Path(model_dir).glob("paper_trades_model_p_*.csv") if file_ts(p)), key=file_ts)
    best, checks = {}, []
    for f in files:
        for r in csv.DictReader(f.open(encoding="utf-8")):
            r["_file"] = f.name
            errs = validate_row(r)
            checks.append({"file": f.name, "date": r.get("date"), "match_id": r.get("match_id"),
                           "ok": not errs, "reasons": "；".join(errs)})
            if not errs:
                best[key_of(r)] = r  # 文件按时间升序，后者覆盖 → 开球前最后一版（已校验 generated_at<开球）
    return best, checks


def settle_score(outcome: str) -> float | None:
    return SCORE.get(outcome)
