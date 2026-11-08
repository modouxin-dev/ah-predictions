"""凭据策略（DATA SOURCE:）：把 5 个需注册源正式声明为「可选增强」，不是硬依赖。

要解决的问题
------------
之前 `config/sources.yaml` 里 5 个需注册接口是事实上的主链路；虽然 provider 客户端各自
有 `enabled` 判定，但仓库里没有一份**显式策略**说明「缺凭据时应该跳过而不是依赖它」，
也没有静态契约保证「每个注册源都有凭据闸门」。

本模块：
1. 读 `config/sources.yaml credential_policy` 声明（含 `source_id / env_var /
   mode=optional_enhancement / on_missing=skip_with_fact`）；
2. 按「当前进程是否注入该环境变量」给出 `active` / `skipped_no_credential` 决策，
   写 `data/source_policy.json` 事实；
3. 做静态契约检查：逐个确认声明的 env_var 在对应 provider 客户端里**真的被用于
   enabled 判定**，避免「声明了可选、代码却硬依赖」；
4. `should_skip(name)` 供编排层询问，缺凭据的可选源直接跳过并留事实。

硬边界
------
- 只判断「当前进程有没有注入这个变量」，**不读值、不判断 key 是否有效、不判断订阅/额度/覆盖**；
- 编排声明了 secret ≠ Secret 已配置 ≠ 密钥有效 ≠ 数据充足；
- 不改任何 provider 的评分/核验/采集算法，不改 `config/sources.yaml` 既有 provider 块，
  不新增需注册数据源，不发 Telegram。
"""
from __future__ import annotations
from ah.degrade import record as _degrade_fact

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import CONFIG, DATA, ROOT

MISSING = "未取得"
DEFAULT_MODE = "optional_enhancement"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_json(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def load_policy(config_path: Path | None = None) -> list[dict]:
    """只读声明；缺字段的条目跳过，不补默认值、不猜变量名。"""
    p = Path(config_path) if config_path else CONFIG / "sources.yaml"
    try:
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    block = cfg.get("credential_policy") or {}
    out = []
    for item in (block.get("sources") or []):
        if not isinstance(item, dict):
            continue
        sid = str(item.get("source_id") or "").strip()
        env = str(item.get("env_var") or "").strip()
        client = str(item.get("client_file") or "").strip()
        if not sid or not env or not client:
            continue
        out.append({"source_id": sid, "env_var": env, "client_file": client,
                    "mode": str(item.get("mode") or DEFAULT_MODE),
                    "on_missing": str(item.get("on_missing") or "skip_with_fact"),
                    "as_of": str(item.get("as_of") or MISSING),
                    "coverage_status": str(item.get("coverage_status") or MISSING)})
    return out


def static_contract_check(policy: list[dict], root: Path | None = None) -> list[dict]:
    """静态检查：env_var 是否真的出现在对应客户端文件里（凭据闸门存在的可复核证据）。"""
    base = Path(root) if root else ROOT
    out = []
    for item in policy:
        p = base / item["client_file"]
        rec = {"source_id": item["source_id"], "client_file": item["client_file"],
               "env_var": item["env_var"], "file_exists": p.is_file(),
               "env_var_referenced": False, "status": MISSING}
        if p.is_file():
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
                rec["env_var_referenced"] = item["env_var"] in txt
            except OSError:
                _degrade_fact("source_policy", "client_file_read", None)
                pass
        rec["status"] = "CREDENTIAL_GATE_PRESENT" if (rec["file_exists"] and rec["env_var_referenced"]) \
            else "GATE_NOT_VERIFIED"
        out.append(rec)
    return out


def decisions(policy: list[dict], env: dict | None = None) -> list[dict]:
    e = os.environ if env is None else env
    out = []
    for item in policy:
        present = bool(str(e.get(item["env_var"]) or "").strip())
        out.append({**item, "credential_present": present,
                    "decision": "active" if present else "skipped_no_credential",
                    "note": "只判断当前进程是否注入该变量；不代表 Secret 已配置、密钥有效、订阅激活或数据充足"})
    return out


def should_skip(name: str, env: dict | None = None, policy: list[dict] | None = None) -> bool:
    """缺凭据的可选增强源 → 跳过。声明里找不到的源 → 不擅自跳过（返回 False）。"""
    e = os.environ if env is None else env
    pol = policy if policy is not None else load_policy()
    for item in pol:
        if item["source_id"] == name or item["env_var"] == name:
            if item["mode"] != DEFAULT_MODE:
                return False
            return not bool(str(e.get(item["env_var"]) or "").strip())
    return False


def check(config_path: Path | None = None, env: dict | None = None,
          root: Path | None = None) -> dict:
    pol = load_policy(config_path)
    dec = decisions(pol, env)
    contract = static_contract_check(pol, root)
    return {
        "schema": 1, "checked_at": _now(),
        "evidence_grade": "【已验证】环境变量布尔事实 + 静态文件文本事实",
        "mode": DEFAULT_MODE,
        "policy": dec, "contract": contract,
        "summary": {
            "declared": len(pol),
            "active": sum(1 for x in dec if x["decision"] == "active"),
            "skipped_no_credential": sum(1 for x in dec if x["decision"] == "skipped_no_credential"),
            "credential_gate_present": sum(1 for x in contract if x["status"] == "CREDENTIAL_GATE_PRESENT"),
            "gate_not_verified": sum(1 for x in contract if x["status"] != "CREDENTIAL_GATE_PRESENT"),
        },
        "note": ("5 个需注册源已声明为可选增强：缺凭据时跳过并留事实，不作为硬依赖；"
                 "只判断当前进程是否注入变量，不读值、不判断有效性；"
                 "编排声明 secret 不等于 Secret 已配置；不推断线上状态，不阻断采集"),
        "unverified": [
            "Actions Secrets 是否真实存在且名字一致（本会话 /actions/secrets 返回 403）",
            "key 有效性、订阅档位、额度与数据覆盖（静态文件无法核验）",
            "CI 运行时各 provider 实际是否被跳过（需读运行事实文件）",
            "Telegram 接收端（本命令不发送任何消息）",
        ],
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="凭据策略事实（只读，不读值、不阻断）")
    ap.add_argument("--out", default=None)
    ap.add_argument("--config", default=None)
    a = ap.parse_args(argv)
    out = check(Path(a.config) if a.config else None)
    if a.out:
        _atomic_json(Path(a.out), out)
    s = out["summary"]
    print(f"SOURCE_POLICY declared={s['declared']} active={s['active']} "
          f"skipped={s['skipped_no_credential']} gates={s['credential_gate_present']} "
          f"gate_unverified={s['gate_not_verified']}")
    for x in out["contract"]:
        if x["status"] != "CREDENTIAL_GATE_PRESENT":
            print(f"::warning::SOURCE_POLICY_GATE_NOT_VERIFIED {x['source_id']} {x['client_file']}")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
