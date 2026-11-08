"""发布对账层：以「发送台账」为唯一幂等键，不以报告文件是否存在为准。

职责边界（见 AGENTS.md）：
- 不参与模型评分、盘口核验、预测生成；
- 只负责：目标会话一致性、运行时代理自检、带重试的 sendMessage、送达证据入账。

防断言：只有拿到 Telegram 返回的 message_id 才写 status=sent。
所有无法读取的信息（Secret 明文、Actions 日志、Telegram 端收件记录）一律标注为未验证，不推断。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile

try:
    import fcntl  # Actions runner / Linux；非 POSIX 环境回退为无锁
except ImportError:  # pragma: no cover
    fcntl = None
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from . import CONFIG, DATA
from .render_report import report_paths

LEDGER_PATH = DATA / "telegram_delivery.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _bj_date() -> str:
    """报告日期按北京时间：一天 4 次推送（北京 07/11/16/22 点）共用同一个日期键。

    优先用工作流在启动时按「定时目标时刻」解析出的 AH_DATE。GitHub cron 经常延迟数小时，
    若按实际运行时刻取日期，延迟到北京次日凌晨的 22 点班次会被误判成「次日」并触发整轮重采、凌晨推送。"""
    env = os.environ.get("AH_DATE", "").strip()
    if env:
        try:
            datetime.strptime(env, "%Y-%m-%d")
            return env
        except ValueError:
            raise RuntimeError(f"AH_DATE 格式无效：{env!r}（应为 YYYY-MM-DD）") from None
    # 统一走 timeutil.bj_date()，同时把 datetime.now() 注入进去，保留测试的时钟替换能力
    from .timeutil import bj_date
    return bj_date(datetime.now(timezone.utc))


# cron 表达式(UTC) -> (时段, UTC 小时)。与 .github/workflows/daily.yml 的 schedule 保持一致。
CRON_SLOTS = {"0 22 * * *": ("1", 22), "0 3 * * *": ("2", 3), "0 8 * * *": ("3", 8), "0 14 * * *": ("4", 14)}


def resolve_slot(event: str, schedule: str = "", input_slot: str = "",
                 now: datetime | None = None) -> tuple[str, str, int]:
    """解析 (slot, 报告日期, 延迟秒数)。

    定时触发：以「最近一次不晚于现在的 cron 目标时刻」为准，报告日期取该时刻的北京日期；
    因此延迟到过了北京午夜的班次仍归属原日期，且延迟秒数可审计。
    手动/外部触发：slot 取输入（缺省 manual），日期取当前北京日期。"""
    from datetime import timedelta
    now = now or datetime.now(timezone.utc)
    if event == "schedule":
        if schedule not in CRON_SLOTS:
            raise RuntimeError(f"未知的 cron 表达式，无法解析时段：{schedule!r}")
        slot, hour = CRON_SLOTS[schedule]
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target > now:
            target -= timedelta(days=1)
        return slot, (target + timedelta(hours=8)).strftime("%Y-%m-%d"), int((now - target).total_seconds())
    slot = validate_slot(input_slot, from_dispatch=True)
    return slot, (now + timedelta(hours=8)).strftime("%Y-%m-%d"), 0


def cmd_resolve() -> None:
    slot, date, late = resolve_slot(os.environ.get("GITHUB_EVENT_NAME", ""),
                                    os.environ.get("AH_SCHEDULE", ""), os.environ.get("AH_INPUT_SLOT", ""))
    print(f"AH_SLOT={slot}\nAH_DATE={date}\nAH_LATE_S={late}")
    print(f"[resolve] slot={slot} date_bj={date} late_s={late}", file=sys.stderr)


REFRESH_SLOTS = ("2", "3", "4")


VALID_SLOTS = ("1", "2", "3", "4", "manual")


def validate_slot(slot: str, *, from_dispatch: bool = False) -> str:
    """归一化并校验时段；非法输入立即失败，不静默落到 manual/skip。"""
    s = (slot or "").strip() or ("manual" if from_dispatch else "1")
    if s not in VALID_SLOTS:
        raise RuntimeError(f"非法 slot：{slot!r}（允许 1/2/3/4/manual）")
    return s


def _slot() -> str:
    """推送时段：1=北京 07:00（完整采集）；2/3/4=11/16/22 点（复用当日采集，仅更新未开赛场次）；manual=手动。"""
    return validate_slot(os.environ.get("AH_SLOT", "1"))


def _parse_ko(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def refresh_view(report: dict, now: datetime | None = None) -> dict:
    """刷新推送视图：只保留尚未开球的推荐与赛程；开球时间无法解析的一律不保留（无法证明未开赛）。
    只做过滤，不修改、不新增任何预测内容；返回深拷贝，原报告不变。"""
    import copy
    now = now or datetime.now(timezone.utc)
    out = copy.deepcopy(report)
    for key in ("picks", "watchlist"):
        keep = []
        for x in out.get(key) or []:
            ko = _parse_ko(x.get("kickoff_utc"))
            if ko is not None and ko > now:
                keep.append(x)
        out[key] = keep
    return out


def refresh_note(report: dict, slot: str, now: datetime | None = None) -> str:
    from datetime import timedelta
    now = now or datetime.now(timezone.utc)
    gen = _parse_ko(report.get("generated_at"))
    when = (gen + timedelta(hours=8)).strftime("%H:%M") if gen else "未取得"
    hhmm = (now + timedelta(hours=8)).strftime("%H:%M")
    return (f"🔄 <b>第 {slot} 次更新</b>（北京 {hhmm}）· 沿用今日 {when} 采集的数据，"
            f"仅列尚未开赛场次；盘口可能已变动，以实际为准\n\n")


# ---------------------------------------------------------------- 台账

def load_ledger(path: Path | None = None) -> dict:
    p = path or LEDGER_PATH
    if not p.exists():
        return {"schema": 1, "deliveries": []}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": 1, "deliveries": []}
    if not isinstance(d, dict) or not isinstance(d.get("deliveries"), list):
        return {"schema": 1, "deliveries": []}
    return d


def save_ledger(d: dict, path: Path | None = None) -> None:
    """原子落盘：同目录临时文件 + os.replace，避免并发读到半截 JSON。"""
    p = path or LEDGER_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(d, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix="." + p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def _chat_hash(chat: str) -> str:
    """日志只输出会话指纹，不回显完整 chat_id。"""
    return hashlib.sha256(str(chat).encode("utf-8")).hexdigest()[:12]


def _ledger_lock(p: Path):
    """独立锁文件（不随台账被 os.replace 换 inode），返回已加排他锁的句柄。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = open(p.with_name(p.name + ".lock"), "a+", encoding="utf-8")
    if fcntl is not None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    return fh


