"""可回溯基本面因子（三态准入）：只把有真实来源的值写进评分。

依据：深度研究报告第一/三节——「声明有权重」不等于「数据充足」。
硬约束（AGENTS.md 防推测/防幻觉/防断言）：
- 每个因子必须带 source_id / source_url / as_of / coverage_status；
- 数据不足时 value=None，绝不填 0、绝不按联赛常识推断；
- injuries / xg 无授权且未逐场取证，保持 missing，不硬上；
- observation 模式只写证据、不进 score；scoring 模式才进 analyze.Candidate.features；
- promote 需连续多日覆盖达标 + 离线重放不把当日推荐打回 0，否则拒绝启用。

不新增任何外部 API 调用：所有输入来自报告 JSON、football-data 已缓存的
standings、form_history 已落盘的逐场赛果。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import CONFIG, DATA, REPORTS
from .match import parse_dt, team_similarity

MISSING = "未取得"
COVERAGE_DIR = DATA / "feature_coverage"
READINESS = DATA / "feature_readiness.json"

# key -> (label, weight_in_analyze, source_url, min_samples)
SPECS = {
    "home_away": ("主客场分离战绩", 0.8, "data/history/results.csv（form_history 逐场赛果）", 3),
    "motivation": ("积分榜战意", 0.8, "https://www.football-data.org/ standings", 1),
    "line_value": ("盘口深度与共识一致性", 1.0, "report.market_consensus（多源盘口快照）", 3),
    "injuries": ("伤停 / 首发", 1.2, MISSING, 0),
    "xg": ("xG 攻防质量", 1.0, MISSING, 0),
}

DEFAULT_PROMOTION = {
    "min_consecutive_days": 14,
    "min_coverage_ratio": 0.80,
    "min_samples": 80,
    "reject_if_replay_zero_picks": True,
}


def settings() -> dict:
    return yaml.safe_load((CONFIG / "settings.yaml").read_text(encoding="utf-8")) or {}


def mode() -> str:
    m = str((settings().get("feature_scoring") or {}).get("mode") or "observation").strip().lower()
    return m if m in ("off", "observation", "scoring") else "observation"


def promotion_rules() -> dict:
    return {**DEFAULT_PROMOTION, **((settings().get("feature_scoring") or {}).get("promotion") or {})}


def _ev(key, value, source_id, as_of, coverage, note) -> dict:
    label, weight, url, min_samples = SPECS[key]
    return {
        "key": key, "label": label, "weight": weight,
        "value": None if value is None else round(max(-2.0, min(2.0, float(value))), 3),
        "source_id": source_id, "source_url": url,
        "as_of": as_of or MISSING,
        "coverage_status": coverage, "min_samples": min_samples,
        "note": note,
    }


# ---------------------------------------------------------------- 因子计算

def line_value_feature(pick: dict) -> dict:
    """盘口深度惩罚 + 多源共识一致性。全部取自报告里真实存在的字段。

    - |line| 越大越负（深盘惩罚，与 policy.max_abs_line_to_publish 同向）；
    - agree/bookmakers 比例映射到 -1..+1（家数 <3 或字段缺失时不判）；
    - 不使用收盘最优价、不把报价超时当价值证据。
    """
    c = pick.get("market_consensus") or {}
    line = pick.get("line")
    ko = pick.get("kickoff_utc")
    if line is None or not c:
        return _ev("line_value", None, "report.market_consensus", ko, "missing",
                   "报告未提供 market_consensus 或 line，未取值")
    n, agree = c.get("bookmakers"), c.get("agree")
    if not isinstance(n, (int, float)) or n < 3 or not isinstance(agree, (int, float)):
        return _ev("line_value", None, "report.market_consensus", ko, "uncertain",
                   "共识家数 <3 或 agree 缺失，按不确定处理，不填 0")
    try:
        depth = -min(2.0, max(0.0, (abs(float(line)) - 0.5) * 0.8))
    except (TypeError, ValueError):
        return _ev("line_value", None, "report.market_consensus", ko, "missing", "line 无法解析")
    ratio = float(agree) / float(n)
    consistency = max(-1.0, min(1.0, (ratio - 0.5) * 2))
    val = max(-2.0, min(2.0, depth + consistency))
    return _ev("line_value", val, "report.market_consensus", ko, "full",
               f"真实字段：line={line}、bookmakers={n}、agree={agree}；单方水位未去水，非成交价结论")


def _points_rate(games: list[tuple]) -> float | None:
    """games: [(kickoff, my_goals, opp_goals)] -> 场均积分率 0..3。"""
    if not games:
        return None
    pts = sum(3 if a > b else 1 if a == b else 0 for _, a, b in games)
    return pts / len(games)


def home_away_feature(rows: list[dict], home: str, away: str, kickoff, pick_side: str,
                      n: int = 6) -> dict:
    """主队只取主场、客队只取客场的近期积分率差，方向相对推荐侧。

    数据源为 form_history 逐场赛果（data/history/results.csv），不新增接口调用；
    任一侧不足 3 场返回 value=None（不填 0）。
    """
    ko = parse_dt(kickoff) if kickoff else None
    if ko is None or not rows:
        return _ev("home_away", None, "form_history/results.csv", kickoff, "missing", "无逐场赛果库或开球时间")
    hg, ag = [], []
    for r in rows:
        try:
            when = parse_dt(r["kickoff_utc"])
            h_goals, a_goals = int(r["home_goals"]), int(r["away_goals"])
        except (KeyError, TypeError, ValueError):
            continue
        if when >= ko:
            continue
        rh, ra = str(r.get("home") or ""), str(r.get("away") or "")
        if team_similarity(rh, home, None) >= 0.75:
            hg.append((when, h_goals, a_goals))          # 主队的主场
        if team_similarity(ra, away, None) >= 0.75:
            ag.append((when, a_goals, h_goals))          # 客队的客场（自身进球在前）
    hg.sort(key=lambda x: x[0], reverse=True)
    ag.sort(key=lambda x: x[0], reverse=True)
    hh, aa = hg[:n], ag[:n]
    if len(hh) < 3 or len(aa) < 3:
        return _ev("home_away", None, "form_history/results.csv", kickoff, "partial",
                   f"主队主场 {len(hh)} 场 / 客队客场 {len(aa)} 场，不足 3 场不取值")
    rh, ra = _points_rate(hh), _points_rate(aa)
    diff = (rh - ra) if pick_side == "home" else (ra - rh)
    val = max(-2.0, min(2.0, diff / 1.5))
    return _ev("home_away", val, "form_history/results.csv", kickoff, "full",
               f"真实赛果：主队主场 {len(hh)} 场均 {rh:.2f} 分、客队客场 {len(aa)} 场均 {ra:.2f} 分；推荐侧={pick_side}")


def motivation_feature(fundamental: dict | None, pick_side: str, kickoff) -> dict:
    """积分榜名次差（football-data.org standings，已在原流程请求、TTL 6h，不额外耗额度）。

    只有 report 里真实存在 standings.home/away.position 才取值；缺则 None。
    """
    st = (fundamental or {}).get("standings") or {}
    h, a = st.get("home"), st.get("away")
    if not (isinstance(h, dict) and isinstance(a, dict)
            and isinstance(h.get("position"), int) and isinstance(a.get("position"), int)):
        return _ev("motivation", None, "football-data.org standings", kickoff, "missing",
                   "该场未取得双方积分榜名次，不推断战意")
    hp, ap = h["position"], a["position"]
    diff = (ap - hp) if pick_side == "home" else (hp - ap)
    val = max(-2.0, min(2.0, diff / 10.0))
    return _ev("motivation", val, "football-data.org standings", kickoff, "full",
               f"真实积分榜：{h.get('position')} 名 vs {a.get('position')} 名；推荐侧={pick_side}；不含伤停/首发")


def unavailable(key: str, kickoff) -> dict:
    label, weight, url, ms = SPECS[key]
    return _ev(key, None, MISSING, kickoff, "missing",
               "无授权接口或未逐场取证；保持未取得，不填 0、不编造")


def build_evidence(pick: dict, fundamental: dict | None = None,
                   history_rows: list[dict] | None = None) -> dict:
    """为单场正式推荐生成全部因子证据；不做任何预测、不修改 report。"""
    ko = pick.get("kickoff_utc")
    side = pick.get("pick_side") or "home"
    out = {
        "line_value": line_value_feature(pick),
        "home_away": home_away_feature(history_rows or [], pick.get("home") or "",
                                       pick.get("away") or "", ko, side),
        "motivation": motivation_feature(fundamental, side, ko),
        "injuries": unavailable("injuries", ko),
        "xg": unavailable("xg", ko),
    }
    for k in ("form", "h2h", "line_movement"):
        out[k] = {
            "key": k, "label": {"form": "近期状态", "h2h": "交锋记录",
                                "line_movement": "初盘→即时盘口变化"}[k],
            "weight": {"form": 1.2, "h2h": 0.3, "line_movement": 1.5}[k],
            "value": MISSING, "source_id": "analyze.Candidate.features（原有评分链）",
            "source_url": "report.reasons / report.line_open", "as_of": ko or MISSING,
            "coverage_status": "scoring", "min_samples": 3,
            "note": "原有三因子由 daily.assess 写入；本报告证据块不重复计算、不覆盖其取值",
        }
    return out


def apply_to_features(features: dict, evidence: dict) -> dict:
    """scoring 模式才把有证据的 value 写进 analyze.Candidate.features；None 一律不写。"""
    if mode() != "scoring":
        return features
    out = dict(features)
    for k in ("home_away", "motivation", "line_value"):
        v = (evidence.get(k) or {}).get("value")
        if isinstance(v, (int, float)) and (evidence.get(k) or {}).get("coverage_status") == "full":
            out[k] = float(v)
    return out



def pre_match_values(match: dict, fundamental: dict | None, history_rows: list[dict] | None) -> dict:
    """评分前按两侧方向分别计算 home_away / motivation。

    返回 {'home': {k: value|None, ...}, 'away': {...}} 以及证据块；
    None 一律不写进 features（不填 0）。仅在 mode=scoring 时由 daily.assess 使用。
    """
    ko = match.get("kickoff_utc")
    out = {}
    for side in ("home", "away"):
        ha = home_away_feature(history_rows or [], match.get("home") or "",
                               match.get("away") or "", ko, side)
        mo = motivation_feature(fundamental, side, ko)
        out[side] = {
            "features": {k: v["value"] for k, v in (("home_away", ha), ("motivation", mo))
                         if v["value"] is not None and v["coverage_status"] == "full"},
            "evidence": {"home_away": ha, "motivation": mo},
        }
    return out


def final_evidence(pick: dict, fundamental: dict | None = None,
                   history_rows: list[dict] | None = None,
                   pre: dict | None = None) -> dict:
    """生成写入报告的逐场证据块；不修改 pick、不生成预测。"""
    ev = build_evidence(pick, fundamental, history_rows)
    side = pick.get("pick_side") or "home"
    if pre and side in pre:
        for k, v in (pre[side].get("evidence") or {}).items():
            ev[k] = v
    return {
        "schema": 1,
        "match_id": pick.get("match_id") or MISSING,
        "mode": mode(),
        "note": ("scoring：带 full 证据的因子已参与 score；observation：仅记录证据，未参与 score。"
                 "injuries / xg 无授权接口，保持未取得；line_value 因共识在评分后 attach，暂不进 score。"),
        "factors": ev,
    }


# ---------------------------------------------------------------- 覆盖日志与准入

def write_coverage(date: str, report: dict, out_dir: Path | None = None) -> dict:
    """每天落一份逐场覆盖事实，供 readiness 自动验收。不推断、不补值。"""
    rows = []
    for p in report.get("picks") or []:
        ev = build_evidence(p)
        rows.append({"match_id": p.get("match_id") or MISSING,
                     "factors": {k: {"value": v.get("value"), "coverage": v.get("coverage_status")}
                                 for k, v in ev.items()}})
    payload = {
        "schema": 1, "date": date,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": mode(), "picks": len(rows), "rows": rows,
        "note": "仅统计报告里真实存在的字段；value=None 表示未取得，不代表 0 分",
    }
    out = (Path(out_dir) if out_dir else COVERAGE_DIR) / f"{date}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"FACTOR_COVERAGE date={date} mode={mode()} picks={len(rows)} out={out}")
    return payload


def readiness(out: Path | None = None, now: datetime | None = None) -> dict:
    """按连续覆盖天数 + 样本量做自动验收；不够就如实标 not_ready，不假称可评分。"""
    rules = promotion_rules()
    files = sorted((COVERAGE_DIR).glob("*.json"))
    days = []
    for f in files:
        try:
            days.append(json.loads(f.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
    keys = ("home_away", "motivation", "line_value")
    per = {}
    for k in keys:
        streak, total_full, total_picks = 0, 0, 0
        for d in reversed(days):
            rs = d.get("rows") or []
            full = sum(1 for r in rs if (r.get("factors") or {}).get(k, {}).get("coverage") == "full")
            total_full += full
            total_picks += len(rs)
            if rs and full / len(rs) >= float(rules["min_coverage_ratio"]):
                streak += 1
            else:
                break
        per[k] = {
            "days_observed": len(days), "consecutive_full_days": streak,
            "full_rows": total_full, "total_rows": total_picks,
            "coverage_ratio": round(total_full / total_picks, 4) if total_picks else None,
            "required": {"min_consecutive_days": rules["min_consecutive_days"],
                         "min_coverage_ratio": rules["min_coverage_ratio"],
                         "min_samples": rules["min_samples"]},
            "eligible": (streak >= int(rules["min_consecutive_days"])
                         and total_full >= int(rules["min_samples"])),
        }
    payload = {
        "schema": 1,
        "checked_at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "current_mode": mode(), "factors": per,
        "all_eligible": all(v["eligible"] for v in per.values()),
        "status": "READY_TO_PROMOTE" if all(v["eligible"] for v in per.values()) else "NOT_READY",
        "note": "未达标一律不得把因子转入 scoring；injuries / xg 无授权接口，永久保持 missing，除非单独取得逐场证据。",
    }
    dst = Path(out) if out else READINESS
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"FACTOR_READINESS status={payload['status']} mode={mode()} out={dst}")
    return payload


def replay(date: str, reports: Path | None = None) -> dict:
    """离线重放：若启用 scoring，当日报告档位/推荐数会怎样。不改报告、不发消息。"""
    from . import analyze
    src = (Path(reports) if reports else REPORTS) / date[:4] / date[5:7] / f"{date}.json"
    if not src.exists():
        return {"date": date, "status": "REPORT_MISSING", "note": f"报告不存在：{src}"}
    report = json.loads(src.read_text(encoding="utf-8"))
    cfg = settings()
    before = [p for p in (report.get("picks") or []) if not p.get("cancelled")]
    changed = []
    for p in before:
        ev = build_evidence(p)
        extra = {}
        for k in ("home_away", "motivation", "line_value"):
            v = (ev.get(k) or {}).get("value")
            if isinstance(v, (int, float)) and ev[k]["coverage_status"] == "full":
                extra[k] = v
        # 原 score 无法从报告逐因子复原，故只报告「可写入的因子」与理论增量，不伪造新 score。
        delta = sum(analyze.WEIGHTS.get(k, 0) * v for k, v in extra.items())
        changed.append({"match_id": p.get("match_id"), "score": p.get("score"),
                        "confidence": p.get("confidence"),
                        "extra_features": extra, "theoretical_score_delta": round(delta, 2)})
    return {
        "date": date, "status": "REPLAY_OK", "mode": "scoring_if_enabled",
        "picks_before": len(before), "rows": changed,
        "note": ("理论增量仅供评估，不改写历史报告；真实启用需 readiness 达标并通过零场保护。"
                 "注意：line_value 当前由深度惩罚与共识一致性构成，实测该日增量多为正值，"
                 "方向与幅度均未经时间外校准；不得据此宣称评分更准或准确率提升。"),
    }


def enable_scoring(out: Path | None = None) -> dict:
    """达标且离线重放不把当日推荐打回 0，才把 settings.yaml 的 mode 改为 scoring。"""
    r = readiness()
    p = CONFIG / "settings.yaml"
    text = p.read_text(encoding="utf-8")
    result = {"status": "REJECTED", "reason": "", "readiness": r["status"]}
    if not r["all_eligible"]:
        result["reason"] = "覆盖/样本未达标，拒绝启用 scoring（不编造数据）"
    elif "mode: observation" not in text and "mode: scoring" not in text:
        result["reason"] = "settings.yaml 缺少 feature_scoring.mode 锚点，拒绝猜测式改写"
    else:
        # 零场保护：用最新报告离线重放，理论增量若把任一场拉到 low 门槛以下则拒绝。
        latest = sorted(REPORTS.glob("*/*/*.json"))
        rep = replay(latest[-1].stem) if latest else {"status": "REPORT_MISSING"}
        if rep.get("status") == "REPORT_MISSING":
            result["reason"] = "无报告可离线重放，拒绝启用"
        else:
            low = float(settings()["confidence_thresholds"]["low"])
            risk = [x for x in rep["rows"]
                    if isinstance(x.get("score"), (int, float))
                    and float(x["score"]) + float(x["theoretical_score_delta"]) < low]
            if risk and promotion_rules().get("reject_if_replay_zero_picks"):
                result["reason"] = f"离线重放显示 {len(risk)} 场会跌破发布门槛，拒绝启用（保护「不能 0 场」）"
            else:
                p.write_text(text.replace("mode: observation", "mode: scoring", 1), encoding="utf-8")
                result = {"status": "ENABLED", "reason": "readiness 达标且离线重放无场次跌破门槛",
                          "readiness": r["status"], "replay": rep["status"]}
    dst = Path(out) if out else DATA / "feature_promotion.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"FACTOR_PROMOTION {result['status']} {result['reason']} out={dst}")
    return result


def main(argv: list[str] | None = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="可回溯因子：覆盖日志 / 自动验收 / 离线重放 / 准入")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("coverage")
    p1.add_argument("date")
    p1.add_argument("--reports", default=None)
    p1.add_argument("--out-dir", default=None)
    sub.add_parser("readiness")
    p3 = sub.add_parser("replay")
    p3.add_argument("date")
    p3.add_argument("--reports", default=None)
    sub.add_parser("promote")
    a = ap.parse_args(argv)
    if a.cmd == "coverage":
        src = Path(a.reports) / a.date[:4] / a.date[5:7] / f"{a.date}.json" if a.reports \
            else REPORTS / a.date[:4] / a.date[5:7] / f"{a.date}.json"
        write_coverage(a.date, json.loads(src.read_text(encoding="utf-8")), a.out_dir)
    elif a.cmd == "readiness":
        readiness()
    elif a.cmd == "replay":
        print(json.dumps(replay(a.date, a.reports), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(enable_scoring(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
