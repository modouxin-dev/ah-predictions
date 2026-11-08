"""数据源凭据依赖审计（facts only，旁路层，不改任何 provider / 源开关 / 核验口径）。

目的：降低对「需要注册才能拿 key 的 API」的隐性依赖，做到三件事：

1. 把 `config/sources.yaml` 里的源分成 **免注册（keyless）** 与 **需注册（registered）** 两类，
   每次运行都落盘事实，谁都看得见当前到底靠谁在跑。
2. 需注册源缺 key 时**不静默**：输出 `::warning::` 明确「本次仅依赖免注册源」，
   但 **不阻断采集**（阻断会让补发漏发），也不推断缺 key 的原因。
3. 可选 `--probe-keyless`：对已评估合规的公开 URL 做一次低频轻量探测，
   只记 HTTP 状态/字节数/耗时，**不解析成赛程、盘口、水位或比分**，不写报告。

硬约束（AGENTS.md）：
- 绝不回显密钥值：只记录 env 名与布尔值；错误信息只给异常类型。
- 不改 `config/sources.yaml`、不改任何 provider 客户端、不改核验口径/阈值/评分。
- 不新增任何需要注册的数据源；缺字段写「未取得」，不补造、不推断。
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import CONFIG, DATA

MISSING = "未取得"

# 需注册的源 -> 其 auth 说明里引用的环境变量名。名字来自 sources.yaml，值永不读取输出。
REGISTERED = {
    "api_football": "API_FOOTBALL_KEY",
    "the_odds_api": "ODDS_API_KEY",
    "football_data": "FOOTBALL_DATA_KEY",
    "fivedollar": "FIVEDOLLAR_KEY",
    "oddspapi": "ODDSPAPI_KEY",
}

# 免注册 / keyless 档（来自 sources.yaml 的 infersports + working 公开网页 + 已评估 scrapers）。
# 只做「分级与可见性」，不声称它们已接进采集主链路。
KEYLESS_SOURCES = ("infersports", "titan007_live_fixtures", "titan007_odds",
                   "titan007_1x2_history", "uefa_fixtures", "jleague_acle",
                   "oddsportal", "okooo", "jleague", "afc", "websearch_previews")

# 公开连通探测白名单：只放 sources.yaml 里已评估「低频读取合规、无特殊请求要求」的 URL。
# titan007 需 Referer + ?r= 随机参数，直接 GET 会被断开 -> 不探（避免误报与误伤）。
# oddsportal / okooo 标注 partial -> 不探。spl.com.sa 条款禁止自动化 -> 永不探。
KEYLESS_PROBE_URLS = {
    "uefa_fixtures": "https://www.uefa.com/uefachampionsleague/",
    "jleague_acle": "https://www.jleague.jp/en/acle/game/",
    "jleague": "https://www.jleague.jp",
    "afc": "https://www.the-afc.com/en/club/afc_champions_league_elite.html",
}

PROBE_TIMEOUT_S = 8
PROBE_UA = "ah-predictions/1.0 (internal source health check; low frequency)"

NOTE = (
    "只做凭据依赖分级与合规公开源连通事实：不回显密钥值、不解析任何赛程/盘口/水位/比分、"
    "不写报告、不新增需注册数据源、不改任何 provider 或核验口径；"
    "缺字段写『未取得』，连通失败只记异常类型，不推断原因；缺 key 只告警不阻断。"
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix="." + p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def load_sources(path: Path | None = None) -> dict:
    p = Path(path) if path else CONFIG / "sources.yaml"
    if not p.exists():
        raise RuntimeError(f"sources.yaml 不存在：{p}；拒绝审计不存在的配置")
    d = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(d, dict):
        raise RuntimeError("sources.yaml 顶层不是对象，拒绝继续")
    return d


def registered_rows(cfg: dict, env: dict | None = None) -> list[dict]:
    e = os.environ if env is None else env
    rows = []
    for name, var in REGISTERED.items():
        c = cfg.get(name) if isinstance(cfg.get(name), dict) else {}
        present = bool(str(e.get(var) or "").strip())
        rows.append({
            "source": name,
            "auth_class": "registered",
            "credential_env": var,
            "credential_present": present,
            # 只记布尔，绝不回显值
            "credential_value": "REDACTED" if present else MISSING,
            "base_url": c.get("base_url") or MISSING,
            "enabled": c.get("enabled") if "enabled" in c else MISSING,
            "note": "仅记录凭据是否存在；不代表密钥有效，也不代表数据充足",
        })
    return rows


def keyless_rows(cfg: dict) -> list[dict]:
    working = cfg.get("working") if isinstance(cfg.get("working"), dict) else {}
    scrapers = cfg.get("scrapers") if isinstance(cfg.get("scrapers"), dict) else {}
    inf = cfg.get("infersports") if isinstance(cfg.get("infersports"), dict) else {}
    rows = []
    for name in KEYLESS_SOURCES:
        if name == "infersports":
            src = inf
        elif name in working:
            src = working[name] if isinstance(working[name], dict) else {}
        elif name in scrapers:
            src = scrapers[name] if isinstance(scrapers[name], dict) else {}
        else:
            src = {}
        rows.append({
            "source": name,
            "auth_class": "keyless",
            "url": src.get("url") or src.get("base_url") or src.get("probe_url") or MISSING,
            "use": src.get("use") or src.get("method") or MISSING,
            "status": src.get("status") or ("enabled" if src.get("enabled") is True else MISSING),
            "requires_registration": False,  # 免注册档；infersports 有可选的 key 但不申请也能用
            "note": "免注册档不等于已接进采集主链路；只陈述配置事实",
        })
    return rows


def probe_keyless(fetcher=None, now: datetime | None = None) -> list[dict]:
    """低频轻量探测：只记 HTTP 状态 / 读取字节 / 耗时 / 异常类型，不解析内容。"""
    import requests
    out = []
    for name, url in KEYLESS_PROBE_URLS.items():
        row = {"source": name, "url": url, "checked_at": _now(),
               "http_status": MISSING, "bytes_read": MISSING,
               "elapsed_ms": MISSING, "error_type": None, "note": "连通性事实，不代表数据充足"}
        t0 = datetime.now(timezone.utc)
        try:
            fn = fetcher or (lambda u: requests.get(
                u, timeout=PROBE_TIMEOUT_S, stream=True,
                headers={"User-Agent": PROBE_UA, "Accept": "text/html,*/*"}))
            r = fn(url)
            row["http_status"] = getattr(r, "status_code", MISSING)
            try:
                chunk = next(r.iter_content(2048), b"")
                row["bytes_read"] = len(chunk)
            except Exception:
                row["bytes_read"] = MISSING
            try:
                r.close()
            except Exception:
                pass
            row["note"] = "仅 HTTP 状态与首块字节数；不解析页面内容"
        except Exception as exc:
            # 只暴露异常类型，绝不回显可能含凭据的 URL/响应体
            row["error_type"] = type(exc).__name__
            row["note"] = "连通检查未完成；不推断为站点不可用或密钥无效"
        row["elapsed_ms"] = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        out.append(row)
    return out


def audit(cfg: dict | None = None, env: dict | None = None,
          probes: list[dict] | None = None, now: datetime | None = None) -> dict:
    c = cfg if cfg is not None else load_sources()
    reg = registered_rows(c, env)
    key = keyless_rows(c)
    p = probes if probes is not None else []
    missing = [r["source"] for r in reg if not r["credential_present"]]
    configured = [r["source"] for r in reg if r["credential_present"]]
    probed_ok = [x["source"] for x in p if x.get("http_status") not in (MISSING, None)
                 and isinstance(x.get("http_status"), int) and 200 <= x["http_status"] < 400]

    alerts = []
    if missing:
        alerts.append({
            "code": "REGISTERED_CREDENTIALS_MISSING", "severity": "warning",
            "message": (f"{len(missing)} 个需注册源未配置凭据（{', '.join(missing)}）；"
                        "本次仅依赖免注册源，不推断原因、不阻断采集"),
            "evidence": {"missing": missing, "configured": configured},
        })
    if len(configured) == 0:
        alerts.append({
            "code": "ALL_REGISTERED_UNAVAILABLE", "severity": "info",
            "message": "所有需注册源均无凭据；免注册源清单与上表为本次可依赖范围",
            "evidence": {"keyless_total": len(key)},
        })
    if p:
        failed = [x["source"] for x in p if x.get("http_status") in (MISSING, None)]
        if failed:
            alerts.append({
                "code": "KEYLESS_PROBE_INCOMPLETE", "severity": "info",
                "message": f"{len(failed)} 个公开源连通检查未完成；只记异常类型，不推断站点状态",
                "evidence": {"sources": failed},
            })
    if not alerts:
        alerts.append({"code": "NO_ALERT", "severity": "info",
                       "message": "凭据分级与公开源探测均已记录，无异常", "evidence": {}})

    return {
        "schema": 1,
        "checked_at": _now(),
        "evidence_grade": "【已验证】本地配置与环境变量布尔事实；连通项为【运行时代理自检】",
        "policy": "免注册优先、缺凭据不静默、不阻断、不推断、不改源开关",
        "registered": reg,
        "keyless": key,
        "keyless_probes": p,
        "summary": {
            "registered_total": len(reg),
            "registered_configured": len(configured),
            "registered_missing": len(missing),
            "keyless_total": len(key),
            "keyless_probe_checked": len(p),
            "keyless_probe_reachable": len(probed_ok),
        },
        "alerts": alerts,
        "unverified": [
            "密钥是否有效、订阅是否激活（只记录凭据是否存在）",
            "免注册源是否返回足够赛程/盘口覆盖（连通 ≠ 数据充足）",
            "报告采集实际使用了哪个源（本审计不读运行日志、不推断）",
            "Telegram 接收端（本命令不发送任何消息）",
        ],
        "note": NOTE,
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="数据源凭据依赖审计（免注册优先，只记事实，不阻断）")
    ap.add_argument("--config", default=None, help="sources.yaml 路径")
    ap.add_argument("--json", dest="json_out", default=None, help="事实输出路径")
    ap.add_argument("--probe-keyless", action="store_true",
                    help="低频探测合规公开 URL（不解析内容；失败只记异常类型）")
    a = ap.parse_args(argv)
    try:
        cfg = load_sources(Path(a.config) if a.config else None)
        probes = probe_keyless() if a.probe_keyless else []
        result = audit(cfg, probes=probes)
    except Exception as exc:
        # 审计本身失败也要落一份「失败事实」，避免工作流提交路径缺文件；
        # 只记异常类型，不推断原因，不阻断采集，不补造任何源数据。
        result = {
            "schema": 1,
            "checked_at": _now(),
            "status": "AUDIT_FAILED",
            "error_type": type(exc).__name__,
            "policy": "免注册优先、缺凭据不静默、不阻断、不推断、不改源开关",
            "registered": [], "keyless": [], "keyless_probes": [],
            "summary": {"registered_total": 0, "registered_configured": 0,
                        "registered_missing": 0, "keyless_total": 0,
                        "keyless_probe_checked": 0, "keyless_probe_reachable": 0},
            "alerts": [{"code": "AUDIT_FAILED", "severity": "warning",
                        "message": "凭据依赖审计未完成；只记异常类型，不推断原因、不阻断采集",
                        "evidence": {"error_type": type(exc).__name__}}],
            "unverified": ["本次审计的源分级与凭据事实（未生成）"],
            "note": NOTE,
        }
    if a.json_out:
        _atomic_write(Path(a.json_out), result)
    for x in result["alerts"]:
        if x["severity"] != "info":
            print(f"::{x['severity']}::{x['code']} {x['message']}")
    s = result["summary"]
    print(f"SOURCE_AUDIT registered={s['registered_total']} configured={s['registered_configured']} "
          f"missing={s['registered_missing']} keyless={s['keyless_total']} "
          f"probed={s['keyless_probe_checked']} reachable={s['keyless_probe_reachable']}")
    # 始终 0：缺 key 不该让整轮采集失败
    raise SystemExit(0)


if __name__ == "__main__":
    main()
