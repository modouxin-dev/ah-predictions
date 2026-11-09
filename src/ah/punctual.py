"""仓库内准点调度（零注册、零外部账号、零额外密钥）。

GitHub Actions 自带 `schedule` 实测会迟到 4.7~6.8 小时甚至整班丢失。
本模块由 `punctual-dispatch.yml` 每 5 分钟在 runner 上跑一次：按北京目标时刻
判断该不该用 REST API 触发 `daily.yml` 的 workflow_dispatch，让推送准点。

设计边界（刻意保守，不改任何日期口径）：
- 目标时刻对齐 docs/external-timer.md：北京 06:00 / 11:00 / 16:00 / 22:00。
  slot 1 提前采集，publish 由 daily.yml 自己等到北京 07:00 再推。
- 只在「目标时刻起 60 分钟内」动作。四个目标 +1h 都不跨北京午夜，
  因此 workflow_dispatch 按「当前北京日期」解析出的 AH_DATE 与目标日期天然一致，
  不会把延迟补发归到错误日期（这正是 2026-11-08 事故的坑）。
- 超过窗口的漏发不自动补：仍由 delivery-gaps 只读告警 + 人工按 slot 补发。
  不假装全自愈，也不绕过 daily.yml 的日期一致性保护。

三重防重复：
1) 台账 (date, slot, status=sent, message_ids 非空) 已有记录 -> skip
2) 最近 30 分钟已有 daily.yml 的 run（任何触发方式）-> skip
3) concurrency 组 + cancel-in-progress 防同一次决策并发重入

绝不：生成赛程/盘口/水位/比分/倾向、写报告或台账、改评分阈值、发 Telegram、
回显任何 token。dispatch 只返回 204，送达证据只认台账 message_id。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import DATA, ROOT

# 北京时刻 -> slot，与 docs/external-timer.md 保持一致
TARGETS: dict[str, tuple[int, int]] = {
    "1": (6, 0),
    "2": (11, 0),
    "3": (16, 0),
    "4": (22, 0),
}
WINDOW_MINUTES = 60          # 目标后 60 分钟内有效；+1h 不跨北京午夜
DISPATCH_LOG = DATA / "punctual_dispatch_log.json"
BJ = timedelta(hours=8)
MISSING = "未取得"


def bj_now(now: datetime) -> datetime:
    return now.astimezone(timezone.utc) + BJ


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_ledger(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"deliveries": []}


def delivered(ledger: dict, date: str, slot: str) -> bool:
    return any(
        r.get("date") == date
        and str(r.get("slot", "1")) == str(slot)
        and r.get("status") == "sent"
        and r.get("message_ids")
        for r in ledger.get("deliveries", [])
    )


def _parse_gh(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def load_dispatch_log(path: Path | None = None) -> list[dict]:
    p = path or DISPATCH_LOG
    try:
        o = json.loads(p.read_text(encoding="utf-8"))
        return o.get("dispatches", []) if isinstance(o, dict) else []
    except (OSError, ValueError):
        return []


def already_dispatched(log: list[dict], date: str, slot: str) -> str | None:
    """按 (date,slot) 精确幂等：不挡别的时段，也不被别的时段挡。"""
    for r in log:
        if r.get("date") == date and str(r.get("slot")) == str(slot) and r.get("status") == 204:
            return str(r.get("at_utc") or MISSING)
    return None


def append_dispatch(log: list[dict], date: str, slot: str, status: int,
                    now: datetime, path: Path | None = None) -> list[dict]:
    log = list(log) + [{"date": date, "slot": str(slot), "status": status,
                        "at_utc": _ts(now), "evidence": "POST /dispatches 204 (run 创建请求被接受)"}]
    p = path or DISPATCH_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pd-", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "dispatches": log[-200:],
                       "note": "只记录 punctual 自己发出的 dispatch 请求；不是 Telegram 送达证据，"
                               "送达只认 data/telegram_delivery.json 的 message_id"},
                      f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return log


def decide(now: datetime | None = None, ledger: dict | None = None,
           runs: list[dict] | None = None, ledger_path: Path | None = None,
           dispatch_log: list[dict] | None = None) -> dict:
    """纯决策：不联网、不发请求、不写文件。返回可回溯的事实对象。"""
    now = now or datetime.now(timezone.utc)
    bj = bj_now(now)
    date = bj.strftime("%Y-%m-%d")
    ledger = ledger if ledger is not None else _load_ledger(ledger_path or (DATA / "telegram_delivery.json"))
    runs = runs or []
    dispatch_log = dispatch_log if dispatch_log is not None else load_dispatch_log()

    actions: list[dict] = []
    skips: list[dict] = []
    for slot, (hh, mm) in TARGETS.items():
        target_bj = bj.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta_min = (bj - target_bj).total_seconds() / 60.0
        item = {"slot": slot, "date": date, "target_bj": f"{date} {hh:02d}:{mm:02d}",
                "minutes_from_target": round(delta_min, 1)}
        if delta_min < 0:
            item["reason"] = "before_target"
            skips.append(item)
            continue
        if delta_min > WINDOW_MINUTES:
            item["reason"] = "outside_window"
            skips.append(item)
            continue
        if delivered(ledger, date, slot):
            item["reason"] = "already_delivered"
            skips.append(item)
            continue
        done = already_dispatched(dispatch_log, date, slot)
        if done:
            item["reason"] = "already_dispatched"
            item["dispatched_at_utc"] = done
            skips.append(item)
            continue
        item["reason"] = "dispatch"
        item["body"] = {"ref": "main", "inputs": {"slot": slot}}
        actions.append(item)

    return {
        "schema": 1,
        "checked_at_utc": _ts(now),
        "checked_at_bj": bj.strftime("%Y-%m-%d %H:%M:%S"),
        "window_minutes": WINDOW_MINUTES,
        "idempotency": "台账 (date,slot,sent) + punctual dispatch_log (date,slot,204) + workflow 并发组",
        "repository": os.environ.get("GITHUB_REPOSITORY", MISSING),
        "actions": actions,
        "skips": skips,
        "note": "只做 (date,slot) 幂等决策；只在目标时刻起 60 分钟内动作，"
                "窗口外的漏发不自动补（由 delivery-gaps 只读告警），避免延迟班次归错日期。"
                "不生成赛程/盘口/水位/比分/倾向，不写报告/台账，不改评分/阈值/核验/推送。"
                "204 不代表 Telegram 已送达。",
        "unverified": ["dispatch 是否创建了 run（需查 Actions API）",
                     "Telegram 接收端（本模块不发送任何消息）",
                     "窗口外的漏发（由 delivery-gaps 只读告警，不自动补）"],
    }


# ---------- runner 侧 HTTP（只在 --apply / --verify 时使用） ----------

def _gh(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, object]:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo or not token:
        return 0, {"error": "GITHUB_REPOSITORY 或 token 未取得（不推断原因）"}
    url = "https://api.github.com/repos/" + repo + path
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ah-punctual",
               "Content-Type": "application/json"}
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode() or ""
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": "HTTP " + str(e.code)}
    except Exception as exc:
        return 0, {"error": type(exc).__name__}


def fetch_recent_runs(token: str, per_page: int = 20) -> list[dict]:
    status, obj = _gh("GET", "/actions/workflows/daily.yml/runs?per_page=" + str(per_page), token)
    return (obj.get("workflow_runs") or []) if status == 200 and isinstance(obj, dict) else []


def dispatch(token: str, slot: str) -> tuple[int, object]:
    return _gh("POST", "/actions/workflows/daily.yml/dispatches", token,
               {"ref": "main", "inputs": {"slot": slot}})


def verify_link(token: str, now: datetime | None = None) -> dict:
    """链路自检：真发一次 slot=1 的 dispatch，再查是否新建了 run。

    安全前提：台账当天 slot 1 已送达时，daily.yml 的 publish 会因 (date,chat,slot)
    幂等跳过，不会重复发 Telegram。仍会跑一次采集，这点如实记录。
    """
    now = now or datetime.now(timezone.utc)
    before = fetch_recent_runs(token)
    known = {str(r.get("id")) for r in before}
    status, obj = dispatch(token, "1")
    out = {"dispatch_status": status, "dispatch_result": obj if status != 204 else "204 No Content",
           "new_run_id": MISSING, "checked_at_utc": _ts(now)}
    if status == 204:
        import time
        for _ in range(6):
            time.sleep(10)
            for r in fetch_recent_runs(token):
                rid = str(r.get("id"))
                dt = _parse_gh(r.get("created_at") or "")
                if rid not in known and dt and dt >= now - timedelta(minutes=2):
                    out["new_run_id"] = rid
                    out["new_run_event"] = r.get("event")
                    break
            if out["new_run_id"] != MISSING:
                break
    out["note"] = ("只验证『punctual -> REST API -> daily run 创建』链路；"
                   "不声明 Telegram 已送达。slot 1 当天已送达时 publish 会幂等跳过，"
                   "但 collect 仍会跑一次并消耗一次数据源额度。")
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m ah punctual",
                                 description="仓库内准点调度决策（默认只读 dry-run）")
    ap.add_argument("--apply", action="store_true", help="对决策出的 action 真正调用 dispatch")
    ap.add_argument("--verify", action="store_true", help="链路自检：发一次 slot=1 dispatch 并核对新建 run")
    ap.add_argument("--ledger", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    runs = fetch_recent_runs(token) if token else []
    obj = decide(ledger_path=a.ledger, runs=runs)
    obj["apply"] = bool(a.apply)
    obj["verification"] = MISSING

    if a.verify:
        obj["verification"] = verify_link(token) if token else {"error": "token 未取得"}
    elif a.apply:
        results = []
        log = load_dispatch_log()
        for act in obj["actions"]:
            s, o = dispatch(token, act["slot"]) if token else (0, {"error": "token 未取得"})
            results.append({"slot": act["slot"], "date": act["date"], "status": s,
                            "result": "204 No Content" if s == 204 else o})
            if s == 204:
                log = append_dispatch(log, act["date"], act["slot"], s, datetime.now(timezone.utc))
        obj["dispatch_results"] = results
        obj["dispatch_log_written"] = bool(obj["actions"])
        print(f"[punctual] dispatch {len(results)} 次；204 只代表 run 创建请求被接受，"
              "送达证据只认台账 message_id", file=sys.stderr)

    txt = json.dumps(obj, ensure_ascii=False, indent=1)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(txt + "\n", encoding="utf-8")
    print(txt)
    print(f"\n[punctual] bj={obj['checked_at_bj']} actions={len(obj['actions'])} "
          f"skips={len(obj['skips'])}", file=sys.stderr)


if __name__ == "__main__":
    main()
