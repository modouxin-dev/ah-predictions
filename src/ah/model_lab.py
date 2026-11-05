"""模型实验场（track=experiment）：只读 data/experiment_picks.csv 与 data/experiment_stats.json 并做展示用整理。

口径来源：audit/experiment-track（docs/predictions_csv.md v3、docs/experiment_protocol.md）。

- experiment_picks.csv：与 predictions.csv 同一 v3 表头 + 评估列（generated_at, model_p, market_p, close_*）。
  只展示 track=experiment 的行；**缺 track 语义上是 official**，绝不当实验盘。缺 track 或 model_id 的行跳过、
  记 warning（logger ``ah.model_lab``）并计入 ``skipped``；不拿 model_version 顶替 model_id。
- experiment_stats.json：模型 × 玩法统计由审计员脚本（``python -m ah.audit.experiment_track stats``）生成，
  这里**直接读取、绝不重算**。null 一律显示「暂无」。
- 实验盘不写 predictions.csv，也不与 paper_trades.csv 合并；不计入正式命中率。
"""
from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

from . import DATA

log = logging.getLogger("ah.model_lab")

PICKS_FILE = DATA / "experiment_picks.csv"
STATS_FILE = DATA / "experiment_stats.json"
BJ = timezone(timedelta(hours=8))
NONE = "暂无"

MARKET_CN = {"ah": "亚盘", "1x2": "1X2", "ou": "大小球"}
MARKET_ORDER = ("ah", "1x2", "ou")
SIDES = {"ah": ("home", "away"), "ou": ("over", "under"), "1x2": ("home", "draw", "away")}
SIDE_CN = {"ah": {"home": "主队", "away": "客队"}, "1x2": {"home": "主胜", "draw": "平", "away": "客胜"},
           "ou": {"over": "大", "under": "小"}}
OUTCOMES = ("win", "half_win", "push", "half_loss", "loss", "void")
DIR_CN = {"same": "同向", "opposite": "反向"}
# 收盘覆盖率：优先取 stats 已有字段；审计员当前未输出时显示「暂无」（不自行用 clv_n/settled 推算）
COVERAGE_KEYS = ("close_coverage", "clv_coverage", "coverage")
SAMPLE_MARK = 30  # 场数 < 30 标 *


@dataclass
class Lab:
    rows: list[dict] = field(default_factory=list)      # 只含 track=experiment 的有效行
    stats: dict | None = None                           # experiment_stats.json 原样
    skipped: Counter = field(default_factory=Counter)   # 原因 -> 行数
    has_picks_file: bool = False

    @property
    def empty(self) -> bool:
        return not self.rows and not board(self.stats)

    @property
    def sample(self) -> bool:
        s = self.stats or {}
        return bool(s.get("sample")) or "示例" in str(s.get("note") or "") or any(
            "示例" in (r.get("league") or "") or "示例" in (r.get("note") or "") for r in self.rows)


def _num(x) -> float | None:
    try:
        s = str(x).strip()
        return None if s in ("", "None", "null", "nan") else float(s)
    except (TypeError, ValueError):
        return None


def normalize(raw: dict, skipped: Counter | None = None, where: str = "") -> dict | None:
    """一行 → 展示字段；非实验盘或无效行返回 None。缺 track / model_id / 非法 market、pick_side 计入 skipped 并告警。"""
    skipped = skipped if skipped is not None else Counter()
    r = {str(k).strip(): ("" if v is None else str(v).strip()) for k, v in raw.items() if k is not None}
    track = r.get("track", "").lower()
    if not track:
        skipped["missing_track"] += 1
        log.warning("[model_lab] 跳过 %s：缺 track（按 official 处理，不进实验场）", where)
        return None
    if track != "experiment":
        skipped["official"] += 1          # 正式推荐：不属于实验场，正常过滤，不告警
        return None
    if not r.get("model_id"):
        skipped["missing_model_id"] += 1
        log.warning("[model_lab] 跳过 %s：缺 model_id（不用 model_version 顶替）", where)
        return None
    m = (r.get("market") or "ah").lower()   # 文档：market 缺省 ah
    if m not in SIDES:
        skipped["bad_market"] += 1
        log.warning("[model_lab] 跳过 %s：未知 market %r", where, r.get("market"))
        return None
    side = r.get("pick_side", "").lower()
    if side not in SIDES[m]:
        skipped["bad_pick_side"] += 1
        log.warning("[model_lab] 跳过 %s：%s 的 pick_side 非法 %r", where, m, r.get("pick_side"))
        return None
    out = {k: r.get(k, "") for k in ("date", "match_id", "league", "home", "away", "kickoff_utc", "pick", "result",
                                     "status", "model_id", "model_version", "overlap", "generated_at",
                                     "close_captured_at", "note")}
    out.update(track=track, market=m, pick_side=side,
               odds_format="decimal" if r.get("odds_format", "").lower().startswith(("dec", "eu")) else "hk",
               outcome=r.get("ah_outcome", "").lower() if r.get("ah_outcome", "").lower() in OUTCOMES else "",
               settled=r.get("settled", "").lower() == "true",
               kickoff_bj="")
    for k, src in (("line", "line"), ("odds", "odds_if_known"), ("units", "units"), ("model_p", "model_p"),
                   ("market_p", "market_p"), ("close_line", "close_line"), ("close_odds", "close_odds"),
                   ("close_fair_p", "close_fair_p")):
        out[k] = _num(r.get(src))
    # kickoff_bj：有就用；否则从 kickoff_utc 转北京时间（展示用）
    kb = r.get("kickoff_bj", "")
    if kb:
        out["kickoff_bj"] = kb
    elif out["kickoff_utc"]:
        try:
            out["kickoff_bj"] = (datetime.fromisoformat(out["kickoff_utc"].replace("Z", "+00:00"))
                                 .astimezone(BJ).isoformat())
        except ValueError:
            out["kickoff_bj"] = ""
    return out