def report_fingerprint(report: dict) -> str:
    return hashlib.sha256(
        json.dumps(report, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:16]


DELIVERY_MODES = ("", "normal", "backfill")


def delivery_mode_env() -> str:
    """运行模式白名单：normal=日常；backfill=明确补发（按 slot 幂等，不强制重发）。"""
    m = os.environ.get("AH_DELIVERY_MODE", "").strip()
    if m not in DELIVERY_MODES:
        raise RuntimeError(f"非法 AH_DELIVERY_MODE：{m!r}（允许 normal/backfill）")
    return m


def _force() -> bool:
    """仅显式 manual 时段强制重发。

    - 外部调度以 workflow_dispatch 带具体 slot 触发时走正常 slot 幂等；
    - AH_DELIVERY_MODE=backfill 是推荐的补发入口：明确说明在补发，但仍按
      (date, chat_id, slot) 幂等，不会因「手动」二字把已有记录再推一遍；
    - 非法 mode 立即失败，不静默兜底。
    """
    delivery_mode_env()
    if os.environ.get("AH_DELIVERY_MODE", "").strip() == "backfill":
        return False
    return os.environ.get("AH_SLOT", "1").strip() == "manual"


def delivery_mode(date: str, sha: str, chat: str, path: Path | None = None,
                  picks_count: int = 0, force: bool | None = None, slot: str = "1") -> tuple[str, str]:
    """返回 (mode, reason)。mode: primary=完整日报；refresh=沿用当日采集的更新推送；skip=不发。

    幂等键 = (date, chat_id, slot)：同一时段只发一次。
    - 手动 workflow_dispatch：强制 primary 重发；
    - 当日尚无任何送达记录：不论哪个时段都按 primary 发（早上失败时由后续时段兜底）；
    - 已有送达且本时段（2/3/4）未发：refresh；
    - 已送达的版本是 0 场、本次重采有正式推荐：允许补发升级版（primary）。
    """
    if force is None:
        force = _force()
    if force:
        return "primary", "forced_by_manual_dispatch"
    rows = [r for r in load_ledger(path)["deliveries"]
            if r.get("date") == date and str(r.get("chat_id")) == str(chat)
            and r.get("status") == "sent" and r.get("message_ids")]
    if not rows:
        return "primary", "no_delivery_record"
    # 幂等键 = (date, chat_id, slot)。「0 场升级」只能看**本 slot** 的最后一条记录；
    # 此前取 rows[-1]（当日全部送达记录的末条）会被别时段的 0 场兜底记录触发，
    # 导致 slot=1 明明已发过 7 场又重发一次完整日报。
    same = [r for r in rows if str(r.get("slot", "1")) == str(slot)]
    if same:
        last_picks = same[-1].get("picks_count")
        if last_picks is not None and int(last_picks) == 0 and picks_count > 0 and slot not in REFRESH_SLOTS:
            return "primary", "upgrade_zero_pick_report"
        return "skip", "already_delivered_today"
    if slot in REFRESH_SLOTS:
        return "refresh", "refresh_slot_not_delivered"
    if slot == "1":
        # 早间版（北京 07:00）不因后续时段的兜底/更新记录而被吞掉。
        return "primary", "morning_edition_not_delivered"
    # 本 slot 未送达、当日已有其它时段送达，且 slot 不是 1/2/3/4：
    # 不再用跨时段末条记录判断「0 场升级」，保守跳过，避免未知时段误发。
    return "skip", "already_delivered_today"


def should_send(date: str, sha: str, chat: str, path: Path | None = None,
                picks_count: int = 0, force: bool | None = None, slot: str = "1") -> tuple[bool, str]:
    """兼容接口：是否发送。report_sha 仍记录在台账中用于审计，但不是幂等键。"""
    mode, reason = delivery_mode(date, sha, chat, path, picks_count, force, slot)
    return mode != "skip", reason


def record_sent(date: str, sha: str, chat: str, message_ids: list,
                run_id: str | None = None, report_path: str | None = None,
                picks_count: int = 0, path: Path | None = None, slot: str = "1") -> dict:
    """仅在 message_ids 非空时调用；否则属于伪造送达证据。"""
    if not message_ids:
        raise RuntimeError("拒绝写入空 message_ids 的送达记录")
    p = path or LEDGER_PATH
    fh = _ledger_lock(p)
    try:
        d = load_ledger(p)
        row = {
            "date": date,
            "report_sha": sha,
            "chat_id": str(chat),
            "status": "sent",
            "message_ids": [int(x) for x in message_ids],
            "picks_count": int(picks_count),
            "slot": str(slot),
            "sent_at": _now(),
            "run_id": run_id or None,
            "report_path": report_path,
            "evidence": "Telegram sendMessage HTTP 200 / ok=true / message_id",
        }
        # 只记录事实来源，不推断采集失败原因；字段缺省时保持旧格式兼容。
        src = os.environ.get("AH_REPORT_SOURCE", "").strip()
        cr = os.environ.get("AH_COLLECT_RESULT", "").strip()
        if src:
            row["report_source"] = src
        if cr:
            row["collect_result"] = cr
        d["deliveries"].append(row)
        save_ledger(d, p)
    finally:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
    return row


# ---------------------------------------------------------------- 目标会话

def resolve_chat() -> tuple[str, str, dict]:
    """返回 (chat_id, 来源, 事实)。双轨不一致时报错，绝不静默任选。"""
    env_chat = os.environ.get("TELEGRAM_CHAT_ID") or None
    cfg_chat = None
    try:
        cfg = yaml.safe_load((CONFIG / "settings.yaml").read_text(encoding="utf-8")) or {}
        cfg_chat = str(((cfg.get("telegram") or {}).get("chat_id")) or "") or None
    except (OSError, yaml.YAMLError, AttributeError):
        cfg_chat = None
    if env_chat and cfg_chat and env_chat != cfg_chat:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID 与 config/settings.yaml 的 telegram.chat_id 不一致；"
            "拒绝猜测目标会话，请在仓库 Secrets 或配置中统一其一"
        )
    chat = env_chat or cfg_chat
    if not chat:
        raise RuntimeError("未配置 TELEGRAM_CHAT_ID，且 config/settings.yaml 无 telegram.chat_id")
    source = "env:TELEGRAM_CHAT_ID" if env_chat else "config/settings.yaml"
    facts = {
        "chat_source": source,
        "env_chat_present": bool(env_chat),
        "config_chat_present": bool(cfg_chat),
        "consistent": (env_chat == cfg_chat) if (env_chat and cfg_chat) else None,
    }
    return str(chat), source, facts


