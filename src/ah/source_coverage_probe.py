"""赛程覆盖诊断（只读事实，绝不生成赛程/盘口/水位/比分/倾向）。

背景：2026-11-09 日报 0 场。独立赛程源证实 11-08/11-09 凌晨有英超/西甲/意甲/
德甲/法甲/卡塔尔联/韩K联/澳超/中超等配置联赛比赛。怀疑 roles.fixtures 声明的
titan007 备用赛程源根本没接进 daily.collect_fixtures，且 InferSports 探针把
联赛分布截断到前 30 名，导致「没比赛」的错误结论。

本模块在 CI runner 侧直连各源，把全量联赛名分布（不截断）、备用源连通性、
按 leagues.yaml 中文名可匹配的配置联赛数写成 JSON 提交回仓库读回。
不改任何采集/推送/评分/阈值/核验口径，不写报告或台账。
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import CONFIG, DATA, fetch_fixtures, football_data, infersports

MISSING = "未取得"


def _stamp(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _configured_names() -> dict[str, str]:
    """中文名/英文名 -> league id，用于判断备用源返回的联赛能否对上配置。"""
    L = yaml.safe_load((CONFIG / "leagues.yaml").read_text(encoding="utf-8")) or {}
    out = {}
    for x in (L.get("leagues") or []):
        for k in (x.get("cn"), x.get("name")):
            if k:
                out[str(k).casefold()] = x["id"]
    return out


def _match_league(name: str, table: dict[str, str]) -> str | None:
    n = str(name or "").strip().casefold()
    if not n:
        return None
    if n in table:
        return table[n]
    # 别名包含匹配（只用于诊断计数，不写回任何映射文件）
    for k, v in table.items():
        if len(k) >= 2 and (k in n or n in k):
            return v
    return None


def probe_titan() -> dict:
    out: dict = {"source": "titan007_live_fixtures", "role": "roles.fixtures 声明的备用赛程源",
                 "declared_in_collect_fixtures": False}
    try:
        rows = fetch_fixtures.fetch()
    except Exception as exc:
        out.update(fetch_ok=False, error_type=type(exc).__name__,
                   note="免注册公开源，失败只记事实不推断")
        return out
    table = _configured_names()
    dist: Counter = Counter()
    matched: Counter = Counter()
    unstarted = 0
    for r in rows:
        lg = str(r.get("league") or "")
        dist[lg or MISSING] += 1
        m = _match_league(lg, table)
        if m:
            matched[m] += 1
        if str(r.get("state") or "") == "0":
            unstarted += 1
    out.update(fetch_ok=True, rows=len(rows), unstarted=unstarted,
               league_name_distribution_all=dict(dist.most_common()),
               configured_league_matches=dict(matched.most_common()),
               sample_rows=[{k: r.get(k) for k in ("match_id", "league", "home", "away", "kickoff_bjt", "state")}
                            for r in rows[:3]])
    return out


def probe_infer(now: datetime, hours: int = 36) -> dict:
    out: dict = {"source": "infersports", "note": "全量联赛名，不截断"}
    c = infersports.Client()
    dist: Counter = Counter()
    status_dist: Counter = Counter()
    per_date: dict[str, int] = {}
    day = now.date()
    end = now + timedelta(hours=hours)
    try:
        while day <= end.date():
            rows = c.events(day.isoformat())
            per_date[day.isoformat()] = len(rows)
            for r in rows:
                st = str(r.get("status") or MISSING)
                status_dist[st] += 1
                if st not in ("scheduled", "not_started"):
                    continue
                lg = r.get("league")
                name = str((lg or {}).get("name") if isinstance(lg, dict) else (lg or "")) or MISSING
                dist[name] += 1
            day += timedelta(days=1)
        out.update(fetch_ok=True, per_date_rows=per_date,
                   status_distribution=dict(status_dist.most_common()),
                   league_name_distribution_all=dict(dist.most_common()),
                   league_name_count=len(dist))
    except Exception as exc:
        out.update(fetch_ok=False, error_type=type(exc).__name__)
    return out


def run(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {
        "schema": 1,
        "checked_at": _stamp(now),
        "window_hours": 36,
        "sources": [probe_titan(), probe_infer(now)],
        "note": ("只记录各源返回的联赛名分布、条数与配置联赛名匹配计数；"
                 "不生成任何赛程/盘口/水位/比分/倾向，不写报告/台账，"
                 "不改评分/阈值/核验/推送，也不改 sources.yaml 映射。"
                 "独立公开赛程源另有 11-08/11-09 配置联赛比赛的事实，不在本文件内推断。"),
        "unverified": ["titan007 联赛名与 leagues.yaml 的精确映射（本探针只做包含匹配计数）",
                     "盘口/近期状态是否充足（本探针不采集）",
                     "Telegram 接收端（本命令不发送任何消息）"],
    }


def cmd(out: Path | None = None) -> dict:
    obj = run()
    out = Path(out or (DATA / "source_coverage_probe.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".scp-", dir=str(out.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    t = next((s for s in obj["sources"] if s["source"] == "titan007_live_fixtures"), {})
    print(f"[source-coverage] titan rows={t.get('rows', MISSING)} "
          f"configured_matches={t.get('configured_league_matches', {})} -> {out}")
    return obj


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m ah source-coverage-probe",
                                 description="赛程源覆盖诊断（只读事实）")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    cmd(out=a.out)


if __name__ == "__main__":
    main()