def load_picks(path: Path | str | None = None) -> tuple[list[dict], Counter, bool]:
    p = Path(path) if path else PICKS_FILE
    skipped: Counter = Counter()
    if not p.exists():
        return [], skipped, False
    with p.open(encoding="utf-8-sig", newline="") as f:
        rows = [normalize(x, skipped, f"{p.name}:{i}") for i, x in enumerate(csv.DictReader(f), start=2)]
    bad = sum(v for k, v in skipped.items() if k != "official")
    if bad:
        log.warning("[model_lab] %s 共跳过 %d 行：%s", p.name, bad, dict(skipped))
    return [r for r in rows if r], skipped, True


def resolve_stats_path(picks: Path | str | None = None, stats: Path | str | None = None) -> Path:
    """显式 --lab-stats 优先；否则同目录 experiment_stats.json / *picks*→*stats*.json；再退回默认。"""
    if stats:
        return Path(stats)
    if picks:
        p = Path(picks)
        for cand in (p.parent / "experiment_stats.json",
                     p.parent / p.name.replace("picks", "stats").replace(".csv", ".json")):
            if cand.exists():
                return cand
    return STATS_FILE


def load_stats(path: Path | str | None = None) -> dict | None:
    p = Path(path) if path else STATS_FILE
    if not p.exists():
        return None
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        log.warning("[model_lab] %s 无法解析：%s", p.name, ex)
        return None
    return s if isinstance(s, dict) else None


def load(picks: Path | str | None = None, stats: Path | str | None = None) -> Lab:
    rows, skipped, has = load_picks(picks)
    return Lab(rows=rows, stats=load_stats(resolve_stats_path(picks, stats)), skipped=skipped, has_picks_file=has)


def as_lab(x: Lab | list | None, stats: dict | None = None) -> Lab:
    """兼容旧调用：list[dict] / None → Lab。"""
    if isinstance(x, Lab):
        return x
    return Lab(rows=list(x or []), stats=stats)


def for_date(rows: list[dict], d: str) -> list[dict]:
    return [r for r in rows if r["date"] == d]