def destination_facts(token: str, chat: str) -> dict:
    """运行时代理自检。只陈述 API 返回的事实；不把预检当成已送达。"""
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 未配置")
    base = "https://api.telegram.org/bot" + token + "/"
    out = {"checked_at": _now(), "chat_id": str(chat), "delivery_verified": False}

    def call(method: str, payload: dict | None = None) -> dict:
        try:
            r = requests.post(base + method, json=payload or {}, timeout=20)
        except requests.RequestException as exc:
            # 只暴露异常类型：requests 的原始异常可能含带令牌的 URL
            raise RuntimeError(f"{method} 网络请求失败：{type(exc).__name__}") from None
        out[method] = {"http_status": r.status_code}
        if r.status_code != 200:
            desc = ""
            try:
                desc = str((r.json() or {}).get("description", ""))[:200]
            except ValueError:
                pass
            raise RuntimeError(f"{method} HTTP {r.status_code}：{desc}")
        return (r.json() or {}).get("result") or {}

    me = call("getMe")
    out["bot_username"] = me.get("username")
    out["bot_id"] = me.get("id")
    ch = call("getChat", {"chat_id": chat})
    out["chat_type"] = ch.get("type")
    out["chat_title_present"] = bool(ch.get("title"))
    if not out.get("bot_id"):
        raise RuntimeError("getMe 未返回 bot id，无法核验发送权限")
    member = call("getChatMember", {"chat_id": chat, "user_id": out["bot_id"]})
    out["member_status"] = member.get("status")
    out["can_send_messages"] = member.get("can_send_messages")
    if (out["member_status"] in ("left", "kicked")
            or (out["member_status"] == "restricted" and not out["can_send_messages"])):
        raise RuntimeError(f"机器人无权在该会话发送：status={out['member_status']}")
    return out


