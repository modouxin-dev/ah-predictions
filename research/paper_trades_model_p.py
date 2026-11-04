"""为审计员模拟单账本输出“基准”(market-baseline-v0) 赢走输概率与 model_p。

只读：/workspace/ah-audit/data/paper_trades.csv（不修改）与本仓库 data/cache/odds/（API-Football Pinnacle 快照）。
输出：research/outputs/paper_trades_model_p_<YYYYMMDDTHHMM+0800>.csv（每次新文件，绝不覆盖）。
规则见 research/README.md。
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
from ah.model import ah_math as am  # noqa: E402
from ah.model.scoring import expected_score, validate_probs  # noqa: E402

BJ = timezone(timedelta(hours=8))
MODEL_VERSION = "market-baseline-v0"
LEDGER = Path("/workspace/ah-audit/data/paper_trades.csv")
MISSING = "数据缺失，不推"


def load_pinnacle_snapshots(cache_dir: Path) -> dict:
    """fixture id -> {captured_at, updated_at, ah{home_line:(oh,oa)}, ou25:(o,u)}"""
    out = {}
    for f in sorted(glob.glob(str(cache_dir / "*.json"))):
        J = json.load(open(f, encoding="utf-8"))
        for r in (J.get("data") or {}).get("response", []):
            fid = str(r["fixture"]["id"])
            for b in r.get("bookmakers", []):
                if b["name"] != "Pinnacle":
                    continue
                ah, ou = {}, None
                for bet in b["bets"]:
                    if bet["name"] == "Asian Handicap":
                        tmp = {}
                        for v in bet["values"]:
                            side, ln = v["value"].split()
                            tmp.setdefault(float(ln), {})[side] = float(v["odd"])
                        ah = {k: (v["Home"], v["Away"]) for k, v in tmp.items() if "Home" in v and "Away" in v}
                    elif bet["name"] == "Goals Over/Under":
                        vals = {v["value"]: float(v["odd"]) for v in bet["values"]}
                        if "Over 2.5" in vals and "Under 2.5" in vals:
                            ou = (vals["Over 2.5"], vals["Under 2.5"])
                captured = datetime.fromtimestamp(J["ts"], timezone.utc)
                prev = out.get(fid)
                if ah and (prev is None or captured > prev["captured_at"]):
                    out[fid] = dict(captured_at=captured, updated_at=datetime.fromisoformat(r["update"]),
                                    ah=ah, ou25=ou, file=Path(f).name)
    return out


def row_for(lr: dict, snaps: dict, now: datetime) -> dict:
    mid = str(lr.get("match_id") or "")
    out = dict(date=lr.get("date"), match_id=mid, league=lr.get("league"), home=lr.get("home"), away=lr.get("away"),
               kickoff_utc=lr.get("kickoff_utc") if pd.notna(lr.get("kickoff_utc")) else "",
               ledger_status=lr.get("status"), model_version=MODEL_VERSION, baseline_label="基准",
               generated_at=now.astimezone(BJ).isoformat(timespec="seconds"))
    fid = mid.split("-", 1)[1] if mid.startswith("apif-") else None
    snap = snaps.get(fid) if fid else None
    hl = lr.get("home_line")
    if snap is None or pd.isna(hl) or float(hl) not in snap["ah"]:
        why = ("无 API-Football fixture id" if not fid else "缓存中无 Pinnacle 亚盘快照" if snap is None
               else "账本无盘口" if pd.isna(hl) else f"Pinnacle 快照无 {hl} 盘")
        out.update(decision=MISSING, note=why, valid_prekickoff=False)
        return out
    hl = float(hl)
    oh, oa = snap["ah"][hl]
    p_home_eff = float(am.devig([oh, oa])[0])
    p_over = float(am.devig(list(snap["ou25"]))[0]) if snap["ou25"] else None
    m = am.implied_matrix(hl, p_home_eff, p_over)
    cand = {}
    for side, line, odds in (("home", hl, oh), ("away", -hl, oa)):
        pr = am.ah_probs(m, line, side)
        validate_probs(pr, line)
        cand[side] = (expected_score(pr), am.ev(pr, odds), line, odds, pr)
    side = max(cand, key=lambda s: (round(cand[s][0], 12), cand[s][1]))
    mp, ev, line, odds, pr = cand[side]
    ko = datetime.fromisoformat(out["kickoff_utc"].replace("Z", "+00:00")) if out["kickoff_utc"] else None
    valid = bool(ko and now < ko and snap["captured_at"] < ko)
    out.update(decision="baseline-only（市场基准，edge≈0，不构成推荐）", pick_side=side, line=line, home_line=hl,
               p_win=pr["win"], p_half_win=pr["half_win"], p_push=pr["push"], p_half_loss=pr["half_loss"],
               p_loss=pr["loss"], model_p=mp, pick_odds_pinnacle=odds, ev_at_pinnacle=ev,
               pin_home_odds=oh, pin_away_odds=oa, pin_margin=1 / oh + 1 / oa - 1,
               p_over25_devig=p_over, devig_method="multiplicative",
               odds_source=f"API-Football /odds Pinnacle (data/cache/odds/{snap['file']})",
               captured_at=snap["captured_at"].astimezone(BJ).isoformat(timespec="seconds"),
               bookmaker_updated_at=snap["updated_at"].astimezone(BJ).isoformat(timespec="seconds"),
               stage="report（日报时点快照）", valid_prekickoff=valid,
               note="" if valid else "开赛后生成或快照晚于开赛：不是有效赛前记录")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default=str(LEDGER))
    ap.add_argument("--cache", default=str(ROOT / "data" / "cache" / "odds"))
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    snaps = load_pinnacle_snapshots(Path(a.cache))
    led = pd.read_csv(a.ledger, dtype={"match_id": str})
    rows = [row_for(r, snaps, now) for r in led.to_dict("records")]
    df = pd.DataFrame(rows)
    outdir = HERE / "outputs"
    outdir.mkdir(exist_ok=True)
    path = outdir / f"paper_trades_model_p_{now.astimezone(BJ).strftime('%Y%m%dT%H%M%z')}.csv"
    if path.exists():
        raise SystemExit(f"{path} 已存在，拒绝覆盖")
    df.to_csv(path, index=False, encoding="utf-8")
    print(path)
    cols = ["match_id", "home", "away", "pick_side", "line", "p_win", "p_half_win", "p_push", "p_half_loss",
            "p_loss", "model_p", "ev_at_pinnacle", "pin_margin", "valid_prekickoff", "decision"]
    print(df[[c for c in cols if c in df]].to_string())


if __name__ == "__main__":
    main()
