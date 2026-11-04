"""只读：数据官的盘口快照 CSV（schema 见 ah-data 仓库 docs/odds_snapshots.md）。

- 新路径 data/snapshots/odds/<报告日期>.csv，旧路径 data/odds_snapshots/<报告日期>.csv，两者都接受（新路径优先）。
- 也可从某个 git 提交读取（git show <rev>:<path>，只读，不改工作区）。
- ah_home_line：主队视角，负数 = 主队让球。odds_format 固定 decimal。
- 只返回解析后的记录；不估算、不补数。missing 行（bookmaker 为空）被丢弃。
"""
from __future__ import annotations

import csv
import io
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

NEW_DIR = Path("data/snapshots/odds")
OLD_DIR = Path("data/odds_snapshots")
REQUIRED = ("match_id", "captured_at", "stage", "bookmaker", "ah_home_line", "odds_home", "odds_away", "source")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass(frozen=True)
class SnapRow:
    match_id: str
    captured_at: datetime
    stage: str
    bookmaker: str
    ah_home_line: float
    odds_home: float
    odds_away: float
    source: str
    verification: str
    is_main: bool
    price_ts: datetime | None
    kickoff_utc: datetime | None
    home: str
    away: str
    league: str
    report_date: str


def find_snapshot_file(root: Path, report_date: str) -> Path | None:
    """root = 数据官仓库根目录；新路径优先，旧路径兼容。"""
    for d in (NEW_DIR, OLD_DIR):
        p = Path(root) / d / f"{report_date}.csv"
        if p.exists():
            return p
    return None


def read_text_from_git(repo: Path, rev: str, report_date: str) -> tuple[str, str]:
    """git show rev:<新路径或旧路径>，返回 (文本, 实际路径)。只读。"""
    for d in (NEW_DIR, OLD_DIR):
        rel = f"{d.as_posix()}/{report_date}.csv"
        r = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{rel}"], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout, f"{repo}@{rev}:{rel}"
    raise FileNotFoundError(f"{repo}@{rev} 中没有 {report_date}.csv（新旧路径都没有）")


def parse_rows(text: str) -> list[SnapRow]:
    rd = csv.DictReader(io.StringIO(text))
    missing = [c for c in REQUIRED if c not in (rd.fieldnames or [])]
    if missing:
        raise ValueError(f"快照缺少必备字段: {missing}")
    out = []
    for r in rd:
        if not r.get("bookmaker") or not r.get("ah_home_line") or not r.get("odds_home") or not r.get("odds_away"):
            continue  # missing 行
        if (r.get("odds_format") or "decimal") != "decimal":
            raise ValueError(f"未知赔率格式 {r.get('odds_format')}")
        out.append(SnapRow(
            match_id=r["match_id"], captured_at=parse_ts(r["captured_at"]), stage=r["stage"],
            bookmaker=r["bookmaker"].lower(), ah_home_line=float(r["ah_home_line"]),
            odds_home=float(r["odds_home"]), odds_away=float(r["odds_away"]), source=r["source"],
            verification=r.get("verification", ""), is_main=r.get("is_main") == "1",
            price_ts=parse_ts(r.get("price_ts")), kickoff_utc=parse_ts(r.get("kickoff_utc")),
            home=r.get("home", ""), away=r.get("away", ""), league=r.get("league", ""),
            report_date=r.get("report_date", "")))
    return out


def load(path: Path) -> list[SnapRow]:
    return parse_rows(Path(path).read_text(encoding="utf-8"))


SOURCE_PREF = ("oddspapi", "api_football", "odds_api")


def pick_price(rows: list[SnapRow], match_id: str, home_line: float, *, bookmaker: str = "pinnacle",
               stage: str = "report", before: datetime | None = None) -> SnapRow | None:
    """某场、某公司、某时点、某条主队视角盘口的价格：只取 captured_at（及 price_ts）早于 before 的记录；
    多来源时按 SOURCE_PREF 优先，同来源取最晚 captured_at。没有就返回 None（不估算）。"""
    c = [r for r in rows if r.match_id == match_id and r.bookmaker == bookmaker and r.stage == stage
         and abs(r.ah_home_line - home_line) < 1e-9
         and (before is None or (r.captured_at < before and (r.price_ts is None or r.price_ts < before)))]
    if not c:
        return None
    rank = {s: i for i, s in enumerate(SOURCE_PREF)}
    return sorted(c, key=lambda r: (rank.get(r.source, 99), -r.captured_at.timestamp()))[0]


def margin(odds_home: float, odds_away: float) -> float:
    """两边 overround：1/oh + 1/oa − 1。"""
    return 1.0 / odds_home + 1.0 / odds_away - 1.0
