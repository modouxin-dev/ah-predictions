"""模型 B（Dixon-Coles）概率校准：温度缩放 vs 等渗（PAV）walk-forward 对比。

用法：../.venv/bin/python calibrate_dc.py [--leagues E0,E1,JPN] [--boot 2000] [--min-past 200]
- 严格前推：每个自然周块开始前，只用该块之前的（预测, 赛果）拟合 T / PAV；块内预测再套校准。
- 指标：1X2 log loss；亚盘 settled-score Brier（走盘保留，y∈{1,0.75,0.5,0.25,0}）；
  亚盘二元 log loss（走盘剔除）；分箱校准；CLV（赛前 EV>0 侧 vs 收盘无水）。
- 所有数字由本脚本写出 research/outputs/calibration_dc.md，勿手改。
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
from ah.model import calibration as cal  # noqa: E402
from ah.model import dixon_coles as dcm  # noqa: E402
from ah.model.scoring import expected_score, settled_score  # noqa: E402

OUT = HERE / "outputs"
CFG = dict(xi=0.0018, window_days=1095, min_team_matches=10, max_goals=12)
TEST_START = {"E0": "2021-07-01", "E1": "2021-07-01", "JPN": "2019-01-01"}


def g(r, c):
    v = r.get(c, np.nan)
    try:
        return float(v)
    except (TypeError, ValueError):
        return np.nan


def market_matrix(line, oh, oa, oo, ou):
    if not all(np.isfinite([line, oh, oa])):
        return None
    ph = am.devig([oh, oa])[0]
    po = am.devig([oo, ou])[0] if np.isfinite(oo) and np.isfinite(ou) else None
    return am.implied_matrix(line, float(ph), None if po is None else float(po))


def ll_1x2(p, y):
    return -np.log(max(float(p[y]), 1e-12))


def boot_mean_ci(x, n=2000, seed=0):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return (np.nan, np.nan, np.nan, 0)
    rng = np.random.default_rng(seed)
    bs = x[rng.integers(0, len(x), size=(n, len(x)))].mean(axis=1)
    return float(x.mean()), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5)), len(x)


def fmt_ci(t, pct=False, digits=4):
    m, lo, hi, n = t
    if not np.isfinite(m):
        return "—"
    f = (lambda v: f"{v * 100:+.2f}%") if pct else (lambda v: f"{v:+.{digits}f}")
    sig = "" if (lo > 0 or hi < 0) else " 不显著"
    return f"{f(m)} [{f(lo)}, {f(hi)}] n={n}{sig}"


def calib_bins(p, y, bins=10):
    x = pd.DataFrame({"p": np.asarray(p, float), "y": np.asarray(y, float)}).dropna()
    if x.empty:
        return pd.DataFrame()
    x["bin"] = pd.cut(x["p"], np.linspace(0, 1, bins + 1), include_lowest=True)
    t = x.groupby("bin", observed=True).agg(n=("y", "size"), 预测均值=("p", "mean"), 实际=("y", "mean"))
    return t.reset_index().assign(bin=lambda d: d["bin"].astype(str))


def fit_calibrators(hist: list[dict], min_past: int):
    """只用 hist（过去已实现场次）拟合。样本不足则返回恒等校准。"""
    T1, Tah = 1.0, 1.0
    pav_ah = None
    if len(hist) >= min_past:
        P = np.array([h["p1"] for h in hist], float)
        y = np.array([h["y"] for h in hist], int)
        T1 = cal.fit_t_multiclass(P, y)
    ah_hist = [h for h in hist if h.get("ah_y") is not None and np.isfinite(h.get("pah", np.nan))]
    if len(ah_hist) >= min_past:
        q = np.array([h["pah"] for h in ah_hist], float)
        yy = np.array([h["ah_y"] for h in ah_hist], float)
        Tah = cal.fit_t_binary(q, yy)
        pav_ah = cal.pav_fit(q, yy)
    return cal.TemperatureCalibrator(t_1x2=T1, t_ah=Tah), pav_ah


def apply_row(raw_p1, raw_ah, Tcal, pav_ah):
    """返回 raw / temp / iso 三套 1x2 与亚盘概率。"""
    out = {"raw": {"p1": list(raw_p1)}}
    out["temp"] = {"p1": list(Tcal.p1x2(raw_p1))}
    # iso 不做 1x2（多分类 PAV 另议）；1x2 仅温度
    out["iso"] = {"p1": list(raw_p1)}
    if raw_ah is not None:
        out["raw"]["ah"] = dict(raw_ah)
        out["temp"]["ah"] = Tcal.ah(raw_ah)
        if pav_ah is not None:
            q = cal.ahead_prob(raw_ah)
            q2 = float(cal.pav_predict(pav_ah, [q])[0]) if np.isfinite(q) else q
            out["iso"]["ah"] = cal.recalibrate_ah_to_q(raw_ah, q2) if np.isfinite(q) else dict(raw_ah)
        else:
            out["iso"]["ah"] = dict(raw_ah)
    return out


def run_league(code: str, min_past: int, log) -> list[dict]:
    df = fd.load_new(code) if code in fd.NEW else fd.load_main(code)
    df = df.dropna(subset=["date"]).sort_values(["date"]).reset_index(drop=True)
    has_ah = "AHCh" in df.columns
    df["block"] = df["date"].dt.to_period("W-SUN")
    start = pd.Timestamp(TEST_START[code])
    hist: list[dict] = []  # 仅测试期内、已有赛果的过去预测，供校准
    rows: list[dict] = []
    t0 = time.time()
    n_fit = 0
    for _, b in df.groupby("block", sort=True):
        bstart = b["date"].min()
        if bstart < start:
            continue
        try:
            model = dcm.fit(df, bstart, xi=CFG["xi"], window_days=CFG["window_days"])
            n_fit += 1
        except Exception as e:  # noqa: BLE001
            log(f"{code} {bstart.date()} DC 拟合失败: {e}")
            model = None
        Tcal, pav_ah = fit_calibrators(hist, min_past)
        block_hist_add = []
        for _, r in b.iterrows():
            hg, ag = int(r["hg"]), int(r["ag"])
            y = 0 if hg > ag else (1 if hg == ag else 2)
            n_tr = getattr(model, "n_train", {}) if model is not None else {}
            ok = model is not None and min(n_tr.get(r["home"], 0), n_tr.get(r["away"], 0)) >= CFG["min_team_matches"]
            rec = dict(
                league=code,
                date=str(r["date"].date()),
                home=r["home"],
                away=r["away"],
                y=y,
                dc_ok=ok,
                t_1x2=Tcal.t_1x2,
                t_ah=Tcal.t_ah,
                hist_n=len(hist),
                hist_ah_n=sum(1 for h in hist if h.get("ah_y") is not None),
            )
            if not ok:
                rows.append(rec)
                continue
            m = dcm.matrix(model, r["home"], r["away"], CFG["max_goals"])
            raw_p1 = list(am.probs_1x2(m))
            raw_ah = None
            lc = g(r, "AHCh") if has_ah else np.nan
            if has_ah and np.isfinite(lc):
                raw_ah = am.ah_probs(m, lc, "home")
            variants = apply_row(raw_p1, raw_ah, Tcal, pav_ah)
            for name, v in variants.items():
                p1 = v["p1"]
                rec[f"p1_{name}"] = p1
                rec[f"ll1_{name}"] = ll_1x2(p1, y)
            # 市场参照
            if np.isfinite([g(r, "PSCH"), g(r, "PSCD"), g(r, "PSCA")]).all():
                pm = list(am.devig([g(r, "PSCH"), g(r, "PSCD"), g(r, "PSCA")]))
                rec["p1_mkt_close"] = pm
                rec["ll1_mkt_close"] = ll_1x2(pm, y)
            if raw_ah is not None:
                outc = am.settle_margin(hg - ag, lc)
                rec["ah_close_line"] = lc
                rec["ah_outcome"] = outc
                y_settle = settled_score(outc)
                rec["ah_y_settle"] = y_settle
                ah_y = None if outc == "push" else (1 if outc in ("win", "half_win") else 0)
                rec["ah_y"] = ah_y
                for name, v in variants.items():
                    probs = v["ah"]
                    rec[f"ahp_{name}"] = probs
                    pc = expected_score(probs)
                    rec[f"pcov_{name}"] = pc
                    rec[f"br_settle_{name}"] = (pc - y_settle) ** 2
                    q = cal.ahead_prob(probs)
                    rec[f"pah_{name}"] = q
                    if ah_y is not None and np.isfinite(q):
                        q = float(np.clip(q, 1e-9, 1 - 1e-9))
                        rec[f"llah_{name}"] = -(ah_y * np.log(q) + (1 - ah_y) * np.log(1 - q))
                # 市场 close 亚盘
                m_close = market_matrix(lc, g(r, "PCAHH"), g(r, "PCAHA"), g(r, "PC>2.5"), g(r, "PC<2.5"))
                if m_close is not None:
                    mp = am.ah_probs(m_close, lc, "home")
                    rec["pcov_mkt_close"] = expected_score(mp)
                    rec["br_settle_mkt_close"] = (rec["pcov_mkt_close"] - y_settle) ** 2
                    mq = cal.ahead_prob(mp)
                    rec["pah_mkt_close"] = mq
                    if ah_y is not None and np.isfinite(mq):
                        mq = float(np.clip(mq, 1e-9, 1 - 1e-9))
                        rec["llah_mkt_close"] = -(ah_y * np.log(mq) + (1 - ah_y) * np.log(1 - mq))
                # CLV：赛前开盘价上 EV>0 的一侧
                lo, oh, oa = g(r, "AHh"), g(r, "PAHH"), g(r, "PAHA")
                m_open = market_matrix(lo, oh, oa, g(r, "P>2.5"), g(r, "P<2.5"))
                if np.isfinite([lo, oh, oa]).all() and m_close is not None:
                    ph_c, pa_c = am.devig([g(r, "PCAHH"), g(r, "PCAHA")])
                    for name, v in variants.items():
                        # 用开盘盘口下的模型概率（raw 矩阵 + 该变体的 AH 温度/等渗）
                        # 开盘盘口概率：对 raw 矩阵算开盘 probs，再按同一 T / PAV 变换
                        raw_open = am.ah_probs(m, lo, "home")
                        raw_open_a = am.ah_probs(m, -lo, "away")
                        if name == "raw":
                            ph, pa = raw_open, raw_open_a
                        elif name == "temp":
                            ph, pa = Tcal.ah(raw_open), Tcal.ah(raw_open_a)
                        else:
                            if pav_ah is None:
                                ph, pa = raw_open, raw_open_a
                            else:
                                qh, qa = cal.ahead_prob(raw_open), cal.ahead_prob(raw_open_a)
                                ph = cal.recalibrate_ah_to_q(raw_open, float(cal.pav_predict(pav_ah, [qh])[0])) if np.isfinite(qh) else raw_open
                                pa = cal.recalibrate_ah_to_q(raw_open_a, float(cal.pav_predict(pav_ah, [qa])[0])) if np.isfinite(qa) else raw_open_a
                        ev_h, ev_a = am.ev(ph, oh), am.ev(pa, oa)
                        side, line, odds, ev_ = ("home", lo, oh, ev_h) if ev_h >= ev_a else ("away", -lo, oa, ev_a)
                        if ev_ <= 0:
                            continue
                        if abs(lo - lc) < 1e-9:
                            fair_c = 1 / (ph_c if side == "home" else pa_c)
                        else:
                            fair_c = am.fair_odds(am.ah_probs(m_close, line, side))
                        rec[f"bet_{name}"] = dict(side=side, line=line, odds=odds, ev=ev_, clv=odds / fair_c - 1)
            rows.append(rec)
            # 供下一块拟合：本场预测（raw）+ 赛果
            h = {"p1": raw_p1, "y": y, "pah": cal.ahead_prob(raw_ah) if raw_ah else np.nan,
                 "ah_y": None if raw_ah is None else (None if am.settle_margin(hg - ag, lc) == "push"
                                                      else (1 if am.settle_margin(hg - ag, lc) in ("win", "half_win") else 0))}
            if raw_ah is not None and h["ah_y"] is None and am.settle_margin(hg - ag, lc) == "push":
                h["ah_y"] = None  # 走盘不进二元拟合
            block_hist_add.append(h)
        hist.extend(block_hist_add)
    log(f"{code}: {len(rows)} 场，{n_fit} 次 DC 拟合，{time.time() - t0:.0f}s；末块 T1x2={Tcal.t_1x2:.3f} Tah={Tcal.t_ah:.3f}")
    return rows


def metric_diff_table(d, col_a, col_b, nboot):
    """a − b，负值 = a 更好（损失更低）。"""
    sub = d[[col_a, col_b]].dropna()
    if sub.empty:
        return "—"
    return fmt_ci(boot_mean_ci(sub[col_a] - sub[col_b], nboot))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--leagues", default="E0,E1,JPN")
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--min-past", type=int, default=200)
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    fd.download()
    logs = []

    def log(s):
        print(s, flush=True)
        logs.append(s)

    rows = []
    for code in a.leagues.split(","):
        rows += run_league(code.strip(), a.min_past, log)
    d = pd.DataFrame(rows)
    d.to_pickle(OUT / "calibration_preds.pkl")

    md = [
        "# Dixon-Coles 校准对比（research/calibrate_dc.py 生成，勿手改）\n",
        f"配置：`{json.dumps({**CFG, 'min_past': a.min_past})}`\n",
        "校准：每个自然周块开始前，用**此前全部**测试期 (预测,赛果) 拟合；温度 T 与 PAV 等渗均严格前推，无泄漏。\n",
        f"CI = 配对 bootstrap 95%（{a.boot} 次）；差值为负 = 比参照更好；跨 0 标「不显著」。\n",
        "## 运行日志\n",
        "\n".join(f"- {x}" for x in logs),
        "\n",
    ]
    # T 轨迹摘要
    dd = d[d.dc_ok].copy()
    md += [
        "## 温度轨迹摘要（按联赛末值 / 中位）\n",
        dd.groupby("league")[["t_1x2", "t_ah"]]
        .agg(["median", "last", "min", "max"])
        .round(3)
        .to_markdown(),
        "\n",
    ]

    scopes = [(c, dd[dd.league == c]) for c in dd.league.unique()]
    scopes.append(("E0+E1", dd[dd.league.isin(["E0", "E1"])]))

    for scope, sub in scopes:
        if sub.empty:
            continue
        md.append(f"## {scope}\n")
        # 1X2
        md.append("### 1X2 log loss\n")
        rows_m = []
        for name in ("raw", "temp"):
            c = f"ll1_{name}"
            if c not in sub or sub[c].notna().sum() == 0:
                continue
            m, lo, hi, n = boot_mean_ci(sub[c].dropna(), a.boot)
            row = {"变体": name, "n": n, "均值": f"{m:.4f}",
                   "− raw": "—" if name == "raw" else metric_diff_table(sub, c, "ll1_raw", a.boot)}
            if "ll1_mkt_close" in sub:
                row["− mkt_close"] = metric_diff_table(sub, c, "ll1_mkt_close", a.boot)
            rows_m.append(row)
        md += [pd.DataFrame(rows_m).to_markdown(index=False), "\n"]

        if "br_settle_raw" in sub and sub["br_settle_raw"].notna().any():
            md.append("### 亚盘 settled-score Brier（走盘保留，y=1/0.75/0.5/0.25/0）\n")
            rows_m = []
            for name in ("raw", "temp", "iso"):
                c = f"br_settle_{name}"
                if c not in sub or sub[c].notna().sum() == 0:
                    continue
                m, lo, hi, n = boot_mean_ci(sub[c].dropna(), a.boot)
                row = {"变体": name, "n": n, "均值": f"{m:.4f}",
                       "− raw": "—" if name == "raw" else metric_diff_table(sub, c, "br_settle_raw", a.boot)}
                if "br_settle_mkt_close" in sub:
                    row["− mkt_close"] = metric_diff_table(sub, c, "br_settle_mkt_close", a.boot)
                rows_m.append(row)
            md += [pd.DataFrame(rows_m).to_markdown(index=False), "\n"]

            md.append("### 亚盘二元 log loss（走盘剔除）\n")
            rows_m = []
            for name in ("raw", "temp", "iso"):
                c = f"llah_{name}"
                if c not in sub or sub[c].notna().sum() == 0:
                    continue
                m, lo, hi, n = boot_mean_ci(sub[c].dropna(), a.boot)
                row = {"变体": name, "n": n, "均值": f"{m:.4f}",
                       "− raw": "—" if name == "raw" else metric_diff_table(sub, c, "llah_raw", a.boot)}
                if "llah_mkt_close" in sub:
                    row["− mkt_close"] = metric_diff_table(sub, c, "llah_mkt_close", a.boot)
                rows_m.append(row)
            md += [pd.DataFrame(rows_m).to_markdown(index=False), "\n"]

            # 校准表
            md.append("### 校准表：亚盘 pah（二元，走盘剔除）\n")
            for name, label in (("raw", "校准前 raw"), ("temp", "温度 temp"), ("iso", "等渗 iso")):
                c = f"pah_{name}"
                if c not in sub:
                    continue
                s2 = sub.dropna(subset=[c, "ah_y"])
                md += [f"**{label}** n={len(s2)}\n", calib_bins(s2[c], s2["ah_y"]).round(3).to_markdown(index=False), "\n"]

            # CLV
            md.append("### CLV（赛前 EV>0 侧，相对收盘无水）\n")
            rows_m = []
            for name in ("raw", "temp", "iso"):
                c = f"bet_{name}"
                if c not in sub:
                    continue
                bets = pd.DataFrame([x for x in sub[c] if isinstance(x, dict)])
                if bets.empty:
                    continue
                rows_m.append({
                    "变体": name,
                    "注数": len(bets),
                    "平均EV": f"{bets.ev.mean():+.2%}",
                    "平均 CLV": fmt_ci(boot_mean_ci(bets.clv, a.boot), pct=True),
                })
            if rows_m:
                md += [pd.DataFrame(rows_m).to_markdown(index=False), "\n"]
                # paired CLV temp-raw on intersection of bet rows is hard; compare mean CLV CIs above

    # 建议
    md += [
        "## 建议（自动摘要，详见上表数字）\n",
        "比较 temp / iso 相对 raw 的 settled Brier 与二元 log loss 差值 CI：\n",
        "- 若 CI 整段 < 0 → 该校准显著改善，可采纳进模型 B；\n",
        "- 若跨 0 → 不显著，维持 raw 或仅作研究记录；\n",
        "- 等渗若优于温度但样本外不稳定（末段 T 或 PAV 波动大），优先温度（参数少）。\n",
    ]
    (OUT / "calibration_dc.md").write_text("\n".join(md), encoding="utf-8")
    print("written", OUT / "calibration_dc.md")


if __name__ == "__main__":
    main()
