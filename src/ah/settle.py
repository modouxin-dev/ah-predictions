"""亚盘结算（含四分之一盘）。

约定：line 为“所选一方”的让球数，负数表示让球（例如 -0.75 = 让半一），
正数表示受让。按所选一方净胜球 + line 计算。

四分之一盘(±0.25/±0.75...) 拆成相邻两个半盘/整盘各下注一半。
"""
from __future__ import annotations
from ah.degrade import record as _degrade_fact

import argparse
import csv
import logging
from decimal import Decimal
from pathlib import Path

log = logging.getLogger("ah.settle")

OUTCOMES = ("win", "half_win", "push", "half_loss", "loss")

# ---------- predictions.csv 结构（v2，向后兼容 v1 的 15 列） ----------
LEGACY_FIELDS = ["date", "match_id", "league", "home", "away", "kickoff_utc", "pick_side", "pick",
                 "line", "odds_if_known", "confidence", "result", "ah_outcome", "units", "settled"]
NEW_FIELDS = ["odds_format", "odds_estimated", "status", "final_line", "final_odds"]
# v3：模型实验盘。旧行默认 track=official / market=ah，model_id/model_version/overlap 为空
TRACK_FIELDS = ["track", "model_id", "model_version", "market", "overlap"]
# DATA SOURCE: 2026-11-08 授权的 CLV/收盘线/真值留痕列。只是 schema 扩列，
# 不改 settle / profit / settle_market 的任何结算、评分、核验或阈值逻辑。
# 口径对齐 src/ah/audit/experiment_track.py：line_move()=line-close_line（正=拿到更好的线），
# clv()=o_bet×p_fair(收盘去水)-1（仅收盘盘口=下注盘口时），缺数据写「未取得」，不另立口径。
# 收盘相关列名**复用** src/ah/audit/experiment_track.py 既有 EXTRA_FIELDS，
# 不另起一套名字；line_move / clv 的计算口径也直接复用该模块的 line_move() / clv()。
CLV_FIELDS = ["line_open", "price_ts", "open_to_report_move", "close_line", "close_odds",
              "close_odds_opp", "close_fair_p", "close_captured_at", "line_move", "clv",
              "clv_basis", "truth_source"]
LEDGER_FIELDS = LEGACY_FIELDS + NEW_FIELDS + TRACK_FIELDS + CLV_FIELDS
TRACKS = ("official", "experiment")
MARKETS = ("ah", "1x2", "ou")
SIDES = {"ah": ("home", "away"), "ou": ("over", "under"), "1x2": ("home", "draw", "away")}
STATUSES = ("active", "void", "cancelled")
ODDS_FORMATS = ("hk", "decimal")
ESTIMATED_HK_ODDS = 0.9
HK_WARN_MIN = 1.5        # 港盘水位 >= 1.5 极可能是误填的欧赔
DECIMAL_WARN_MAX = 1.2   # 欧赔 < 1.2 极可能是误填的港盘水位
CONFIDENCES = ("high", "medium", "low")


def norm_odds_format(v) -> str:
    """'hk' / 'HK (港盘水位…)' / '港盘' -> hk；'decimal' / 'eu' / '欧赔' -> decimal；空 -> hk（旧数据默认）。"""
    t = str(v or "").strip().lower()
    if not t or t.startswith("hk") or "港" in t or t.startswith("hong"):
        return "hk"
    if t.startswith(("dec", "eu")) or "欧" in t or "十进制" in t:
        return "decimal"
    raise ValueError(f"未知 odds_format {v!r}，应为 hk 或 decimal")


def check_odds(odds, odds_format: str = "hk", context: str = "") -> list[str]:
    """赔率范围检查：hk 水位 >=1.5（或 <=0）、decimal <1.2 视为疑似格式错误。
    返回告警列表（空 = 正常），并以 logging.WARNING 写日志（logger 'ah.settle'）。"""
    if odds in (None, ""):
        return []
    fmt = norm_odds_format(odds_format)
    o = float(odds)
    msgs = []
    if fmt == "hk" and (o >= HK_WARN_MIN or o <= 0):
        msgs.append(f"港盘水位 {o:g} 超出正常范围 (0, {HK_WARN_MIN})，疑似误填欧赔；请核对 odds_format")
    if fmt == "decimal" and o < DECIMAL_WARN_MAX:
        msgs.append(f"欧赔 {o:g} < {DECIMAL_WARN_MAX}，疑似误填港盘水位；请核对 odds_format")
    for m in msgs:
        log.warning("%s%s", f"[{context}] " if context else "", m)
    return msgs


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("true", "1", "yes", "y")