# ---------------------------------------------------------------- CLI

def _report(date: str) -> tuple[Path, dict]:
    p, _ = report_paths(date)
    if not p.exists():
        raise RuntimeError(f"报告不存在：{p}；拒绝发送不存在的报告")
    return p, json.loads(p.read_text(encoding="utf-8"))


def cmd_preflight() -> None:
    chat, source, facts = resolve_chat()
    f = destination_facts(os.environ.get("TELEGRAM_BOT_TOKEN", ""), chat)
    print("PREFLIGHT_CHAT_ID_HASH", _chat_hash(chat))
    print("PREFLIGHT_CHAT_SOURCE", source)
    print("PREFLIGHT_CONFIG_FACTS", json.dumps(facts, ensure_ascii=False))
    print("PREFLIGHT_BOT", "@" + str(f.get("bot_username")))
    print("PREFLIGHT_CHAT_TYPE", f.get("chat_type"))
    print("PREFLIGHT_MEMBER_STATUS", f.get("member_status"))
    print("PREFLIGHT_DELIVERY_VERIFIED", "false")
    print("PREFLIGHT_NOTE 预检只确认机器人可访问会话；送达必须以 sendMessage 返回的 message_id 为准【运行时代理自检】")


def cmd_check() -> None:
    """stdout 只输出一行 GITHUB_OUTPUT 变量；其余信息走 stderr。"""
    date = _bj_date()
    chat, source, facts = resolve_chat()
    p, _ = report_paths(date)
    if not p.exists():
        print("should_send=false", flush=True)
        print(f"[check] 报告不存在，拒绝发送：{p}", file=sys.stderr)
        return
    report = json.loads(p.read_text(encoding="utf-8"))
    sha = report_fingerprint(report)
    from . import policy
    picks_count = len(policy.published(report))
    slot = _slot()
    mode, reason = delivery_mode(date, sha, chat, picks_count=picks_count, slot=slot)
    if mode == "refresh":
        view = refresh_view(report)
        if not policy.published(view):
            mode, reason = "skip", "refresh_no_unstarted_picks"
    ok = mode != "skip"
    print(f"should_send={'true' if ok else 'false'}", flush=True)
    print(f"[check] date={date} slot={slot} mode={mode} report_sha={sha} picks={picks_count} chat_source={source} "
          f"event={os.environ.get('GITHUB_EVENT_NAME', 'local')} "
          f"facts={json.dumps(facts, ensure_ascii=False)} reason={reason}", file=sys.stderr)


