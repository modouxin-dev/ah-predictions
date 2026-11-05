"""先知 Telegram 按钮/命令机器人（长轮询，只读）。

运行：``python -m ah tg-bot``（需环境变量 TELEGRAM_BOT_TOKEN；绝不打印 Token）。
守护：``scripts/tg_bot.sh start|stop|status|ensure``。

- 只读：只读取本地仓库文件（reports/、data/、config/）；不会触发发布、git 提交或任何付费 API。
- 数据新鲜度：最多每 5 分钟执行一次 ``git pull --ff-only``（仅在工作区干净时）。
- 每个 chat 限流；群里的未知命令静默忽略。
"""
from __future__ import annotations

import html
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import CONFIG, DATA, REPORTS, ROOT, policy, records, source_status
from . import publish_telegram as pt

SITE_URL = pt.SITE_URL
BOT_USERNAME = "LYZKYPbot"
BJ = timezone(timedelta(hours=8))
e = pt.e

# (command, 中文描述) —— setMyCommands 用
COMMANDS = [
    ("start", "打开先知菜单"),
    ("today", "今日推荐"),
    ("sources", "数据源状态"),
    ("stats", "历史命中率 / 回测"),
    ("records", "赛前确认 / 赛后结算"),
    ("rules", "规则说明（命中率优先）"),
    ("report", "最新报告与网站看板"),
]
ALIASES = {"start": "menu", "menu": "menu", "help": "menu", "today": "today", "sources": "sources",
           "stats": "stats", "records": "records", "rules": "rules", "report": "report"}

# 键盘布局（行）
MENU_ROWS = [
    [("⚽ 今日推荐", "today"), ("📡 数据源状态", "sources")],
    [("📈 历史命中率", "stats"), ("🔎 赛前确认 / 🏁 赛后结算", "records")],
    [("📜 规则说明", "rules"), ("📰 最新报告", "report")],
]
BACK = [{"text": "« 返回菜单", "callback_data": "m:menu"}]


def menu_keyboard() -> dict:
    rows = [[{"text": t, "callback_data": f"m:{k}"} for t, k in row] for row in MENU_ROWS]
    rows.append([{"text": "🌐 完整看板", "url": SITE_URL}])
    return {"inline_keyboard": rows}


def back_keyboard(extra: list[list[dict]] | None = None) -> dict:
    return {"inline_keyboard": [*(extra or []), BACK]}


# ---------- 数据读取 ----------

def _json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def report_files(reports: Path = REPORTS) -> list[Path]:
    return sorted(reports.glob("*/*/????-??-??.json"), key=lambda p: p.stem)


def latest_report(reports: Path = REPORTS) -> dict | None:
    fs = report_files(reports)
    return _json(fs[-1]) if fs else None


def recent_reports(n: int = 7, reports: Path = REPORTS) -> list[dict]:
    return [r for r in (_json(p) for p in report_files(reports)[-n:]) if r][::-1]


