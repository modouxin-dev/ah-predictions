"""亚盘结算（含四分之一盘）。

约定：line 为“所选一方”的让球数，负数表示让球（例如 -0.75 = 让半一），
正数表示受让。按所选一方净胜球 + line 计算。

四分之一盘(±0.25/±0.75...) 拆成相邻两个半盘/整盘各下注一半。
"""
from __future__ import annotations

import argparse
import csv
from decimal import Decimal
from pathlib import Path

OUTCOMES = ("win", "half_win", "push", "half_loss", "loss")


def _single(margin: Decimal) -> Decimal:
    """单一(整/半)盘：返回 1 赢, 0 走, -1 输。"""
    if margin > 0:
        return Decimal(1)
    if margin < 0:
        return Decimal(-1)
    return Decimal(0)


def settle(pick_goals: int, opp_goals: int, line: float) -> str:
    """返回 win/half_win/push/half_loss/loss。"""
    ln = Decimal(str(line))
    if (ln * 4) % 1 != 0:
        raise ValueError(f"非法盘口 {line}，必须是 0.25 的倍数")
    diff = Decimal(pick_goals - opp_goals)
    if (ln * 2) % 1 == 0:  # 整盘或半盘
        parts = [ln]
    else:  # 四分之一盘
        parts = [ln - Decimal("0.25"), ln + Decimal("0.25")]
    score = sum(_single(diff + p) for p in parts) / len(parts)
    return {
        Decimal(1): "win",
        Decimal("0.5"): "half_win",
        Decimal(0): "push",
        Decimal("-0.5"): "half_loss",
        Decimal(-1): "loss",
    }[score]


def profit(outcome: str, odds: float | None, stake: float = 1.0, odds_format: str = "hk") -> float:
    """单位盈亏。odds 为港盘水位(hk, 如 0.92) 或十进制(decimal, 如 1.92)。
    无赔率时按 0.9 水位估算（报告中会标注 odds_if_known 为空）。"""
    if odds in (None, ""):
        odds = 0.9
        odds_format = "hk"
    o = float(odds)
    net = o if odds_format == "hk" else o - 1.0
    return round({
        "win": stake * net,
        "half_win": stake * net / 2,
        "push": 0.0,
        "half_loss": -stake / 2,
        "loss": -stake,
    }[outcome], 4)


def parse_score(result: str) -> tuple[int, int]:
    """'2-1' -> (2, 1)（主-客）。"""
    h, a = result.replace(":", "-").split("-")
    return int(h), int(a)


def settle_row(row: dict) -> dict:
    """对 predictions.csv 的一行结算（需要 result 为 '主-客' 比分）。"""
    if not row.get("result") or str(row.get("settled", "")).lower() == "true":
        return row
    hg, ag = parse_score(row["result"])
    side = row["pick_side"]
    pg, og = (hg, ag) if side == "home" else (ag, hg)
    out = settle(pg, og, float(row["line"]))
    row["ah_outcome"] = out
    row["units"] = str(profit(out, float(row["odds_if_known"]) if row.get("odds_if_known") else None))
    row["settled"] = "true"
    return row


def settle_csv(path: Path) -> int:
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    if not rows:
        return 0
    fields = list(rows[0].keys())
    n = 0
    for r in rows:
        before = r.get("settled")
        settle_row(r)
        n += before != r.get("settled")
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return n


def main() -> None:
    from . import DATA
    ap = argparse.ArgumentParser(description="结算 predictions.csv 中已填写比分的记录")
    ap.add_argument("--csv", default=str(DATA / "predictions.csv"))
    a = ap.parse_args()
    # TODO: 自动从比分源(titan007 bfdata / 官方)回填 result；目前需手动或由 fetch_fixtures 回填
    print(f"settled {settle_csv(Path(a.csv))} rows")


if __name__ == "__main__":
    main()
