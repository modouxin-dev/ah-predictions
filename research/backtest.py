"""滚动（walk-forward）回测：Elo 基线 vs Dixon-Coles(penaltyblog) vs DC+Elo vs 融合 vs Pinnacle 市场。

用法：  ../.venv/bin/python backtest.py [--leagues E0,E1,JPN] [--boot 2000]
- 按自然周分块：每块开始前只用此前比赛拟合 DC、刷新 Elo 进球映射，块内预测，块结束后再用赛果更新 Elo。
- 1X2：多分类 Brier（三项平方和）与 log loss。
- 亚盘（只限有 AHCh/PCAH 的欧洲联赛）：以收盘盘口 AHCh 为准，目标 = 主队“赢/赢半”(1) vs “输/输半”(0)，全走盘剔除；
  预测概率 = (P赢+P赢半)/(1−P走)。市场概率来自 Pinnacle 收盘（亚盘+大小 2.5）反解的隐含比分矩阵。
- 模拟下注 + CLV：用模型矩阵在 football-data 赛前价（AHh, PAHH/PAHA）上算主/客 EV，取较大且 >0 的一方；
  CLV = 下注赔率 / 收盘无水公平赔率 − 1（盘口变化时用收盘隐含矩阵换算到下注盘口）；ROI 按真实结算。
- 置信区间：按场次配对 bootstrap，95% 百分位区间。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

import fd_data as fd  # noqa: E402
from ah.model import ah_math as am  # noqa: E402
from ah.model import blend as bl  # noqa: E402
from ah.model import dixon_coles as dcm  # noqa: E402
from ah.model.baseline import EloGoals  # noqa: E402

OUT = HERE / "outputs"
CFG = dict(xi=0.0018, window_days=1095, min_team_matches=10, w_elo=0.3, w_blend=0.25, max_goals=12,
           elo_k=20.0, elo_hfa=60.0)
TEST_START = {"E0": "2021-07-01", "E1": "2021-07-01", "JPN": "2019-01-01"}
MODELS = ["elo", "dc", "dcelo", "blend", "mkt_open", "mkt_close"]


def ll_1x2(p, y):
    return -np.log(max(p[y], 1e-12))


def brier_1x2(p, y):
    t = np.zeros(3)
    t[y] = 1
    return float(((np.asarray(p) - t) ** 2).sum())


def ah_ahead_prob(m, line):
    p = am.ah_probs(m, line, "home")
    np_ = 1 - p["push"]
    return (p["win"] + p["half_win"]) / np_ if np_ > 1e-12 else np.nan


def market_matrix(line, oh, oa, oo, ou):
    if not all(np.isfinite([line, oh, oa])):
        return None
    ph = am.devig([oh, oa])[0]
    po = am.devig([oo, ou])[0] if np.isfinite(oo) and np.isfinite(ou) else None
    return am.implied_matrix(line, float(ph), None if po is None else float(po))


def g(r, c):
    v = r.get(c, np.nan)
    try:
        return float(v)
    except (TypeError, ValueError):
        return np.nan


def run_league(code: str, log) -> tuple[list[dict], list[dict]]:
    df = fd.load_new(code) if code in fd.NEW else fd.load_main(code)
    df = df.dropna(subset=["date"]).sort_values(["date"]).reset_index(drop=True)
    has_ah = "AHCh" in df.columns
    df["block"] = df["date"].dt.to_period("W-SUN")
    elo = EloGoals(k=CFG["elo_k"], hfa=CFG["elo_hfa"])
    start = pd.Timestamp(TEST_START[code])
    rows, fits = [], []
    t0 = time.time()
    for blk, b in df.groupby("block", sort=True):
        bstart = b["date"].min()
        if bstart >= start:
            elo.refit_goal_map()
            try:
                model = dcm.fit(df, bstart, xi=CFG["xi"], window_days=CFG["window_days"])
                fits.append(dict(league=code, date=str(bstart.date()), **dcm.params(model)))
            except Exception as e:  # noqa: BLE001
                log(f"{code} {bstart.date()} DC 拟合失败: {e}")
                model = None
            for _, r in b.iterrows():
                rows.append(predict_row(code, r, elo, model, has_ah))
        for _, r in b.iterrows():
            elo.update(r["home"], r["away"], int(r["hg"]), int(r["ag"]))
    log(f"{code}: {len(rows)} 场测试样本，{len(fits)} 次 DC 拟合，{time.time() - t0:.0f}s")
    return rows, fits


def predict_row(code, r, elo, model, has_ah):
    hg, ag = int(r["hg"]), int(r["ag"])
    y = 0 if hg > ag else (1 if hg == ag else 2)
    rec = dict(league=code, season=str(r["season"]), date=str(r["date"].date()), home=r["home"], away=r["away"],
               hg=hg, ag=ag, y=y)
    mats = {"elo": elo.matrix(r["home"], r["away"], CFG["max_goals"])}
    n_tr = getattr(model, "n_train", {}) if model is not None else {}
    ok_dc = model is not None and min(n_tr.get(r["home"], 0), n_tr.get(r["away"], 0)) >= CFG["min_team_matches"]
    rec["dc_ok"] = ok_dc
    if ok_dc:
        mats["dc"] = dcm.matrix(model, r["home"], r["away"], CFG["max_goals"])
        mats["dcelo"] = bl.pool_matrices(mats["dc"], mats["elo"], 1 - CFG["w_elo"])
    p1 = {k: am.probs_1x2(m) for k, m in mats.items()}
    if np.isfinite([g(r, "PSH"), g(r, "PSD"), g(r, "PSA")]).all():
        p1["mkt_open"] = tuple(am.devig([g(r, "PSH"), g(r, "PSD"), g(r, "PSA")]))
    if np.isfinite([g(r, "PSCH"), g(r, "PSCD"), g(r, "PSCA")]).all():
        p1["mkt_close"] = tuple(am.devig([g(r, "PSCH"), g(r, "PSCD"), g(r, "PSCA")]))
    if "dc" in p1 and "mkt_open" in p1:
        p1["blend"] = tuple(bl.loglinear_1x2(np.array(p1["dc"]), np.array(p1["mkt_open"]), CFG["w_blend"]))
    for k, p in p1.items():
        rec[f"p1_{k}"] = list(p)
        rec[f"ll1_{k}"] = ll_1x2(p, y)
        rec[f"br1_{k}"] = brier_1x2(p, y)
    if not has_ah:
        return rec
    lc = g(r, "AHCh")
    m_close = market_matrix(lc, g(r, "PCAHH"), g(r, "PCAHA"), g(r, "PC>2.5"), g(r, "PC<2.5"))
    lo = g(r, "AHh")
    m_open = market_matrix(lo, g(r, "PAHH"), g(r, "PAHA"), g(r, "P>2.5"), g(r, "P<2.5"))
    if m_close is not None:
        mats["mkt_close"] = m_close
    if m_open is not None:
        mats["mkt_open"] = m_open
        if "dc" in mats:
            mats["blend"] = bl.pool_matrices(mats["dc"], m_open, CFG["w_blend"])
    if not np.isfinite(lc):
        return rec
    margin = hg - ag
    outc = am.settle_margin(margin, lc)
    rec["ah_close_line"] = lc
    rec["ah_outcome_home"] = outc
    if outc != "push":
        yy = 1 if outc in ("win", "half_win") else 0
        rec["ah_y"] = yy
        for k, m in mats.items():
            q = float(np.clip(ah_ahead_prob(m, lc), 1e-9, 1 - 1e-9))
            rec[f"pah_{k}"] = q
            rec[f"llah_{k}"] = -(yy * np.log(q) + (1 - yy) * np.log(1 - q))
            rec[f"brah_{k}"] = (q - yy) ** 2
    # 模拟下注 + CLV（赛前价 -> 收盘）
    oh, oa = g(r, "PAHH"), g(r, "PAHA")
    if np.isfinite([lo, oh, oa]).all() and m_close is not None:
        ph_c, pa_c = am.devig([g(r, "PCAHH"), g(r, "PCAHA")])
        for k in ("elo", "dc", "dcelo", "blend"):
            if k not in mats:
                continue
            m = mats[k]
            ev_h = am.ev(am.ah_probs(m, lo, "home"), oh)
            ev_a = am.ev(am.ah_probs(m, -lo, "away"), oa)
            side, line, odds, ev = ("home", lo, oh, ev_h) if ev_h >= ev_a else ("away", -lo, oa, ev_a)
            if abs(lo - lc) < 1e-9:
                fair_c = 1 / (ph_c if side == "home" else pa_c)
            else:
                fair_c = am.fair_odds(am.ah_probs(m_close, line, side))
            res = am.settle_margin(margin if side == "home" else -margin, line)
            w, l_ = am.win_lose_share({o: float(o == res) for o in am.OUTCOMES})
            rec[f"bet_{k}"] = dict(side=side, line=line, odds=odds, ev=ev, clv=odds / fair_c - 1,
                                   pnl=w * (odds - 1) - l_, line_moved=bool(abs(lo - lc) > 1e-9))
    return rec


# ------------------------------------------------------------------ 统计
def boot_mean_ci(x, n=2000, seed=0):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return (np.nan, np.nan, np.nan, 0)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(x), size=(n, len(x)))
    bs = x[idx].mean(axis=1)
    return float(x.mean()), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5)), len(x)


def fmt_ci(t, pct=False, digits=4):
    m, lo, hi, n = t
    if not np.isfinite(m):
        return "—"
    f = (lambda v: f"{v * 100:+.2f}%") if pct else (lambda v: f"{v:+.{digits}f}")
    sig = "" if (lo > 0 or hi < 0) else " 不显著"
    return f"{f(m)} [{f(lo)}, {f(hi)}]{sig}"


def metric_table(d: pd.DataFrame, prefix: str, models: list[str], refs: list[str], nboot: int):
    out = []
    cols = [f"{prefix}_{k}" for k in models]
    sub = d.dropna(subset=[c for c in cols if c in d])
    for k in models:
        c = f"{prefix}_{k}"
        if c not in sub:
            continue
        row = {"模型": k, "n": len(sub), "均值": f"{sub[c].mean():.4f}"}
        for ref in refs:
            rc = f"{prefix}_{ref}"
            row[f"− {ref}（95% CI）"] = "—" if k == ref or rc not in sub else fmt_ci(
                boot_mean_ci(sub[c] - sub[rc], nboot), digits=4)
        out.append(row)
    return pd.DataFrame(out)


def calib_table(d, col_p, col_y, bins=10):
    x = d[[col_p, col_y]].dropna()
    x["bin"] = pd.cut(x[col_p], np.linspace(0, 1, bins + 1), include_lowest=True)
    t = x.groupby("bin", observed=True).agg(n=(col_y, "size"), 预测均值=(col_p, "mean"), 实际频率=(col_y, "mean"))
    return t.reset_index().assign(bin=lambda t: t["bin"].astype(str))


def bets_table(d, models, nboot):
    out = []
    buckets = [(0, 0.01), (0.01, 0.03), (0.03, 0.05), (0.05, 1.0)]
    for k in models:
        c = f"bet_{k}"
        if c not in d:
            continue
        b = pd.DataFrame([x for x in d[c] if isinstance(x, dict)])
        if b.empty:
            continue
        for lo, hi in buckets:
            s = b[(b.ev > lo) & (b.ev <= hi)] if lo > 0 else b[(b.ev > 0) & (b.ev <= hi)]
            if len(s) == 0:
                continue
            out.append({"模型": k, "EV 区间": f"({lo:.0%}, {hi:.0%}]" if hi < 1 else f"> {lo:.0%}", "注数": len(s),
                        "平均EV": f"{s.ev.mean():+.2%}", "平均 CLV（95% CI）": fmt_ci(boot_mean_ci(s.clv, nboot), pct=True),
                        "ROI（95% CI）": fmt_ci(boot_mean_ci(s.pnl, nboot), pct=True),
                        "盘口变动占比": f"{s.line_moved.mean():.0%}"})
        s = b[b.ev > 0]
        out.append({"模型": k, "EV 区间": "全部 EV>0", "注数": len(s), "平均EV": f"{s.ev.mean():+.2%}",
                    "平均 CLV（95% CI）": fmt_ci(boot_mean_ci(s.clv, nboot), pct=True),
                    "ROI（95% CI）": fmt_ci(boot_mean_ci(s.pnl, nboot), pct=True),
                    "盘口变动占比": f"{s.line_moved.mean():.0%}"})
    return pd.DataFrame(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--leagues", default="E0,E1,JPN")
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--from-pickle", action="store_true", help="跳过拟合，直接用 outputs/predictions.pkl 出报告")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    fd.download()
    logs = []

    def log(s):
        print(s, flush=True)
        logs.append(s)

    if a.from_pickle:
        d = pd.read_pickle(OUT / "predictions.pkl")
        fits = pd.read_csv(OUT / "dc_fits.csv").to_dict("records")
        log("从 outputs/predictions.pkl 读取预测（未重新拟合）")
    else:
        rows, fits = [], []
        for code in a.leagues.split(","):
            r, f = run_league(code, log)
            rows += r
            fits += f
        d = pd.DataFrame(rows)
        d.to_pickle(OUT / "predictions.pkl")
        pd.DataFrame(fits).to_csv(OUT / "dc_fits.csv", index=False)
    md = ["# 回测结果（research/backtest.py 生成，勿手改）\n", f"配置：`{json.dumps(CFG)}`\n",
          f"CI = 配对 bootstrap 95% 百分位区间（{a.boot} 次）；“不显著”= 区间跨 0。差值为负 = 比参照更好（损失更低）。\n"]
    md += ["## 运行日志\n", "\n".join(f"- {x}" for x in logs), "\n"]
    # 样本量
    samp = d.groupby(["league", "season"]).agg(测试场次=("y", "size"), DC可预测=("dc_ok", "sum")).reset_index()
    md += ["## 样本量（测试期）\n", samp.to_markdown(index=False), "\n"]
    fit_s = pd.DataFrame(fits).groupby("league")[["home_advantage", "rho"]].agg(["mean", "min", "max"]).round(3)
    md += ["## DC 参数（各次滚动拟合）\n", fit_s.to_markdown(), "\n"]
    for scope, sub in [*[(c, d[d.league == c]) for c in d.league.unique()],
                       ("E0+E1", d[d.league.isin(["E0", "E1"])])]:
        if sub.empty:
            continue
        sub = sub[sub.dc_ok]
        md.append(f"## {scope}\n")
        mods = [m for m in MODELS if f"ll1_{m}" in sub and sub[f"ll1_{m}"].notna().any()]
        for prefix, name in [("ll1", "1X2 log loss"), ("br1", "1X2 Brier")]:
            md += [f"### {name}\n", metric_table(sub, prefix, mods, ["mkt_close", "elo"], a.boot).to_markdown(index=False), "\n"]
        if "llah_dc" in sub and sub["llah_dc"].notna().any():
            amods = [m for m in MODELS if f"llah_{m}" in sub]
            for prefix, name in [("llah", "亚盘（收盘盘口，主队赢/输，剔除走盘）log loss"), ("brah", "亚盘 Brier")]:
                md += [f"### {name}\n", metric_table(sub, prefix, amods, ["mkt_close", "elo"], a.boot).to_markdown(index=False), "\n"]
            md += ["### 模拟下注：赛前 Pinnacle 价（AHh/PAHH/PAHA）上 EV>0 的一侧，CLV 与 ROI 按 EV 分桶\n",
                   bets_table(sub, ["elo", "dc", "dcelo", "blend"], a.boot).to_markdown(index=False), "\n"]
    # 校准
    dd = d[d.dc_ok].copy()
    dd["p_home_dc"] = dd["p1_dc"].map(lambda p: p[0])
    dd["home_win"] = (dd.y == 0).astype(int)
    md += ["## 校准：DC 主胜概率（全部联赛）\n", calib_table(dd, "p_home_dc", "home_win").round(3).to_markdown(index=False), "\n"]
    if "pah_dc" in dd:
        md += ["## 校准：DC 亚盘主队赢（收盘盘口）\n", calib_table(dd, "pah_dc", "ah_y").round(3).to_markdown(index=False), "\n"]
    (OUT / "backtest_results.md").write_text("\n".join(md), encoding="utf-8")
    print("written", OUT / "backtest_results.md")


if __name__ == "__main__":
    main()