def by_market(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """[(market, rows)]，按 亚盘/1X2/大小球 排序，空组省略。"""
    return [(m, [r for r in rows if r["market"] == m]) for m in MARKET_ORDER if any(r["market"] == m for r in rows)]


# ---------- overlap ----------

def parse_overlap(s: str) -> list[tuple[str, str]]:
    """'B:same;C:opposite;official:same' → [('B','same'),('C','opposite'),('official','same')]；格式不对的片段忽略。"""
    out = []
    for part in str(s or "").split(";"):
        who, _, d = part.strip().partition(":")
        if who.strip() and d.strip().lower() in DIR_CN:
            out.append((who.strip(), d.strip().lower()))
    return out


def overlap_tags(s: str) -> list[str]:
    """['与正式同向', '与 B 同向', '与 C 反向']：正式推荐在前。"""
    xs = parse_overlap(s)
    xs.sort(key=lambda x: (x[0] != "official", x[0]))
    return [f"与正式{DIR_CN[d]}" if w == "official" else f"与 {w} {DIR_CN[d]}" for w, d in xs]


# ---------- 文本 ----------

def short_team(name: str) -> str:
    """「波尔图 Porto」→「波尔图」；纯外文名原样返回。"""
    n = str(name or "").strip()
    head = n.split(" ")[0]
    return head if head and any("\u4e00" <= c <= "\u9fff" for c in head) else n


def side_cn(r: dict) -> str:
    return SIDE_CN[r["market"]][r["pick_side"]]


def selection_text(r: dict) -> str:
    """选项：亚盘=球队名（主/客）；1X2=主胜/平/客胜；大小球=大/小。"""
    if r["market"] == "ah":
        team = short_team(r.get(r["pick_side"], "")) or short_team(r.get("pick", ""))
        return f"{team}（{side_cn(r)[0]}）" if team else side_cn(r)
    return side_cn(r)


def line_text(r: dict) -> str:
    x = r.get("line")
    if r["market"] == "1x2" or x is None:
        return ""
    if r["market"] == "ou":
        return f"{x:g}"
    return "0" if x == 0 else f"{x:+g}"


def odds_text(r: dict) -> str:
    return "" if r.get("odds") is None else f"{r['odds']:.2f}"


def pct(x: float | None, signed: bool = False, nd: int = 1) -> str:
    if x is None:
        return NONE
    return f"{x:+.{nd}%}" if signed else f"{x:.{nd}%}"


def num(x: float | None, nd: int = 3, signed: bool = False) -> str:
    return NONE if x is None else (f"{x:+.{nd}f}" if signed else f"{x:.{nd}f}")


def model_code(model_id: str) -> str:
    """紧凑代号：「A」→「A」；「A-market」→「A」（前缀 ≤3 个字符时）；否则取前 8 个字符。"""
    mid = str(model_id or "")
    head = mid.split("-")[0].split("_")[0]
    return head if 0 < len(head) <= 3 and head != mid else mid[:8]


def legend(rows: list[dict]) -> str:
    """代号图例：「A 市场模型 · B 进球模型」；只列代号与原名不同的模型，没有就返回空串。"""
    seen: dict[str, str] = {}
    for r in rows:
        c = model_code(r["model_id"])
        if c != r["model_id"] and c not in seen:
            seen[c] = r.get("model_version") or r["model_id"]
    return " · ".join(f"{c} {v}" for c, v in sorted(seen.items()))


def n_label(n: int | None, mark: int = SAMPLE_MARK) -> str:
    """场数；n < mark 时加 *。None → 暂无。"""
    if n is None:
        return NONE
    return f"{n}*" if n < mark else str(n)


def coverage_text(a: dict) -> str:
    """收盘覆盖率：优先 stats 字段；没有 → 暂无（不推算）。"""
    cov = a.get("coverage")
    if cov is None:
        return NONE
    try:
        return f"{float(cov):.0%}"
    except (TypeError, ValueError):
        return NONE


# ---------- stats（直接读取，不重算） ----------

def _rejected_by(stats: dict | None) -> Counter:
    """rejected 的 key 为 date/match_id/model_id/market，按 (model_id, market) 计数。"""
    c: Counter = Counter()
    for x in (stats or {}).get("rejected") or []:
        parts = str((x or {}).get("key", "")).split("/")
        if len(parts) >= 4:
            c[(parts[-2], parts[-1])] += 1
    return c


def rejected_total(stats: dict | None) -> int:
    return len((stats or {}).get("rejected") or [])


def board(stats: dict | None) -> list[dict]:
    """experiment_stats.json 的 by_model_market 展开为行（字段原样），按平均 CLV 降序，CLV 为 null 的排最后。"""
    rej = _rejected_by(stats)
    out = []
    for mid, markets in ((stats or {}).get("by_model_market") or {}).items():
        for m, st in (markets or {}).items():
            if not isinstance(st, dict):
                continue
            cov = next((st[k] for k in COVERAGE_KEYS if st.get(k) is not None), None)
            out.append({**st, "model_id": mid, "market": m, "coverage": cov, "rejected_n": rej.get((mid, m), 0)})
    order = {m: i for i, m in enumerate(MARKET_ORDER)}
    return sorted(out, key=lambda a: (a.get("clv_mean") is None, -(a.get("clv_mean") or 0),
                                      a["model_id"], order.get(a["market"], 9)))


def market_cn(m: str) -> str:
    return MARKET_CN.get(m, m)


def stats_time(stats: dict | None) -> str:
    """updated_at（UTC）→ 北京时间「MM-DD HH:MM」；没有返回空串。"""
    t = (stats or {}).get("updated_at")
    try:
        return datetime.fromisoformat(str(t).replace("Z", "+00:00")).astimezone(BJ).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


def week_range(end: str | None = None) -> tuple[str, str]:
    e = _date.fromisoformat(end) if end else datetime.now(BJ).date()
    return (e - timedelta(days=6)).isoformat(), e.isoformat()


SKIP_CN = {"missing_track": "缺 track", "missing_model_id": "缺 model_id", "bad_market": "market 非法",
           "bad_pick_side": "pick_side 非法"}


def skipped_text(skipped: Counter) -> str:
    """「已跳过 3 行（缺 track 2 · 缺 model_id 1）」；official 行不算跳过；没有返回空串。"""
    xs = [(SKIP_CN[k], v) for k, v in skipped.items() if k in SKIP_CN and v]
    if not xs:
        return ""
    return f"已跳过 {sum(v for _, v in xs)} 行（" + " · ".join(f"{k} {v}" for k, v in xs) + "）"