def cmd_send() -> None:
    date = _bj_date()
    slot = _slot()
    chat, source, facts = resolve_chat()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 未配置，拒绝静默跳过")
    p, report = _report(date)
    sha = report_fingerprint(report)
    from . import grounding, policy, publish_telegram as pt
    picks_count = len(policy.published(report))
    mode, reason = delivery_mode(date, sha, chat, picks_count=picks_count, slot=slot)
    if mode == "skip":
        print(f"[send] 本时段已送达或无需补发，跳过：{reason} slot={slot}")
        return

    view = report
    if mode == "refresh":
        view = refresh_view(report)
        picks_count = len(policy.published(view))
        if picks_count == 0:
            print(f"[send] 更新时段无尚未开赛的正式推荐，不发送：slot={slot}（不是故障；早间日报已送达）")
            return

    # 发送前 fail-closed 事实闸门（对实际要发送的内容）
    gate = grounding.check_all(view)
    if not gate["ok"]:
        raise RuntimeError("事实闸门未通过，拒绝发送：" + "；".join(gate["errors"][:5]))

    stats_path = DATA / "stats.json"
    stats = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.exists() else None
    messages = pt.daily(view, stats, os.environ.get("SITE_URL") or pt.SITE_URL)
    if mode == "refresh" and messages:
        note = refresh_note(report, slot)
        if len(note) + len(messages[0]) <= 4000:
            messages[0] = note + messages[0]
        else:
            messages.insert(0, note.rstrip())
    ids = pt.send(messages)  # 缺配置 / 失败一律非零退出；返回 message_id 列表
    row = record_sent(date, sha, chat, ids,
                      run_id=os.environ.get("GITHUB_RUN_ID"), report_path=str(p),
                      picks_count=picks_count, slot=slot)
    print(f"DELIVERED date={date} slot={slot} mode={mode} message_ids={row['message_ids']} "
          f"chat_hash={_chat_hash(chat)} chat_source={source} sha={sha} evidence=sendMessage")


def cmd_show() -> None:
    d = load_ledger()
    print(json.dumps(d, ensure_ascii=False, indent=2))
    print("LEDGER_NOTE status=sent 均带 message_ids；无记录不代表未发送，仅代表本台账未观测到【未验证】",
          file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Telegram 发布对账（preflight|check|send|show）")
    ap.add_argument("action", choices=["preflight", "check", "send", "show", "resolve"])
    a = ap.parse_args(argv)
    {"preflight": cmd_preflight, "check": cmd_check, "send": cmd_send,
     "show": cmd_show, "resolve": cmd_resolve}[a.action]()


if __name__ == "__main__":
    main()
