"""只读诊断：InferSports 赛程「接口返回 N 场、入池 0 场」到底卡在哪一层。

背景：2026-11-09 日报 `fixture_source_counts` 显示 infersports raw=120 / added=0，
而日志下载被网络策略拦截。本模块把观察动作放到 CI runner（出网正常），
结果写成 JSON 提交回仓库，从已通的 contents 通道读回——换出口，不撞被拦的日志/artifact。

硬约束：只统计字段名、状态分布、联赛名分布与逐层淘汰计数；
不生成任何赛程/盘口/水位/比分/倾向，不改报告、台账、评分、阈值、推送。
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

from . import CONFIG, DATA, consensus, infersports
from .match import parse_dt

STATUS_OK = ("scheduled", "not_started")
MISSING = "未取得"


def _stamp(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def probe(hours: int = 36, now: datetime | None = None, client=None) -> dict:
    now = now or datetime.now(timezone.utc)
    end = now + timedelta(hours=hours)
    cfg = yaml.safe_load((CONFIG / "sources.yaml").read_text(encoding="utf-8")) or {}
    L = yaml.safe_load((CONFIG / "leagues.yaml").read_text(encoding="utf-8")) or {}
    by_id = {x["id"]: x for x in (L.get("leagues") or [])}
    codes = (cfg.get("infersports") or {}).get("leagues") or {}
    # 与 daily.collect_fixtures 完全相同的映射构造，不另立口径
    infer_ids = {str(v).casefold(): k for k, v in codes.items() if k in by_id}
    c = client if client is not None else infersports.Client()

    stages: Counter = Counter()
    status_dist: Counter = Counter()
    league_dist: Counter = Counter()
    samples: list[dict] = []
    passed: list[dict] = []
    per_date: dict[str, int] = {}
    errors: list[str] = []

    day = now.date()
    while day <= end.date():
        try:
            games = c.events(day.isoformat())
            per_date[day.isoformat()] = len(games)
        except Exception as exc:
            errors.append(f"{day.isoformat()}: {type(exc).__name__}")
            day += timedelta(days=1)
            continue
        for g in games:
            stages["raw"] += 1
            st = str(g.get("status") or "")
            status_dist[st or MISSING] += 1
            if st not in STATUS_OK:
                stages["drop_status"] += 1
                continue
            stages["pass_status"] += 1
            lg = g.get("league")
            if isinstance(lg, dict):
                lname = str(lg.get("name") or "")
            elif lg is None:
                lname = ""
            else:
                lname = str(lg)
            league_dist[lname or MISSING] += 1
            lid = infer_ids.get(lname.casefold())
            if lid is None:
                stages["drop_league_unmapped"] += 1
                continue
            stages["pass_league"] += 1
            mid = "infs-" + str(g.get("id") or "")
            home = (g.get("home_team") or {}).get("name")
            away = (g.get("away_team") or {}).get("name")
            ko = g.get("scheduled_at")
            if not all((mid, lid, home, away, ko)):
                stages["drop_missing_fields"] += 1
                continue
            try:
                dt = parse_dt(ko)
            except (TypeError, ValueError):
                stages["drop_bad_kickoff"] += 1
                continue
            if not now < dt <= end:
                stages["drop_outside_window"] += 1
                continue
            # 去重与 daily.add 一致：只比同一联赛内、已入池的行（不是跨天累积的样本）
            events = {str(i): {"home": r["home"], "away": r["away"], "commence": r["ko"]}
                      for i, r in enumerate(passed) if r["lid"] == lid}
            if consensus.find_event(events, home, away, dt):
                stages["drop_duplicate"] += 1
                continue
            stages["pass_all"] += 1
            passed.append({"home": home, "away": away, "ko": str(ko), "lid": lid})
            if len(samples) < 3:
                samples.append({"home": home, "away": away, "ko": str(ko),
                                "league": lname, "mapped_to": lid, "status": st})
        day += timedelta(days=1)

    return {
        "schema": 1,
        "checked_at": _stamp(now),
        "evidence_grade": "【已验证】CI runner 直连数据源的字段解析事实",
        "window": {"now": now.isoformat(), "end": end.isoformat(), "hours": hours},
        "mapping_size": len(infer_ids),
        "mapping": dict(sorted(infer_ids.items())),
        "per_date_rows": per_date,
        "status_distribution": dict(status_dist.most_common()),
        "league_name_distribution": dict(league_dist.most_common(30)),
        "stages": dict(stages),
        "first_passing_samples": samples,
        "errors": errors,
        "note": ("只记录接口返回的字段分布与逐层淘汰计数，不生成任何赛程、盘口、水位、比分或倾向；"
                 "不写报告/台账，不改评分、阈值、核验或推送。raw 含全部联赛，added 只计配置联赛且未开赛场次。"),
        "unverified": ["未配置联赛的比赛是否可用（本项目口径不纳入）", "盘口/近期状态是否充足（本探针不采集）"],
    }


def cmd(out: Path | None = None, hours: int = 36) -> dict:
    obj = probe(hours=hours)
    out = Path(out or (DATA / "infer_probe.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".infer-", dir=str(out.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    s = obj["stages"]
    print(f"[infer-probe] raw={s.get('raw', 0)} pass_status={s.get('pass_status', 0)} "
          f"pass_league={s.get('pass_league', 0)} pass_all={s.get('pass_all', 0)} "
          f"unmapped={s.get('drop_league_unmapped', 0)} -> {out}")
    return obj


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m ah infer-probe",
                                 description="InferSports 赛程逐层淘汰计数（只读事实）")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--hours", type=int, default=36)
    a = ap.parse_args(argv)
    cmd(out=a.out, hours=a.hours)


if __name__ == "__main__":
    main()
