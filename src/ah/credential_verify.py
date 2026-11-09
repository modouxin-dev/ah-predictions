"""凭据换号验证（只读事实，绝不回显任何 key 值）。

用途：用户在 GitHub Actions Secrets 里换了另一个账号的 API_FOOTBALL_KEY 后，
在 runner 侧跑一次，把「/status 是否成功、账号是否 active、赛程端点返回几场」
写成 JSON 提交回 main，从 contents 通道读回（日志/artifact 下载被网络策略拦截）。

硬约束：
- 只记录布尔、HTTP 状态、条数与状态分布；不输出 key/token 明文、不写赛程明细；
- 不写报告 / 台账 / 评分 / 阈值 / 核验口径 / 推送；
- 不写「已恢复 / 已验证 / 已送达 / 已命中」这类断言（沿用 source_probe 口径）；
- 连不上只陈述事实，不推断为密钥无效。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import DATA, ROOT, api_football, fetch_fixtures, source_probe

MISSING = "未取得"


def _stamp(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_status() -> dict:
    """读 runner 本地运行状态快照，只取 api_football 段且剔除任何疑似密钥。"""
    p = Path(os.environ.get("AH_SOURCE_STATUS_FILE") or (ROOT / "var" / "source_status.json"))
    if not p.exists():
        return {"present": False, "note": "运行状态快照未取得（不推断原因）"}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"present": True, "note": "快照解析失败（不推断原因）"}
    a = (d.get("sources") or {}).get("api_football") or {}
    # 只保留已知事实字段，绝不整体 dump（避免把任何错误信息里的敏感串带出去）
    return {
        "present": True,
        "last_ok_at": a.get("last_ok_at") or MISSING,
        "last_call_at": a.get("last_call_at") or MISSING,
        "last_error_at": a.get("last_error_at") or MISSING,
        "last_error": str(a.get("last_error") or "")[:300],
        "quota": a.get("quota") or {},
        "snapshot_updated_at": d.get("updated_at") or MISSING,
        "suspended_error_present": "suspend" in str(a.get("last_error") or "").lower(),
    }


def verify(now: datetime | None = None, days: int = 2) -> dict:
    now = now or datetime.now(timezone.utc)
    raw_key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    injected = bool(raw_key)
    # 不可逆指纹：只用于回答「runner 看到的 Secret 是不是我刚换的那条」，
    # 不回显 key 本身；用户可在本地对自己的新 key 做同样运算比对。
    fingerprint = hashlib.sha256(raw_key.encode()).hexdigest()[:12] if raw_key else MISSING
    kill_env = bool(os.environ.get("AH_API_FOOTBALL_DISABLED", "").strip())
    try:
        kill_reason = api_football.kill_switch_reason()
    except Exception as exc:
        kill_reason = f"未取得（{type(exc).__name__}）"

    # 1) 官方自检：/status（账号 active / 订阅 / 额度），失败也只记事实
    probe = source_probe.run()
    apif = next((s for s in probe.get("sources", []) if s.get("source") == "api_football"), {})

    # 2) 赛程端点：/status 成功不等于赛程数据充足，再探两天，只数条数与状态分布
    fixture_days: dict[str, dict] = {}
    fixture_errors: list[str] = []
    for i in range(days):
        day = (now + timedelta(days=i)).date().isoformat()
        try:
            rows = fetch_fixtures.fetch_api(day)
            fixture_days[day] = {
                "rows": len(rows),
                "status_distribution": dict(Counter(
                    str(r.get("status") or MISSING) for r in rows).most_common()),
            }
        except Exception as exc:
            fixture_days[day] = {"rows": 0, "error_type": type(exc).__name__}
            fixture_errors.append(f"{day}: {type(exc).__name__}")

    # 3) 运行状态快照（probe 与 fixtures 调用都会写 var/source_status.json）
    snap = _safe_status()

    return {
        "schema": 1,
        "checked_at": _stamp(now),
        "evidence_grade": "【运行时代理自检】runner 直连数据源的字段事实",
        "secret": {
            "api_football_key_injected": injected,
            "api_football_key_fingerprint": fingerprint,
            "kill_env_set": kill_env,
            "disabled_flag_present": api_football.DISABLED_FLAG.exists(),
            "kill_switch_reason": kill_reason or MISSING,
            "note": "只记录环境变量是否注入与其 sha256 前 12 位不可逆指纹，绝不回显值；"
                    "指纹用于自行核对『runner 看到的是不是刚换的那条 key』："
                    "在本地对同一条 key 做 echo -n KEY | sha256sum | cut -c1-12 比对即可。"
                    "注入不代表 Secret 有效或账号可用",
        },
        "status_probe": {
            "status_checked": apif.get("status_checked"),
            "http_status": apif.get("http_status"),
            "account_active": apif.get("account_active"),
            "subscription_active": apif.get("subscription_active"),
            "quota": apif.get("quota"),
            "error_type": apif.get("error_type"),
            "note": apif.get("note"),
        },
        "fixture_probe": {
            "days": fixture_days,
            "errors": fixture_errors,
            "note": "只数返回条数与状态分布，不写赛程明细、不生成盘口/水位/比分/倾向；"
                    "条数为 0 也可能是窗口内确实无配置联赛比赛，不推断原因",
        },
        "runtime_status_snapshot": snap,
        "unverified": [
            "Secrets 明文与所换账号的对应关系（进程只能读到注入值，不回显）",
            "免注册源与交叉核验口径是否随之变化（本命令不读配置开关）",
            "Telegram 接收端（本命令不发送任何消息）",
        ],
        "assertions": "本输出不包含任何『已恢复 / 已验证 / 已送达 / 已命中』结论；"
                     "只陈述可回溯字段，判断由读者作出",
    }


def cmd(out: Path | None = None, days: int = 2) -> dict:
    obj = verify(days=days)
    out = Path(out or (DATA / "credential_verify.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cred-", dir=str(out.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    s = obj["status_probe"]
    print(f"[credential-verify] injected={obj['secret']['api_football_key_injected']} "
          f"status_checked={s['status_checked']} account_active={s['account_active']} "
          f"fixture_rows={sum(v.get('rows', 0) for v in obj['fixture_probe']['days'].values())} -> {out}")
    return obj


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m ah credential-verify",
                                 description="换号后的凭据/赛程端点自检（只读事实，不回显 key）")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--days", type=int, default=2)
    a = ap.parse_args(argv)
    cmd(out=a.out, days=a.days)


if __name__ == "__main__":
    main()
