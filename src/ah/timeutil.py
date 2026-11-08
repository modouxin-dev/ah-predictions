"""统一时区口径（工程层，纯常量/工具，无业务逻辑）。

背景：`BJ` / `BJT` / `timezone(timedelta(hours=8))` 此前分散在 10+ 文件各自定义，
容易出现一处改了另一处没改。这里收成单一真源：

- `BJT`：优先 `ZoneInfo("Asia/Shanghai")`（含历史夏令时规则，比固定 +8 更正确）；
  环境无 tzdata 时回退到固定 +8，**不抛异常、不推断**。
- `BJ = BJT`：兼容存量代码里两种命名。
- `utc_now()` / `utc_now_iso()` / `bj_date()` / `to_bj()`：统一时间读取与北京日期计算。

不改任何评分、核验、阈值、展示与推送逻辑；`build_site.py` 原本就用 ZoneInfo，
现在改为引用本模块，行为一致。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:  # pragma: no cover —— 取决于运行环境是否有 tzdata
    from zoneinfo import ZoneInfo
    BJT = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001 —— 只做安全回退，不推断原因
    BJT = timezone(timedelta(hours=8))

BJ = BJT
UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def bj_date(now: datetime | None = None) -> str:
    """北京日期 YYYY-MM-DD；now 可注入（测试用），无则取当前 UTC。"""
    return (now or utc_now()).astimezone(BJT).strftime("%Y-%m-%d")


def to_bj(dt: datetime, fmt: str = "%m-%d %H:%M") -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(BJT).strftime(fmt)
