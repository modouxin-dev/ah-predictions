"""数据源运行状态 -> data/source_status.json（网站「数据源」页）。

各客户端每次真实请求后调用 record()：最近成功时间、最近错误、已知额度。
绝不写入 Key：错误信息会去掉 query 参数中的 key/token，并替换所有已知密钥值。
测试/其他场景可用环境变量 AH_SOURCE_STATUS_FILE 改写入位置。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import DATA

STATUS_FILE = DATA / "source_status.json"
SECRET_NAMES = ("ODDS_API_KEY", "ODDSPAPI_KEY", "API_FOOTBALL_KEY", "FOOTBALL_DATA_KEY", "FIVEDOLLAR_KEY",
                "TELEGRAM_BOT_TOKEN", "GITHUB_AH_TOKEN")

# 展示顺序与静态信息（额度为各家公开免费档）
SOURCES = {
    "api_football": {"name": "API-Football", "role": "主源：赛程、首发、交锋、10 家公司亚盘",
                     "limit": 100, "period": "日", "url": "https://www.api-football.com/"},
    "odds_api": {"name": "The Odds API", "role": "多公司亚盘共识（含 Pinnacle）",
                 "limit": 500, "period": "月", "url": "https://the-odds-api.com/"},
    "oddspapi": {"name": "OddsPapi", "role": "亚洲公司亚盘共识（亚冠/卡塔尔等补充）",
                 "limit": 250, "period": "月", "url": "https://oddspapi.io/"},
    "football_data": {"name": "football-data.org", "role": "欧洲联赛积分、状态、交锋；赛果交叉核对",
                      "limit": 10, "period": "分钟", "url": "https://www.football-data.org/"},
    "fivedollar": {"name": "5DollarFootballAPI", "role": "五大联赛 Bet365 亚盘初盘→即时盘",
                   "limit": 60, "period": "小时", "url": "https://5dollarfootballapi.com/"},
    "titan007": {"name": "titan007（球探）", "role": "备用：即时赛程与默认公司盘口",
                 "limit": None, "period": None, "url": "https://live.titan007.com/"},
}


def path() -> Path:
    return Path(os.environ.get("AH_SOURCE_STATUS_FILE") or STATUS_FILE)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize(msg: str) -> str:
    s = str(msg)
    for n in SECRET_NAMES:
        v = os.environ.get(n)
        if v and len(v) >= 6:
            s = s.replace(v, "***")
    try:
        from .secrets import get_secret
        for n in SECRET_NAMES:
            v = get_secret(n)
            if v and len(v) >= 6:
                s = s.replace(v, "***")
    except Exception:  # noqa: BLE001
        pass
    s = re.sub(r"(?i)(api_?key|apikey|token|key|authorization|bearer)([=:\s]+)[^&\s,;]+", r"\1\2***", s)
    return s[:300]


def load(p: Path | None = None) -> dict:
    try:
        d = json.loads((p or path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d.setdefault("sources", {})
    return d


def record(source: str, ok: bool, error: str | None = None, quota: dict | None = None,
           at: str | None = None, p: Path | None = None) -> None:
    """记录一次真实请求结果。永不抛异常（状态记录失败不影响数据抓取）。"""
    try:
        p = p or path()
        d = load(p)
        s = d["sources"].setdefault(source, {})
        t = at or _now()
        s["last_call_at"] = t
        if ok:
            s["last_ok_at"] = t
        else:
            s["last_error_at"] = t
            s["last_error"] = sanitize(error or "未知错误")
        if quota:
            q = {k: v for k, v in quota.items() if v is not None}
            if q:
                s["quota"] = {**s.get("quota", {}), **q, "as_of": t}
        d["updated_at"] = t
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(p)
    except Exception:  # noqa: BLE001
        pass


def seed_from_usage(cache=None, p: Path | None = None) -> dict:
    """用各客户端本地用量文件（data/cache/*usage*.json，不入库）补全额度信息；只写真实记录过的数字。"""
    cache = Path(cache or DATA / "cache")
    p = p or path()
    d = load(p)

    def rd(name):
        try:
            return json.loads((cache / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def mtime(name):
        return datetime.fromtimestamp((cache / name).stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    u = rd("api_usage.json")
    if u:
        s = d["sources"].setdefault("api_football", {})
        rem = u.get("remaining") if u.get("remaining") is not None else 100 - u.get("used", 0)
        s["quota"] = {"used": u.get("used"), "remaining": rem, "limit": 100, "period": "日", "as_of": mtime("api_usage.json")}
        oks = [x for x in u.get("log", []) if x.get("status") == 200]
        if oks and u.get("date") and not s.get("last_ok_at"):
            s["last_ok_at"] = f"{u['date']}T{oks[-1]['t']}Z"
            s.setdefault("last_call_at", s["last_ok_at"])
    u = rd("odds_api_usage.json")
    if u:
        s = d["sources"].setdefault("odds_api", {})
        s["quota"] = {"used": u.get("used"), "remaining": u.get("remaining"), "limit": 500, "period": "月",
                      "as_of": mtime("odds_api_usage.json")}
        ts = (u.get("last") or {}).get("ts")
        if ts and not s.get("last_call_at"):
            s["last_call_at"] = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    u = rd("oddspapi_usage.json")
    if u:
        s = d["sources"].setdefault("oddspapi", {})
        lim = u.get("limit", 250)
        s["quota"] = {"used": u.get("used"), "remaining": lim - u.get("used", 0), "limit": lim, "period": "月",
                      "month": u.get("month"), "as_of": mtime("oddspapi_usage.json")}
        s.setdefault("last_call_at", mtime("oddspapi_usage.json"))
    u = rd("fivedollar_usage.json")
    if u:
        s = d["sources"].setdefault("fivedollar", {})
        s["quota"] = {"remaining": u.get("remaining"), "limit": u.get("limit") or 60, "period": "小时",
                      "as_of": mtime("fivedollar_usage.json")}
        s.setdefault("last_call_at", mtime("fivedollar_usage.json"))
    d["updated_at"] = _now()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return d


def main() -> None:
    d = seed_from_usage()
    for k, meta in SOURCES.items():
        s = d["sources"].get(k, {})
        q = s.get("quota", {})
        print(f"{meta['name']}: ok={s.get('last_ok_at', '-')} err={'有' if s.get('last_error') else '无'} "
              f"remaining={q.get('remaining', '-')}")


if __name__ == "__main__":
    main()