def norm_market(v) -> str:
    """ah / 1x2 / ou；空 -> ah（旧数据）。兼容 'AH'、'1X2'、'OU'、'totals'、'h2h' 等写法。"""
    t = str(v or "").strip().lower()
    t = {"": "ah", "asian_handicap": "ah", "spreads": "ah", "h2h": "1x2", "1x2": "1x2", "x12": "1x2",
         "totals": "ou", "o/u": "ou", "over_under": "ou"}.get(t, t)
    if t not in MARKETS:
        raise ValueError(f"未知 market {v!r}，应为 ah / 1x2 / ou")
    return t


def is_official(row: dict) -> bool:
    """正式推荐（计入正式命中率）。缺 track 列的旧行视为 official。"""
    return str(row.get("track") or "official").strip().lower() == "official"


def row_status(row: dict) -> str:
    """status 列；旧数据（无 status 列）中 confidence=cancelled 视为 cancelled，其余 active。"""
    st = str(row.get("status") or "").strip().lower()
    if st in STATUSES:
        return st
    return "cancelled" if str(row.get("confidence") or "").strip().lower() == "cancelled" else "active"


def row_confidence(row: dict) -> str:
    """原始信心等级；旧数据 confidence=cancelled 时无法得知原信心，返回 ''。"""
    c = str(row.get("confidence") or "").strip()
    return "" if c.lower() == "cancelled" else c


def normalize_row(row: dict) -> dict:
    """就地补齐新列默认值（不改已有值）：odds_format=hk、odds_estimated=无赔率时 true、
    status 由旧 confidence 推断、final_line/final_odds 为空。返回同一 dict。"""
    for k in LEGACY_FIELDS:
        if row.get(k) is None:
            row[k] = ""
    row["track"] = str(row.get("track") or "").strip().lower() or "official"
    try:
        row["market"] = norm_market(row.get("market"))
    except ValueError:  # 未知玩法原样保留（小写），由实验盘校验拒绝；不在读取时崩溃
        row["market"] = str(row.get("market")).strip().lower()
    for k in ("model_id", "model_version", "overlap"):
        if row.get(k) is None:
            row[k] = ""
    if not str(row.get("odds_format") or "").strip() and row["market"] == "1x2":
        row["odds_format"] = "decimal"  # 1X2 通常是欧赔
    row["odds_format"] = norm_odds_format(row.get("odds_format"))
    if str(row.get("odds_estimated") or "").strip() == "":
        row["odds_estimated"] = "false" if row.get("odds_if_known") not in (None, "") else "true"
    else:
        row["odds_estimated"] = "true" if _truthy(row["odds_estimated"]) else "false"
    row["status"] = row_status(row)
    for k in ("final_line", "final_odds"):
        if row.get(k) is None:
            row[k] = ""
    return row


def read_ledger(path: Path) -> list[dict]:
    """读取 predictions.csv（v1/v2 均可），返回已 normalize 的行。"""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8") as f:
        return [normalize_row(r) for r in csv.DictReader(f)]


def ledger_fieldnames(rows: list[dict], existing: list[str] | None = None) -> list[str]:
    """标准列在前（LEDGER_FIELDS 顺序），其余未知列按出现顺序保留在后。"""
    extra = []
    for k in list(existing or []) + [k for r in rows for k in r]:
        if k not in LEDGER_FIELDS and k not in extra and k is not None:
            extra.append(k)
    return LEDGER_FIELDS + extra


def write_ledger(path: Path, rows: list[dict]) -> None:
    """按 v2 表头写回（自动补齐新列默认值，不丢未知列）。rows 为空时不写。"""
    if not rows:
        return
    rows = [normalize_row(r) for r in rows]
    with Path(path).open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ledger_fieldnames(rows))
        w.writeheader()
        w.writerows(rows)


def new_ledger_row(**kw) -> dict:
    """新推荐行：未给出的列取默认值。"""
    return normalize_row({k: "" for k in LEDGER_FIELDS} | {"settled": "false"} | {k: v for k, v in kw.items()})


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


