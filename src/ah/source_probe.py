"""数据源运行时代理自检（只陈述可观测事实，不推断、不补全、不断言）。

证据约定（见 AGENTS.md）：
- 打印的每一项均为【运行时代理自检】：由本次进程实际调用远端后记录；
- 只输出「是否存在 / HTTP 状态 / 订阅字段 / 错误类型」，不输出密钥、URL、请求参数；
- 不把「能连通」写成「数据充足」，不把「key 已配置」写成「key 有效」；
- 仓库静态文件无法确认的项一律标【未验证】，由本命令在运行时给出事实。

用法：
    PYTHONPATH=src python -m ah source-probe
    PYTHONPATH=src python -m ah source-probe --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from . import api_football, oddspapi


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def probe_api_football() -> dict:
    """只做 /status（免费、不计额度）+ 开关读取。"""
    out = {
        "source": "api_football",
        "key_env_present": bool(os.environ.get("API_FOOTBALL_KEY")),
        "kill_env_set": bool(os.environ.get(api_football.KILL_ENV)),
        "disabled_flag_present": api_football.DISABLED_FLAG.exists(),
        "kill_switch_reason": api_football.kill_switch_reason(),
        "status_checked": False,
        "http_status": None,
        "subscription_active": None,
        "account_active": None,
        "quota": None,
        "error_type": None,
    }
    if not out["key_env_present"]:
        out["note"] = "环境变量 API_FOOTBALL_KEY 未配置；不推断原因"
        return out
    if out["kill_switch_reason"]:
        out["note"] = "本地开关仍停用；未发起远端请求"
        return out
    try:
        c = api_football.Client(check_yaml=True)
        data = c.status()  # /status 不计额度
        out["status_checked"] = True
        out["http_status"] = 200
        sub = data.get("subscription") or {}
        acc = data.get("account") or {}
        out["subscription_active"] = sub.get("active")
        out["account_active"] = acc.get("active")
        out["quota"] = {
            "requests_limit_day": sub.get("requests_limit_day")
            or sub.get("requests") or None,
            "requests_used": acc.get("requests_used") or None,
        }
        out["note"] = "仅记录 /status 返回字段；不代表盘口/赛程数据充足"
    except Exception as exc:
        # 只暴露异常类型，绝不回显可能含密钥的 URL 或响应体
        out["error_type"] = type(exc).__name__
        out["note"] = "远端 /status 未成功；不推断为密钥无效，也不推断为账号停用"
    return out


def probe_oddspapi() -> dict:
    """只做 /account（不计数）+ 一次非计费连通确认。"""
    out = {
        "source": "oddspapi",
        "key_env_present": bool(os.environ.get("ODDSPAPI_KEY")),
        "account_checked": False,
        "http_status": None,
        "active_subscriptions": None,
        "monthly_remaining_local": None,
        "error_type": None,
    }
    if not out["key_env_present"]:
        out["note"] = "环境变量 ODDSPAPI_KEY 未配置；不推断原因"
        return out
    try:
        c = oddspapi.Client()
        a = c.account()  # /account 不计数
        out["account_checked"] = True
        out["http_status"] = 200
        subs = [s for s in (a.get("subscriptions") or []) if s.get("is_active")]
        out["active_subscriptions"] = len(subs)
        out["monthly_remaining_local"] = c.remaining()
        out["note"] = "仅记录 /account 返回字段；HTTP 404 的赛程端点需由日报采集另行确认"
    except Exception as exc:
        out["error_type"] = type(exc).__name__
        out["note"] = "远端 /account 未成功；不推断为密钥无效"
    return out


def run() -> dict:
    return {
        "probe": "data-source-runtime-selfcheck",
        "checked_at": _ts(),
        "evidence_grade": "【运行时代理自检】",
        "sources": [probe_api_football(), probe_oddspapi()],
        "unverified": [
            "Secrets 明文与仓库 settings.yaml 的一致性（进程只能读到运行时环境变量）",
            "赛程/盘口端点是否返回足够覆盖（/status 与 /account 不等于数据充足）",
            "Telegram 目标群是否收到消息（本命令不发送任何消息）",
        ],
        "assertions": "本输出不包含任何『已恢复 / 已验证 / 已送达 / 已命中』结论",
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="数据源运行时代理自检（只输出事实）")
    ap.add_argument("--json", dest="json_out", help="把结构化事实写到指定路径")
    a = ap.parse_args(argv)
    result = run()
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
            f.write("\n")
    print("=== 数据源运行时代理自检【运行时代理自检】 ===")
    for s in result["sources"]:
        print(f"\n[{s['source']}]")
        for k, v in s.items():
            if k == "source":
                continue
            print(f"  {k}: {v}")
    print("\n【未验证】" + "；".join(result["unverified"]))
    print(result["assertions"])
    # 连不上不算「自检通过」，但只陈述事实，不推断为密钥无效
    failed = [s["source"] for s in result["sources"]
              if s.get("key_env_present") and
              ((s["source"] == "api_football" and not s.get("status_checked"))
               or (s["source"] == "oddspapi" and not s.get("account_checked")))]
    if failed:
        print("SELFCHECK_INCOMPLETE " + ",".join(failed), file=sys.stderr)
        raise SystemExit(1)
    print("SELFCHECK_FACTS_RECORDED")


if __name__ == "__main__":
    main()
