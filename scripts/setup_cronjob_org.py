#!/usr/bin/env python3
"""可选增强：用 cron-job.org REST API 一次性建好 4 个准点任务。

主路径是仓库内的 punctual-dispatch.yml（零注册）；本脚本给已经开了
cron-job.org 账号、想要站点侧失败通知/执行历史的人用，把「点 4 下」压成一条命令。

用法（两个密钥都只从环境变量读，绝不进仓库、绝不回显）：
  CRONJOB_API_KEY=xxx GH_DISPATCH_PAT=yyy python3 scripts/setup_cronjob_org.py
  ... --dry-run          # 只打印脱敏后的 4 份 payload，不发任何请求
  ... --history          # 创建后读一次各 job 的执行历史

REST API 依据 https://docs.cron-job.org/rest-api.html ：
  base https://api.cron-job.org ；Authorization: Bearer <API key>；
  GET /jobs 列现有；PUT /jobs 创建（body {"job": DetailedJob}）；
  GET /jobs/<id>/history 读执行历史；requestMethod 1 = POST。
官方文档未提供「Test run / 立即执行」的 API 端点，执行历史只能等真实班次产生，
这一点如实标注，不假装已测试通过。

硬约束：不生成赛程/盘口/水位/比分/倾向，不写仓库任何报告或台账，
不改评分/阈值/核验/推送；输出不含任何密钥值。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

ENDPOINT = "https://api.cron-job.org"
GH_URL = ("https://api.github.com/repos/modouxin-dev/ah-predictions/"
          "actions/workflows/daily.yml/dispatches")
# 北京时刻与 slot，对齐 docs/external-timer.md
JOBS: list[dict[str, Any]] = [
    {"slot": "1", "hour": 6, "label": "早间版（采集，publish 等到 07:00）"},
    {"slot": "2", "hour": 11, "label": "第 2 次更新"},
    {"slot": "3", "hour": 16, "label": "第 3 次更新"},
    {"slot": "4", "hour": 22, "label": "第 4 次更新"},
]


def job_title(j: dict) -> str:
    return f"ah-predictions slot {j['slot']} (BJ {j['hour']:02d}:00)"


def build_payload(j: dict, pat: str) -> dict:
    return {"job": {
        "title": job_title(j),
        "url": GH_URL,
        "enabled": True,
        "saveResponses": True,          # Execution history 里能看到 204 与时序
        "requestMethod": 1,             # 1 = POST
        "schedule": {"timezone": "Asia/Shanghai", "hours": [j["hour"]], "minutes": [0],
                     "mdays": [-1], "months": [-1], "wdays": [-1]},
        "extendedData": {
            "headers": {
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + pat,
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
            "body": json.dumps({"ref": "main", "inputs": {"slot": j["slot"]}},
                               ensure_ascii=False, separators=(",", ":")),
        },
        # 失败/恢复/自动停用通知；用户贴的官网功能清单明确支持
        "notification": {"onFailure": True, "onFailureCount": 1,
                         "onSuccess": True, "onDisable": True},
    }}


def redact(obj: Any, secrets: list[str]) -> Any:
    """递归脱敏：任何含密钥值的字符串整段替换为 <REDACTED>，绝不截断拼接。"""
    if isinstance(obj, str):
        out = obj
        for s in secrets:
            if s:
                out = out.replace(s, "<REDACTED>")
        return out
    if isinstance(obj, dict):
        return {k: redact(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, secrets) for v in obj]
    return obj


def api(method: str, path: str, key: str, body: dict | None = None) -> tuple[int, Any]:
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json",
               "Accept": "application/json", "User-Agent": "ah-predictions-setup"}
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(ENDPOINT + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode() or ""
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": "HTTP " + str(e.code)}
    except Exception as exc:
        return 0, {"error": type(exc).__name__}


def run(dry_run: bool = False, want_history: bool = False,
        key: str | None = None, pat: str | None = None,
        _api=api) -> dict:
    key = key if key is not None else os.environ.get("CRONJOB_API_KEY", "").strip()
    pat = pat if pat is not None else os.environ.get("GH_DISPATCH_PAT", "").strip()
    secrets = [key, pat]
    out: dict[str, Any] = {
        "mode": "dry_run" if dry_run else "apply",
        "endpoint": ENDPOINT,
        "credentials": {"cronjob_api_key_present": bool(key), "gh_dispatch_pat_present": bool(pat),
                        "note": "只记录是否注入，绝不回显值；PAT 应为仅授该仓库 Actions:write 的新令牌"},
        "jobs": [],
        "planned_payloads": [],
        "note": ("只创建/核对外部定时任务；不生成赛程/盘口/水位/比分/倾向，"
                 "不写仓库报告或台账，不改评分/阈值/核验/推送。"
                 "GitHub dispatch 返回 204 只代表 run 创建请求被接受，送达证据只认台账 message_id。"),
        "unverified": ["Test run 无 REST API 端点（官方文档未列出），需在 Console 手动点一次",
                     "cron-job.org 实际准时率与失败通知是否到达",
                     "Telegram 接收端（本脚本不发送任何消息）"],
    }
    for j in JOBS:
        out["planned_payloads"].append(redact(build_payload(j, pat or "<PAT>"), secrets))

    if dry_run:
        out["jobs"] = [{"title": job_title(j), "action": "not_created_dry_run"} for j in JOBS]
        return out
    if not key or not pat:
        out["error"] = "CRONJOB_API_KEY 或 GH_DISPATCH_PAT 未取得；不猜测、不创建任何任务"
        return out

    status, listing = _api("GET", "/jobs", key)
    out["list_status"] = status
    existing: set[str] = set()
    if status == 200 and isinstance(listing, dict):
        existing = {str(x.get("title")) for x in (listing.get("jobs") or [])}
    out["existing_titles"] = sorted(existing)

    for j in JOBS:
        title = job_title(j)
        if title in existing:
            out["jobs"].append({"title": title, "action": "already_exists_skipped"})
            continue
        s, o = _api("PUT", "/jobs", key, build_payload(j, pat))
        rec: dict[str, Any] = {"title": title, "action": "create", "status": s}
        if isinstance(o, dict):
            jid = (o.get("jobId") or (o.get("job") or {}).get("jobId")
                   or (o.get("jobDetails") or {}).get("jobId"))
            if jid:
                rec["job_id"] = jid
                if want_history:
                    hs, ho = _api("GET", f"/jobs/{jid}/history", key)
                    rec["history_status"] = hs
                    rec["history_count"] = len((ho.get("history") or [])) if isinstance(ho, dict) else 0
        if s not in (200, 201):
            rec["result"] = redact(o, secrets)
        out["jobs"].append(rec)
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="cron-job.org REST API 一键建 4 个准点任务")
    ap.add_argument("--dry-run", action="store_true", help="只打印脱敏 payload，不发请求")
    ap.add_argument("--history", action="store_true", help="创建后读一次执行历史")
    a = ap.parse_args(argv)
    o = run(dry_run=a.dry_run, want_history=a.history)
    print(json.dumps(o, ensure_ascii=False, indent=1))
    if o.get("error"):
        print("[setup-cronjob] " + o["error"], file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