def load_settings(config: Path = CONFIG) -> dict:
    try:
        import yaml
        return yaml.safe_load((config / "settings.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def now_bj() -> datetime:
    return datetime.now(BJ)


def footer() -> str:
    return "\n".join(pt.footer())


def _status_icon(st: str) -> str:
    return {"已撤销": "❌", "已结算": "🏁", "已确认": "✅", "待确认": "⏳"}.get(st, "")


# ---------- 各回复 ----------

def menu_text() -> str:
    pol = policy.load()
    rule = policy.publish_rule_title(pol)
    return ("<b>【先知】⚽ 亚盘分析菜单</b>\n"
            f"命中率优先：{rule}，宁缺毋滥。\n\n"
            "⚽ 今日推荐 ｜ 📡 数据源状态\n📈 历史命中率 ｜ 🔎 赛前确认 / 🏁 赛后结算\n📜 规则说明 ｜ 📰 最新报告\n\n"
            + footer())


def today_text(rep: dict | None, pol: dict | None = None, stats: dict | None = None) -> str:
    pol = pol or policy.load()
    head = "<b>【先知】⚽ 今日推荐</b>"
    if not rep:
        return f"{head}\n\n{policy.no_pick_title(pol)}（宁缺毋滥）\n\n{footer()}"
    picks = policy.published(rep, pol)
    lines = [head, pt.date_line(rep, len(picks))]
    if not picks:
        lines += ["", *pt.no_pick_lines(rep)]
    for p in picks:
        st = records.status(p)
        lines += [pt.DIV, pt.compact_block(p, stats, extra=[f"⏱ 状态：{_status_icon(st)} {st}"])]
    return "\n".join(lines) + "\n" + pt.DIV + "\n" + pt.tail(stats)


def _ago(ts: str | None) -> str:
    return pt.bj(ts) if ts else "—"


def sources_text(st: dict | None) -> str:
    st = st or {}
    srcs = st.get("sources") or {}
    lines = ["<b>【先知】📡 数据源状态</b>"]
    for k, meta in source_status.SOURCES.items():
        s = srcs.get(k) or {}
        ok_t, err_t = s.get("last_ok_at"), s.get("last_error_at")
        if not s:
            dot, label = "⚪", "暂无运行记录"
        elif err_t and (not ok_t or err_t > ok_t):
            dot, label = "🔴", "最近一次请求出错"
        elif ok_t:
            dot, label = "🟢", "正常"
        else:
            dot, label = "🟡", "有调用记录"
        q = s.get("quota") or {}
        if meta["limit"] is None:
            qs = "无额度限制"
        elif q.get("remaining") is not None:
            qs = f"剩余 {q['remaining']}/{q.get('limit', meta['limit'])}（每{q.get('period', meta['period'])}）"
        else:
            qs = f"上限 {meta['limit']}/{meta['period']}"
        lines += ["", f"{dot} <b>{e(meta['name'])}</b> · {label}", f"• {e(meta['role'])}",
                  f"• 最近调用 {_ago(s.get('last_call_at') or ok_t)} ｜ {e(qs)}"]
        if s.get("last_error") and label.startswith("最近一次"):
            lines.append(f"• 错误：{e(source_status.sanitize(str(s['last_error']))[:120])}")
        if k == "fivedollar":
            lines.append("• <i>Bet365 亚盘数据来自 5DollarFootballAPI</i>")
    if st.get("updated_at"):
        lines += ["", f"🕐 状态更新于 {pt.bj(st['updated_at'])} 北京时间（不含任何密钥）"]
    return "\n".join(lines) + "\n\n" + footer()


def _rate(d: dict) -> str:
    if not d.get("decided"):
        return f"{d.get('picks', 0)} 场，暂无已结算"
    return (f"{d['hit_rate']:.1%}（{d['decided']} 场，{d.get('units', 0):+.2f} 单位，ROI {d.get('roi', 0):+.1%}）")


def stats_text(s: dict | None) -> str:
    lines = ["<b>【先知】📈 历史命中率 / 回测</b>"]
    o = (s or {}).get("overall") or {}
    if o.get("decided"):
        lines.append(f"📊 总体 {_rate(o)}")
        bc = s.get("by_confidence") or {}
        conf_rows = [[f"{k}信心", v.get("picks", 0), v.get("win", 0) + v.get("half_win", 0), v.get("push", 0),
                      v.get("loss", 0) + v.get("half_loss", 0), f"{v.get('hit_rate', 0):.0%}", f"{v.get('units', 0):+.1f}"]
                     for k, v in bc.items() if v.get("picks")]
        if conf_rows:
            lines += ["🔥 <b>分信心</b>", pt.pre_table(["", "场次", "赢", "走", "输", "命中", "盈亏"], conf_rows)]
    lines += [pt.DIV, pt.stats_table(s, top=5),
              "<i>命中率 = (赢 + 0.5×赢半) / (场次 − 走水 − 取消)；每注 1 单位</i>"]
    if s and s.get("updated_at"):
        lines.append(f"🕐 统计更新于 {pt.bj(s['updated_at'])} 北京时间")
    return "\n".join(lines) + "\n\n" + footer()



def records_text(reps: list[dict], pol: dict | None = None, limit: int = 5) -> str:
    pol = pol or policy.load()
    lines = ["<b>【先知】🔎 赛前确认 / 🏁 赛后结算</b>"]
    rows = []
    for r in reps:
        for p in sorted(records.published_picks(r, pol), key=lambda p: p.get("kickoff_utc") or "", reverse=True):
            rows.append(p)
    if not rows:
        lines += ["", "暂无推荐记录"]
    for p in rows[:limit]:
        st = records.status(p)
        odds = f" @ {p['odds']}" if p.get("odds") is not None else ""
        lines += ["", f"⚽ <b>{e(p['home'])} vs {e(p['away'])}</b>",
                  f"🕐 {pt.bj(p.get('kickoff_utc'))} 北京时间 ｜ {_status_icon(st)} {st}",
                  f"🎯 {e(pt._team(p))} {pt.ln(p['line'])}{odds}"]
        pm = records.prematch_of(p)
        if pm:
            dec = pt.DECISION.get(pm.get("decision"), e(pm.get("decision") or "—"))
            extra = ""
            if pm.get("decision") == "调整" and pm.get("new_line") is not None:
                extra = f" → {pt.ln(pm['new_line'])}" + (f" @ {pm['new_odds']}" if pm.get("new_odds") is not None else "")
            reason = str(pm.get("reason") or "")
            reason = reason[:80] + ("…" if len(reason) > 80 else "")
            lines.append(f"🔎 赛前：{dec}{extra}" + (f"（{pt.bj(pm['checked_at'])}）" if pm.get("checked_at") else ""))
            if reason:
                lines.append(f"   {e(reason)}")
        else:
            lines.append("🔎 赛前：⏳ 待确认（开赛前 50 分钟）")
        res = p.get("result") or {}
        if res.get("settlement"):
            s = res["settlement"]
            u = f"{float(res['units']):+.2f} 单位" if res.get("units") is not None else "—"
            score = f"比分 {e(res['score'])} ｜ " if res.get("score") else ""
            lines.append(f"🏁 赛后：{score}{pt.SETTLE_ICON.get(s, '')} {e(pt.SETTLE_CN.get(s, s))}（{u}）")
        elif st != "已撤销":
            lines.append("🏁 赛后：⏳ 待结算")
    return "\n".join(lines) + "\n\n" + footer()


def rules_text(pol: dict | None = None, cfg: dict | None = None) -> str:
    pol = pol or policy.load()
    cfg = cfg if cfg is not None else load_settings()
    pr = cfg.get("push_rules") or {}
    dr = (pr.get("daily_report") or {}).get("time", "10:00")
    pm = (pr.get("pre_match_final_check") or {}).get("minutes_before_kickoff", 50)
    tol = float(pol.get("alignment_tolerance", 0.25))
    lines = ["<b>【先知】📜 规则说明</b>", "",
             "🎯 <b>命中率优先</b>：宁可少推，不推不确定的场次。",
             f"🔥 <b>{e(policy.publish_rule_title(pol))}</b>：{e(policy.publish_rule_desc(pol))}",
             f"📅 <b>每日上限</b>：最多 {pol['max_picks_per_day']} 场（积累期目标 5–10 场）。",
             (f"🛡 <b>深盘</b>：|线|≥{float(pol.get('max_abs_line_to_publish', 1.75)):g} 默认进观察，不进正式。"
              if pol.get("max_abs_line_to_publish") is not None else None),
             "📊 <b>真实盘口</b>：没有真实盘口与水位绝不推荐，绝不编造数据。" if pol.get("require_real_odds") else None,
             f"⚖️ <b>市场共识</b>：推荐盘与多家中位盘相差 &gt;{tol:g} 降一档，&gt;{2 * tol:g} 直接撤下。",
             f"🔎 <b>赛前确认</b>：开赛前 {pm} 分钟复核临场盘口、首发、伤停；"
             + ("信心下降即撤销。" if pol.get("prematch_cancel_on_confidence_drop") else "按需调整。"),
             "🏁 <b>赛后回测</b>：回填比分，亚盘结算并复盘；撤销记为取消，不计入命中率。",
             "📈 <b>命中率</b> = (赢 + 0.5×赢半) / (场次 − 走水 − 取消)。",
             f"🕐 <b>推送</b>：每日 {e(dr)} 北京时间日报 → 赛前确认 → 赛后回测。",
             f"🗓 规则自 {e(pol.get('effective_from', ''))} 起生效。"]
    return "\n".join(x for x in lines if x is not None) + "\n\n" + footer()


def report_text(rep: dict | None, pol: dict | None = None) -> tuple[str, dict]:
    pol = pol or policy.load()
    site = [{"text": "🌐 完整看板", "url": SITE_URL}]
    if not rep:
        return f"<b>【先知】📰 最新报告</b>\n\n暂无报告\n\n{footer()}", back_keyboard([site])
    picks = policy.published(rep, pol)
    gen = f"生成于 {pt.bj(rep['generated_at'])} 北京时间" if rep.get("generated_at") else ""
    lines = [f"<b>【先知】📰 最新报告 {e(rep['date'])}</b>", f"🕐 {gen}" if gen else "",
             f"🎯 公开推荐 {len(picks)} 场"]
    if not picks:
        lines += pt.no_pick_lines(rep)
    for p in picks:
        conf = pt.CONF.get(p.get("confidence"), str(p.get("confidence")))
        odds = f" @ {p['odds']}" if p.get("odds") is not None else ""
        st = records.status(p)
        lines.append(f"• {e(p['home'])} vs {e(p['away'])} ｜ <b>{e(pt._team(p))} {pt.ln(p['line'])}{odds}</b> ｜ "
                     f"{pt.CONF_ICON.get(conf, '')}{e(conf)} ｜ {_status_icon(st)}{st}")
    lines += ["", "📈 " + pt.history(_json(DATA / "stats.json")).removeprefix("📈 ")]
    page = f"{SITE_URL}reports/{rep['date']}.html"
    kb = back_keyboard([site, [{"text": f"📰 {rep['date']} 报告页", "url": page}]])
    return "\n".join(x for x in lines if x) + "\n\n" + footer(), kb


def render(action: str, reports: Path = REPORTS, data: Path = DATA) -> tuple[str, dict]:
    """按钮/命令 -> (HTML 文本, reply_markup)。纯本地读取，无网络。"""
    pol = policy.load()
    if action == "today":
        out = today_text(latest_report(reports), pol, _json(data / "stats.json")), back_keyboard([[{"text": "🌐 完整看板", "url": SITE_URL}]])
    elif action == "sources":
        out = sources_text(source_status.load_latest(data / "source_status.json")), back_keyboard()
    elif action == "stats":
        out = stats_text(_json(data / "stats.json")), back_keyboard(
            [[{"text": "📈 战绩页", "url": SITE_URL + "stats.html"}]])
    elif action == "records":
        out = records_text(recent_reports(7, reports), pol), back_keyboard(
            [[{"text": "📋 推荐记录页", "url": SITE_URL + "records.html"}]])
    elif action == "rules":
        out = rules_text(pol), back_keyboard([[{"text": "📜 规则页", "url": SITE_URL + "rules.html"}]])
    elif action == "report":
        out = report_text(latest_report(reports), pol)
    else:
        out = menu_text(), menu_keyboard()
    text, kb = out
    if len(text) > pt.LIMIT:  # 单条消息上限（编辑消息不能拆分）
        text = pt.split([text])[0]
        cut = text.rfind("\n")
        text = text[:cut] + "\n…\n\n" + footer() if cut > 0 else text
        text = text[: pt.LIMIT]
    return text, kb


# ---------- 更新处理（纯函数，便于测试） ----------

def parse_command(text: str, bot_username: str = BOT_USERNAME) -> str | None:
    """'/today@LYZKYPbot arg' -> 'today'；发给其他机器人 -> None；非命令 -> None。"""
    if not text or not text.startswith("/"):
        return None
    cmd = text.split()[0][1:]
    if "@" in cmd:
        cmd, at = cmd.split("@", 1)
        if at.lower() != bot_username.lower():
            return None
    return cmd.lower()


class RateLimiter:
    def __init__(self, min_interval: float = 1.5, per_minute: int = 20, clock=time.monotonic):
        self.min_interval, self.per_minute, self.clock = min_interval, per_minute, clock
        self.hist: dict[int, list[float]] = {}

    def allow(self, chat_id: int) -> bool:
        t = self.clock()
        h = [x for x in self.hist.get(chat_id, []) if t - x < 60]
        if (h and t - h[-1] < self.min_interval) or len(h) >= self.per_minute:
            self.hist[chat_id] = h
            return False
        h.append(t)
        self.hist[chat_id] = h
        return True


def handle(update: dict, limiter: RateLimiter, bot_username: str = BOT_USERNAME,
           reports: Path = REPORTS, data: Path = DATA, max_age: int = 900, now: float | None = None) -> list[tuple[str, dict]]:
    """返回要调用的 Bot API 列表 [(method, params)]。不做网络调用。"""
    now = time.time() if now is None else now
    if "callback_query" in update:
        cq = update["callback_query"]
        msg = cq.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        data_s = str(cq.get("data") or "")
        if not data_s.startswith("m:") or chat is None:
            return [("answerCallbackQuery", {"callback_query_id": cq["id"]})]
        if not limiter.allow(chat):
            return [("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "操作太频繁，请稍候再试"})]
        action = data_s[2:]
        text, kb = render(action, reports, data)
        return [("answerCallbackQuery", {"callback_query_id": cq["id"]}),
                ("editMessageText", {"chat_id": chat, "message_id": msg.get("message_id"), "text": text,
                                     "parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": kb})]
    msg = update.get("message")
    if not msg:
        return []
    chat = msg.get("chat") or {}
    if now - float(msg.get("date", now)) > max_age:  # 过期消息（停机期间积压）不回复
        return []
    private = chat.get("type") == "private"
    cmd = parse_command(msg.get("text") or "", bot_username)
    action = ALIASES.get(cmd or "")
    if action is None:
        if private and not cmd:
            action = "menu"  # 私聊随便发什么都回菜单
        elif private and cmd:
            action = "menu"
        else:
            return []  # 群里：非命令 / 未知命令 / 发给其他机器人 —— 静默
    if not limiter.allow(chat["id"]):
        return []
    text, kb = render(action, reports, data)
    params = {"chat_id": chat["id"], "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
              "reply_markup": kb}
    if not private:
        params["reply_parameters"] = {"message_id": msg["message_id"], "allow_sending_without_reply": True}
    if msg.get("message_thread_id"):
        params["message_thread_id"] = msg["message_thread_id"]
    return [("sendMessage", params)]


# ---------- 网络层 ----------

def log(msg: str) -> None:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    s = source_status.sanitize(str(msg))
    if tok:
        s = s.replace(tok, "***")
    print(f"{datetime.now(BJ):%Y-%m-%d %H:%M:%S}+08 {s}", flush=True)


class Api:
    def __init__(self, token: str, session: requests.Session | None = None):
        self._base = f"https://api.telegram.org/bot{token}/"
        self.s = session or requests.Session()

    def call(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        try:
            r = self.s.post(self._base + method, json=params or {}, timeout=timeout)
            return r.json()
        except (requests.RequestException, ValueError) as ex:
            # 异常信息可能含 URL（含 Token），只保留类型名
            return {"ok": False, "error_code": -1, "description": f"network: {type(ex).__name__}"}


def execute(api: Api, calls: list[tuple[str, dict]]) -> None:
    for method, params in calls:
        res = api.call(method, params)
        if res.get("ok"):
            continue
        desc = str(res.get("description", ""))
        if method == "editMessageText" and "not modified" in desc:
            continue
        if method == "editMessageText":  # 旧消息不可编辑 -> 发新消息
            p = {k: v for k, v in params.items() if k != "message_id"}
            res = api.call("sendMessage", p)
            if res.get("ok"):
                continue
            desc = str(res.get("description", ""))
        log(f"[warn] {method} failed: {res.get('error_code')} {desc[:200]}")


class Puller:
    """最多每 interval 秒 git pull 一次（仅在工作区无已跟踪改动、无进行中的 git 操作时）。"""

    def __init__(self, root: Path = ROOT, interval: int = 300):
        self.root, self.interval, self.last = root, interval, 0.0

    def maybe(self) -> None:
        if time.monotonic() - self.last < self.interval:
            return
        self.last = time.monotonic()
        if os.environ.get("AH_TG_NO_PULL") or not (self.root / ".git").exists():
            return
        if (self.root / ".git" / "index.lock").exists() or (self.root / ".git" / "rebase-merge").exists():
            return
        try:
            dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=self.root,
                                   capture_output=True, text=True, timeout=30).stdout.strip()
            if dirty:
                return
            r = subprocess.run(["git", "pull", "--ff-only", "-q"], cwd=self.root, capture_output=True, text=True,
                               timeout=90, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            if r.returncode != 0:
                log(f"[warn] git pull: {r.stderr.strip()[:200]}")
        except Exception as ex:  # noqa: BLE001
            log(f"[warn] git pull error: {type(ex).__name__}")


OFFSET_FILE = ROOT / "logs" / "tg_bot.offset"


def run() -> None:
    from .secrets import get_secret as secret
    token = secret("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN 未设置")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", token)
    api = Api(token)
    me = api.call("getMe")
    username = (me.get("result") or {}).get("username") or BOT_USERNAME
    log(f"start: @{username} (long polling, read-only)")
    limiter, puller = RateLimiter(), Puller()
    try:
        offset = int(OFFSET_FILE.read_text().strip())
    except (OSError, ValueError):
        offset = None
    last_conflict_fix, backoff = 0.0, 1
    while True:
        puller.maybe()
        params = {"timeout": 50, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        res = api.call("getUpdates", params, timeout=65)
        if not res.get("ok"):
            code, desc = res.get("error_code"), str(res.get("description", ""))
            log(f"[warn] getUpdates: {code} {desc[:200]}")
            if code == 409 and "webhook" in desc.lower() and time.monotonic() - last_conflict_fix > 600:
                # 旧服务重新设置了 webhook：用户已授权由先知接管，删除它（不丢弃积压更新）
                last_conflict_fix = time.monotonic()
                log(f"[info] deleteWebhook -> {api.call('deleteWebhook', {'drop_pending_updates': False}).get('ok')}")
            if code == 401:
                time.sleep(300)  # Token 失效：放慢重启频率
                raise SystemExit("Token 无效（401）")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        backoff = 1
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            try:
                calls = handle(upd, limiter, username)
                if calls:
                    kind = "callback" if "callback_query" in upd else "message"
                    log(f"update {upd['update_id']} {kind} -> {', '.join(m for m, _ in calls)}")
                execute(api, calls)
            except Exception as ex:  # noqa: BLE001
                log(f"[error] update {upd.get('update_id')}: {type(ex).__name__}: {ex}")
        if res.get("result"):
            try:
                OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
                OFFSET_FILE.write_text(str(offset))
            except OSError:
                pass


def set_commands(api: Api) -> dict:
    cmds = [{"command": c, "description": d} for c, d in COMMANDS]
    out = {}
    for scope in (None, {"type": "all_group_chats"}, {"type": "all_private_chats"}):
        p = {"commands": cmds}
        if scope:
            p["scope"] = scope
        out[(scope or {}).get("type", "default")] = api.call("setMyCommands", p).get("ok")
    out["menu_button"] = api.call("setChatMenuButton", {"menu_button": {"type": "commands"}}).get("ok")
    return out


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "set-commands":
        from .secrets import get_secret as secret
        print(json.dumps(set_commands(Api(secret("TELEGRAM_BOT_TOKEN") or "")), ensure_ascii=False))
    elif argv and argv[0] == "preview":
        action = argv[1] if len(argv) > 1 else "menu"
        text, kb = render(action)
        print(pt.plain(text))
        print("\n[键盘] " + " / ".join("[" + " | ".join(b["text"] for b in row) + "]" for row in kb["inline_keyboard"]))
    else:
        run()


if __name__ == "__main__":
    main()