def settle_ou(total_goals: int, line: float, side: str) -> str:
    """大小球：side=over/under；整数线可走盘，半球线只有输赢，四分之一线拆两半（赢半/输半）。
    over 等价于 total + (-line) 的让球结算；under 等价于 -total + line。"""
    if side == "over":
        return settle(total_goals, 0, -float(line))
    if side == "under":
        return settle(0, total_goals, float(line))
    raise ValueError(f"大小球 side 必须是 over/under，收到 {side!r}")


def settle_1x2(home_goals: int, away_goals: int, side: str) -> str:
    """胜平负：side=home/draw/away；只有 win / loss。"""
    if side not in SIDES["1x2"]:
        raise ValueError(f"1x2 side 必须是 home/draw/away，收到 {side!r}")
    res = "home" if home_goals > away_goals else "away" if home_goals < away_goals else "draw"
    return "win" if res == side else "loss"


def settle_market(market: str, home_goals: int, away_goals: int, side: str, line=None) -> str:
    """按玩法结算（比分为 90 分钟主-客）。ah 的 line 为所选一方视角；ou 的 line 为总进球线；1x2 无 line。"""
    m = norm_market(market)
    if m == "ah":
        if side not in SIDES["ah"]:
            raise ValueError(f"亚盘 side 必须是 home/away，收到 {side!r}")
        pg, og = (home_goals, away_goals) if side == "home" else (away_goals, home_goals)
        return settle(pg, og, float(line))
    if m == "ou":
        return settle_ou(home_goals + away_goals, float(line), side)
    return settle_1x2(home_goals, away_goals, side)


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
    """对 predictions.csv 的一行结算（需要 result 为 '主-客' 比分）。
    status=cancelled/void 的行直接作废（void, 0 单位）。按 odds_format 计算盈亏（hk 净赔 / decimal 欧赔），
    无赔率按 0.9 水位估算并写 odds_estimated=true。旧格式行（缺新列）同样可用。"""
    if str(row.get("settled", "")).lower() == "true":
        return row
    if row_status(row) in ("cancelled", "void"):
        row.update({"ah_outcome": "void", "units": "0", "settled": "true"})
        return row
    if not row.get("result"):
        return row
    hg, ag = parse_score(row["result"])
    market = norm_market(row.get("market"))
    out = settle_market(market, hg, ag, row["pick_side"], row.get("line") if market != "1x2" else None)
    fmt = norm_odds_format(row.get("odds_format")) if str(row.get("odds_format") or "").strip() or market != "1x2" \
        else "decimal"
    odds = row.get("odds_if_known")
    if odds not in (None, ""):
        check_odds(odds, fmt, context=str(row.get("match_id", "")))
        row["units"] = str(profit(out, float(odds), odds_format=fmt))
        row["odds_estimated"] = "false"
    elif not is_official(row):
        row["units"] = ""  # 实验盘不估算赔率（数据缺失不补值），统计时拒绝计入
        row["odds_estimated"] = "false"
    else:
        row["units"] = str(profit(out, None))
        row["odds_estimated"] = "true"
    row["ah_outcome"] = out
    row["settled"] = "true"
    return row


def settle_csv(path: Path) -> int:
    rows = read_ledger(Path(path))
    if not rows:
        return 0
    n = 0
    for r in rows:
        before = r.get("settled")
        settle_row(r)
        n += before != r.get("settled")
    write_ledger(Path(path), rows)
    return n


def _fd_code(league: str) -> str | None:
    from .consensus import league_id
    from .football_data import CODE_BY_LEAGUE
    try:
        return CODE_BY_LEAGUE.get(league_id(league or "") or "")
    except Exception:  # noqa: BLE001
        _degrade_fact("settle", "fd_score_second_source", None)
        return None


def _fd_score(fd_client, r) -> tuple[int, int] | None:
    """football-data.org 第二比分源（仅欧洲联赛）；不可用时 None。"""
    code = _fd_code(r.get("league", ""))
    if not code or fd_client is None or not fd_client.enabled:
        return None
    from .football_data import score_for
    try:
        return score_for(fd_client, code, r["home"], r["away"], r["kickoff_utc"])
    except Exception:  # noqa: BLE001
        _degrade_fact("settle", "api_football_score", None)
        return None


