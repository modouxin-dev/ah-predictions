"""漏发自愈盘点（facts only，零注册依赖，不改台账、不自动推送）。

GitHub 自带 cron 会延迟/丢失。与其依赖需要注册账号的外部定时服务，不如让仓库自己
「每次运行都看一眼今天有没有漏掉的班次」：比对当日四个目标推送时刻与
data/telegram_delivery.json，只报告**已过目标时刻且缺失**的 slot。

硬约束：
- 只读台账，不写台账、不触发 workflow、不发 Telegram、不生成预测。
- 未到点的 slot 不报（否则会把 22:00 的班次误报成漏发）。
- 输出建议补发命令但不执行，避免误发与递归触发。
- 证据只认 (date, slot, status=sent, message_ids 非空)；picks_count=0 视为未有效送达。
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import DATA

MISSING = "未取得"

# 目标推送时刻（北京 HH:MM）-> slot。slot 1 采集提前 1 小时，但以推送时刻判定是否漏发。
TARGETS = {
    "1": (7, 0, "北京 07:00 早间版"),
    "2": (11, 0, "北京 11:00 更新"),
    "3": (16, 0, "北京 16:00 更新"),
    "4": (22, 0, "北京 22:00 更新"),
}

NOTE = (
    "只读比对当日目标时刻与发送台账；未到点不报、已有 message_ids 送达记录不报；"
    "不写台账、不触发 workflow、不发 Telegram、不生成任何预测或赛程盘口；"
    "建议补发命令只给出不执行，避免误发。零注册依赖，纯仓库内自愈可见性。"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _target_utc(date: str, slot: str) -> datetime | None:
    hh, mm, _ = TARGETS[slot]
    try:
        bj = datetime.strptime(date, "%Y-%m-%d").replace(
            hour=hh, minute=mm, tzinfo=timezone(timedelta(hours=8)))
        return bj.astimezone(timezone.utc)
    except ValueError:
        return None


def delivered(rows: list[dict], date: str, slot: str,
              chat: str | None = None) -> list[dict]:
    return [r for r in rows
            if r.get("date") == date
            and str(r.get("slot", "1")) == slot
            and r.get("status") == "sent"
            and bool(r.get("message_ids"))
            and int(r.get("picks_count") or 0) > 0
            and (chat is None or str(r.get("chat_id")) == str(chat))]


def check(date: str, ledger_path: Path, now: datetime | None = None,
          chat: str | None = None) -> dict:
    if not ledger_path.exists():
        rows = []
        ledger_note = "台账不存在；不补造记录，所有已过时刻班次列为待核"
    else:
        try:
            rows = json.loads(ledger_path.read_text(encoding="utf-8")).get("deliveries") or []
        except (OSError, ValueError):
            rows = []
        ledger_note = "只读 data/telegram_delivery.json"
    now = now or _now()
    slots = []
    for slot, (hh, mm, label) in TARGETS.items():
        tgt = _target_utc(date, slot)
        if tgt is None:
            slots.append({"slot": slot, "label": label, "target_utc": MISSING,
                          "due": False, "delivered": False, "status": "INVALID_DATE"})
            continue
        due = now >= tgt
        got = delivered(rows, date, slot, chat)
        slots.append({
            "slot": slot,
            "label": label,
            "target_utc": _stamp(tgt),
            "due": due,
            "delivered": bool(got),
            "message_ids": got[0].get("message_ids") if got else MISSING,
            "picks_count": got[0].get("picks_count") if got else MISSING,
            # 目标时刻未到却已有送达记录：如实标为离档班次，不推断原因、不包装成正常。
            "status": ("DELIVERED" if got and due else
                       "DELIVERED_BEFORE_TARGET" if got and not due else
                       "DUE_MISSING" if due else "NOT_DUE"),
        })
    missing = [s for s in slots if s["status"] == "DUE_MISSING"]
    off_schedule = [s["slot"] for s in slots if s["status"] == "DELIVERED_BEFORE_TARGET"]
    return {
        "schema": 1,
        "checked_at": _stamp(now),
        "date": date,
        "source": str(ledger_path),
        "slots": slots,
        "missing_slots": [s["slot"] for s in missing],
        "off_schedule_slots": off_schedule,
        "suggested_dispatch": [{"ref": "main", "inputs": {"slot": s["slot"]}} for s in missing],
        "alerts": ([{
            "code": "OFF_SCHEDULE_DELIVERY", "severity": "info",
            "message": (f"slot {', '.join(off_schedule)} 目标时刻未到但台账已有送达记录；"
                        "只记录事实，不推断为误发或正常"),
            "evidence": {"slots": off_schedule},
        }] if off_schedule else []),
        "note": NOTE + "；" + ledger_note,
        "unverified": [
            "Telegram 接收端是否真正读到（台账只记录 sendMessage 返回的 message_id）",
            "漏发的具体成因（cron 延迟 / 采集失败 / 闸门拦截，本盘点不推断）",
            "建议补发命令是否被实际执行（本命令不执行）",
        ],
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="当日漏发班次盘点（只读事实，零注册依赖，不自动补发）")
    ap.add_argument("--date", default=None, help="北京日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--ledger", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    date = a.date or (_now() + timedelta(hours=8)).strftime("%Y-%m-%d")
    datetime.strptime(date, "%Y-%m-%d")
    ledger = Path(a.ledger) if a.ledger else DATA / "telegram_delivery.json"
    result = check(date, ledger)
    if a.out:
        _atomic_write(Path(a.out), result)
    for s in result["missing_slots"]:
        print(f"::warning::DELIVERY_GAP {date} slot={s} 已过目标时刻但台账无有效送达记录；"
              f"建议核对后按需补发，本命令不自动触发")
    for a in result["alerts"]:
        print(f"::{a['severity']}::{a['code']} {a['message']}")
    print(f"DELIVERY_GAPS date={date} due_missing={len(result['missing_slots'])} "
          f"slots={','.join(s['slot'] + ':' + s['status'] for s in result['slots'])}")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
