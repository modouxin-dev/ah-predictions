"""为审计员模拟单账本输出“基准”(market-baseline-v0) 赢走输概率与 model_p。

只读：/workspace/ah-audit/data/paper_trades.csv（不修改）；Pinnacle 亚盘价格来源（二选一）：
- 默认（v2 起）：数据官盘口快照 CSV（stage=report, bookmaker=pinnacle），读 `--snap-repo` 的工作区文件，
  或 `--snap-rev <提交>` 用 git show 只读读取；新路径 data/snapshots/odds/ 与旧路径 data/odds_snapshots/ 都接受。
- `--source apif-cache`：本仓库 data/cache/odds/（API-Football Pinnacle 快照，20261104T2101 版用的就是它）。
大小 2.5 只来自 data/cache/odds/ 的 Pinnacle（开赛前抓取的）；没有则总进球按 2.6 假设（列 total_source 标明）。
margin = 1/主赔 + 1/客赔 − 1；> 5% 标 margin_flag=high（仍输出 model_p）。
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
from ah.model import snapshots as sn  # noqa: E402
from ah.model.scoring import expected_score, validate_probs  # noqa: E402

BJ = timezone(timedelta(hours=8))
MODEL_VERSION = "market-baseline-v0"
LEDGER = Path("/workspace/ah-audit/data/paper_trades.csv")
MISSING = "数据缺失，不推"
MARGIN_HIGH = 0.05


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


def _ko(lr) -> datetime | None:
    k = lr.get("kickoff_utc")
    return datetime.fromisoformat(str(k).replace("Z", "+00:00")) if isinstance(k, str) and k else None


def lookup_apif(lr, snaps, ko):
    mid = str(lr.get("match_id") or "")
    fid = mid.split("-", 1)[1] if mid.startswith("apif-") else None
    snap = snaps.get(fid) if fid else None
    hl = lr.get("home_line")
    if snap is None or pd.isna(hl) or float(hl) not in snap["ah"]:
        why = ("无 API-Football fixture id" if not fid else "缓存中无 Pinnacle 亚盘快照" if snap is None
               else "账本无盘口" if pd.isna(hl) else f"Pinnacle 快照无 {hl} 盘")
        return None, why
    oh, oa = snap["ah"][float(hl)]
    return dict(oh=oh, oa=oa, captured_at=snap["captured_at"], price_ts=snap["updated_at"], verification="",
                odds_source=f"API-Football /odds Pinnacle (data/cache/odds/{snap['file']})", stage="report"), ""


def lookup_csv(lr, rows, ko, src_label):
    mid = str(lr.get("match_id") or "")
    hl = lr.get("home_line")
    if pd.isna(hl) or hl == "":
        return None, "账本无盘口"
    if not any(r.match_id == mid for r in rows):
        return None, "盘口快照中无此 match_id"
    r = sn.pick_price(rows, mid, float(hl), bookmaker="pinnacle", stage="report")
    if r is None:
        return None, f"快照中无 Pinnacle report 时点 {float(hl):+g} 盘"
    return dict(oh=r.odds_home, oa=r.odds_away, captured_at=r.captured_at, price_ts=r.price_ts,
                verification=r.verification, stage=r.stage,
                odds_source=f"盘口快照 {src_label}（source={r.source}, bookmaker=pinnacle, stage=report）"), ""


def row_for(lr: dict, price: dict | None, why: str, ou_snaps: dict, now: datetime) -> dict:
    mid = str(lr.get("match_id") or "")
    out = dict(date=lr.get("date"), match_id=mid, league=lr.get("league"), home=lr.get("home"), away=lr.get("away"),
               kickoff_utc=lr.get("kickoff_utc") if pd.notna(lr.get("kickoff_utc")) else "",
               ledger_status=lr.get("status"), model_version=MODEL_VERSION, baseline_label="基准",
               generated_at=now.astimezone(BJ).isoformat(timespec="seconds"))
    if price is None:
        out.update(decision=MISSING, note=why, valid_prekickoff=False)
        return out
    ko = _ko(lr)
    hl = float(lr["home_line"]) + 0.0
    oh, oa = price["oh"], price["oa"]
    fid = mid.split("-", 1)[1] if mid.startswith("apif-") else None
    ou = (ou_snaps.get(fid) or {}).get("ou25") if fid else None
    ou_ok = ou and ko and ou_snaps[fid]["captured_at"] < ko
    p_home_eff = float(am.devig([oh, oa])[0])
    p_over = float(am.devig(list(ou))[0]) if ou_ok else None
    m = am.implied_matrix(hl, p_home_eff, p_over)
    cand = {}
    for side, line, odds in (("home", hl, oh), ("away", -hl + 0.0, oa)):
        pr = am.ah_probs(m, line, side)
        validate_probs(pr, line)
        cand[side] = (expected_score(pr), am.ev(pr, odds), line, odds, pr)
    side = max(cand, key=lambda s: (round(cand[s][0], 12), cand[s][1]))
    mp, ev, line, odds, pr = cand[side]
    valid = bool(ko and now < ko and price["captured_at"] < ko)
    mg = sn.margin(oh, oa)
    out.update(decision="baseline-only（市场基准，edge≈0，不构成推荐）", pick_side=side, line=line, home_line=hl,
               p_win=pr["win"], p_half_win=pr["half_win"], p_push=pr["push"], p_half_loss=pr["half_loss"],
               p_loss=pr["loss"], model_p=mp, pick_odds_pinnacle=odds, ev_at_pinnacle=ev,
               pin_home_odds=oh, pin_away_odds=oa, margin=mg, margin_flag="high" if mg > MARGIN_HIGH else "",
               verification=price["verification"], p_over25_devig=p_over,
               total_source="Pinnacle 大小2.5（data/cache/odds）" if ou_ok else "假设总进球 2.6（无赛前大小球价）",
               devig_method="multiplicative", odds_source=price["odds_source"],
               captured_at=price["captured_at"].astimezone(BJ).isoformat(timespec="seconds"),
               bookmaker_updated_at=price["price_ts"].astimezone(BJ).isoformat(timespec="seconds")
               if price["price_ts"] else "",
               stage=price["stage"], valid_prekickoff=valid,
               note="" if valid else "开赛后生成或快照晚于开赛：不是有效赛前记录")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default=str(LEDGER))
    ap.add_argument("--cache", default=str(ROOT / "data" / "cache" / "odds"))
    ap.add_argument("--source", choices=["snapshots", "apif-cache"], default="snapshots")
    ap.add_argument("--snap-repo", default="/workspace/ah-data", help="数据官仓库根目录（只读）")
    ap.add_argument("--snap-rev", default=None, help="用 git show 从该提交只读读取快照（不填则读工作区文件）")
    ap.add_argument("--report-date", default=None, help="快照文件的报告日期，如 2026-11-05")
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    snaps = load_pinnacle_snapshots(Path(a.cache))
    led = pd.read_csv(a.ledger, dtype={"match_id": str})
    rows_csv, label = [], ""
    if a.source == "snapshots":
        if not a.report_date:
            raise SystemExit("--source snapshots 需要 --report-date")
        if a.snap_rev:
            text, label = sn.read_text_from_git(Path(a.snap_repo), a.snap_rev, a.report_date)
        else:
            p = sn.find_snapshot_file(Path(a.snap_repo), a.report_date)
            if p is None:
                raise SystemExit(f"{a.snap_repo} 下没有 {a.report_date}.csv（新旧路径都没有）")
            text, label = p.read_text(encoding="utf-8"), str(p)
        rows_csv = sn.parse_rows(text)
    rows = []
    for r in led.to_dict("records"):
        price, why = (lookup_csv(r, rows_csv, _ko(r), label) if a.source == "snapshots"
                      else lookup_apif(r, snaps, _ko(r)))
        rows.append(row_for(r, price, why, snaps, now))
    df = pd.DataFrame(rows)
    outdir = HERE / "outputs"
    outdir.mkdir(exist_ok=True)
    path = outdir / f"paper_trades_model_p_{now.astimezone(BJ).strftime('%Y%m%dT%H%M%z')}.csv"
    if path.exists():
        raise SystemExit(f"{path} 已存在，拒绝覆盖")
    df.to_csv(path, index=False, encoding="utf-8")
    print(path)
    cols = ["match_id", "home", "away", "pick_side", "line", "p_win", "p_half_win", "p_push", "p_half_loss",
            "p_loss", "model_p", "ev_at_pinnacle", "margin", "margin_flag", "verification", "valid_prekickoff", "decision", "note"]
    print(df[[c for c in cols if c in df]].to_string())


if __name__ == "__main__":
    main()