def autofill_results(path: Path, client=None, now=None, min_after_kickoff_min: int = 110,
                     fd_client=None) -> list[dict]:
    """用 API-Football 自动回填 predictions.csv 中已开赛 >=110 分钟且未填比分的 result。
    match_id 为 apif-<id> 时直接用；否则按日期取赛程，开球±15分钟 + 队名模糊匹配找到 fixture。
    亚盘按 90 分钟（含补时）比分结算：使用 score.fulltime，加时/点球不计。
    返回日志列表 [{match_id, action, detail}]，不抛异常（单场失败不影响其他场）。"""
    from datetime import datetime, timedelta, timezone

    from .api_football import ApiFootballError, Client
    from .match import find_fixture, parse_dt

    rows = read_ledger(Path(path))
    if not rows:
        return []
    c = client or Client()
    now = now or datetime.now(timezone.utc)
    log, changed = [], False
    for r in rows:
        if r.get("result") or str(r.get("settled", "")).lower() == "true":
            continue
        try:
            ko = parse_dt(r["kickoff_utc"])
        except (KeyError, ValueError):
            log.append({"match_id": r.get("match_id"), "action": "skip", "detail": "kickoff_utc 无法解析"})
            continue
        if now - ko < timedelta(minutes=min_after_kickoff_min):
            continue
        try:
            mid = r["match_id"]
            if mid.startswith("apif-"):
                fid = int(mid.split("-", 1)[1])
            else:
                fx = find_fixture(c.fixtures_by_date(ko.strftime("%Y-%m-%d")), r["home"], r["away"], ko)
                if not fx:
                    fs = _fd_score(fd_client, r)
                    if fs:
                        r["result"] = f"{fs[0]}-{fs[1]}"
                        changed = True
                        log.append({"match_id": mid, "action": "filled", "detail": f"football-data.org {r['result']}"})
                        continue
                    log.append({"match_id": mid, "action": "unmatched",
                                "detail": "API-Football 中未找到对应比赛，请补 config/team_aliases.yaml 或手动填比分"})
                    continue
                fid = fx["fixture"]["id"]
            res = c.fixture_result(fid)
        except ApiFootballError as e:
            fs = _fd_score(fd_client, r)
            if fs:
                r['result'] = f'{fs[0]}-{fs[1]}'
                changed = True
                log.append({'match_id': r.get('match_id'), 'action': 'filled',
                            'detail': f"football-data.org {r['result']}（API-Football 不可用）"})
                continue
            log.append({"match_id": r.get("match_id"), "action": "error", "detail": str(e)})
            continue
        if not res or not res["finished"] or res["home_goals"] is None:
            log.append({"match_id": r["match_id"], "action": "pending",
                        "detail": f"fixture {fid} 状态 {res and res['status']}"})
            continue
        fs = _fd_score(fd_client, r)
        if fs and fs != (res["home_goals"], res["away_goals"]):
            log.append({"match_id": r["match_id"], "action": "mismatch",
                        "detail": f"API-Football {res['home_goals']}-{res['away_goals']} ≠ football-data.org "
                                  f"{fs[0]}-{fs[1]}，暂不结算，请人工核对"})
            continue
        r["result"] = f"{res['home_goals']}-{res['away_goals']}"
        changed = True
        log.append({"match_id": r["match_id"], "action": "filled",
                    "detail": f"fixture {fid} {res['home']} {r['result']} {res['away']} ({res['status']})"
                              + ("，football-data.org 一致" if fs else "")})
    if changed:
        write_ledger(Path(path), rows)
    return log


def main() -> None:
    import os

    from . import DATA
    ap = argparse.ArgumentParser(description="（可选）用 API-Football 回填比分，然后结算 predictions.csv")
    ap.add_argument("--csv", default=str(DATA / "predictions.csv"))
    ap.add_argument("--no-autofill", action="store_true", help="不调用 API-Football，只结算已手动填写比分的行")
    a = ap.parse_args()
    if not a.no_autofill and (os.environ.get("API_FOOTBALL_KEY") or os.environ.get("FOOTBALL_DATA_KEY")):
        from .football_data import Client as FDClient
        for e in autofill_results(Path(a.csv), fd_client=FDClient()):  # 欧洲比赛用 football-data.org 交叉核对
            print(f"[autofill] {e['match_id']}: {e['action']} {e['detail']}")
    elif not a.no_autofill:
        print("[autofill] 未设置比分数据源密钥，跳过自动回填比分")
    print(f"settled {settle_csv(Path(a.csv))} rows")
    from .records import sync_from_csv
    for x in sync_from_csv(Path(a.csv)):
        print(f"[records] 结果已写入报告 {x}")


if __name__ == "__main__":
    main()
