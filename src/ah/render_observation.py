"""统一观察块渲染（展示层）：所有入口共用同一个函数与同一套免责标记。

背景
----
观察块此前在 `publish_telegram.py::observation_blocks()` 里手写拼接，免责标记只在
section 标题出现一次；`grounding.py::check_observation_markup()` 又是另一套字符串检查；
`recheck.py` 直接调 `observation_blocks`。三处措辞靠人肉保持一致，容易回退。

本模块把「渲染 + 校验」收成单一真源：
- 每个观察块**逐条**带「非正式推荐，不计战绩」，不只 section 标题带；
- section 标题固定带同一标记；
- 禁止出现任何推荐/断言措辞；
- `publish_telegram.observation_blocks()` 与 `grounding.check_observation_markup()`
  都改为调用本模块，行为不变、不再各写一套字符串。

硬边界
------
- 不改 `daily.py` 的 watchlist / observation 生成逻辑，不改评分、核验、阈值；
- 不新增场次、不凑数（沿用 DAILY_ITEMS_MAX 与「有真实评分才展示」）；
- 所有字段逐字取自报告 JSON，缺字段写「未取得」，不推断、不补造；
- 纯渲染函数，不发消息、不写文件。
"""
from __future__ import annotations
from .timeutil import BJT

import html
from datetime import datetime, timedelta, timezone

DISCLAIMER = "非正式推荐，不计战绩"
MARKERS = ("非正式推荐", "不计战绩")
BANNED = ("🎯 <b>推荐", "规则档位 🟢", "稳赢", "保证命中", "高胜率", "已校准概率")
DAILY_ITEMS_MAX = 7     # 与 publish_telegram.py 一致：正式推荐 + 观察合计上限

MISSING = "未取得"


def e(s) -> str:
    return html.escape(str(s if s is not None else MISSING), quote=False)


def ln(x) -> str:
    if x is None:
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    return "0" if v == 0 else f"{v:+g}"


def bj(utc, fmt: str = "%m-%d %H:%M") -> str:
    if not utc:
        return "—"
    try:
        dt = datetime.fromisoformat(str(utc).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BJT).strftime(fmt)
    except (ValueError, TypeError):
        return "—"


def sec(icon: str, title: str, note: str) -> str:
    return f"{icon} <b>{e(title)}</b> <i>{e(note)}</i>"


def render_one(w: dict) -> str:
    """渲染单条观察。每条都带免责标记，不依赖 section 标题。"""
    o = w.get("observation") if isinstance(w.get("observation"), dict) else {}
    reason = w.get("withheld_reason") or w.get("withheld_category") or "未达到正式发布门槛"
    age = "；报价超过30分钟" if o.get("freshness") == "stale_downgrade" else ""
    form = ""
    if o.get("form_source") == "5DollarFootballAPI":
        form = "Football data by 5DollarFootballAPI"
    elif o.get("form_source"):
        form = f"近况来源：{e(o['form_source'])}"
    lines = [
        f"⚽ <b>{e(w.get('home'))} vs {e(w.get('away'))}</b>",
        f"{e(w.get('league'))} · {bj(w.get('kickoff_utc'))} 北京时间",
        f"模型观察：{e(o.get('pick_team'))} {ln(o.get('line'))} · 评分 {e(o.get('score'))}（{DISCLAIMER}）",
        f"{e(o.get('bookmaker'))} 初盘 {ln(o.get('line_open'))} → 当前 {ln(o.get('line'))} @ {e(o.get('odds'))} 港盘",
        f"近况（最新在前，W胜/D平/L负）：{e(o.get('home_form'))} / {e(o.get('away_form'))}",
    ]
    if form:
        lines.append(form)
    lines.append(f"报价：{e(o.get('source'))} · {bj(o.get('price_ts'))} 北京时间{age}")
    lines.append(f"未正式推荐：{e(reason)}")
    return "\n".join(lines)


def render_observation_section(report: dict, n_picks: int | None = None) -> list[str]:
    """唯一渲染入口：返回与旧 observation_blocks 相同形状的 list[str]。"""
    if n_picks is None:
        n_picks = len(report.get("picks") or [])
    candidates = [w for w in (report.get("watchlist") or [])
                  if isinstance(w.get("observation"), dict)]
    try:
        candidates.sort(key=lambda w: -float(w["observation"].get("score", 0)))
    except (TypeError, ValueError):
        pass  # 排序失败保持原顺序，不编造分数
    blocks = [render_one(w) for w in candidates[:max(0, DAILY_ITEMS_MAX - n_picks)]]
    if not blocks:
        return []
    return [sec("🔎", "比赛观察分析", DISCLAIMER) + "\n" + "\n\n".join(blocks)]


def validate(rendered: str) -> list[str]:
    """唯一校验入口：观察内容出现时，标记必须齐全且不得有推荐/断言措辞。"""
    errors: list[str] = []
    if not (("比赛观察分析" in rendered) or ("模型观察" in rendered)):
        return errors
    for marker in MARKERS:
        if marker not in rendered:
            errors.append(f"观察块缺少免责标记：{marker}")
    for bad in BANNED:
        if bad in rendered:
            errors.append(f"观察块出现推荐/断言措辞：{bad}")
    return errors
