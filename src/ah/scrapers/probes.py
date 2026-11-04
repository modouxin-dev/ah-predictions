"""AFC / A-Leagues / 沙特联：合规探测（2026-11-04 结论见 config/sources.yaml scrapers 段）。

- AFC (the-afc.com)：robots 全允许、条款未明文禁止自动化，但比赛中心是单页应用（服务端只返回约 5KB 外壳），
  首发由前端 JS 从第三方数据平台加载；不做 JS 渲染、不调内部接口 -> SiteUnavailable。
  日本球队参加的 ACLE/ACL2 比赛可由 J.League 官网覆盖（jleague.py）。
- A-Leagues (aleagues.com.au)：robots 允许页面但禁止 /fixtures/loaders/ 与 ?apiIdentifier= 等数据加载端点；
  比赛页由前端模板 + Opta 组件渲染 -> SiteUnavailable。
- 沙特联 (spl.com.sa)：robots 允许，但条款明确「未经书面许可禁止使用 Robots、Spiders 等自动化系统」
  -> 配置 enabled: false，不发任何请求（SourceDisabled）。

probe() 只用于定期确认这些结论是否仍然成立（例如站点改为服务端渲染），每次最多 1 个页面请求。
"""
from __future__ import annotations

import re

from . import SiteUnavailable, SourceDisabled, StructureChanged
from . import common
from .http import PoliteFetcher, load_site_config

LINEUP_MARKERS = re.compile(r"(?i)starting\s*(xi|line-?up)|\"startXI\"|\"lineups?\"\s*:")


def _spa_shell(html: str) -> bool:
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", body)
    return len(re.sub(r"\s+", " ", text).strip()) < 1500


def detect(site: str, html: str) -> str | None:
    """返回不可用原因；None 表示页面里看起来有服务端渲染的首发数据（需要人工复核后再接入）。"""
    if LINEUP_MARKERS.search(html):
        return None
    if site == "afc" and _spa_shell(html):
        return "比赛中心为单页应用，服务端 HTML 只有外壳，首发由前端 JS 加载（不做 JS 渲染）"
    if site == "aleagues" and ("<%=" in html or "opta-widgets" in html):
        return "比赛列表/比赛中心由前端模板与 Opta 组件渲染，数据端点被 robots.txt 禁止（/fixtures/loaders/、?apiIdentifier=）"
    return "页面不含首发数据"


def probe(site: str, fetcher: PoliteFetcher | None = None) -> dict:
    cfg = load_site_config(site)
    fetcher = fetcher or PoliteFetcher.from_config(site, cfg)
    if not cfg.get("enabled", True):
        msg = f"已停用：{cfg.get('disabled_reason', '')}"
        common.alert(site, msg)
        raise SourceDisabled(f"{site} {msg}")
    url = cfg["probe_url"]
    try:
        html, meta = fetcher.fetch(url, ttl_s=cfg.get("probe_ttl_s", 6 * 3600))
    except Exception as e:  # noqa: BLE001
        common.alert(site, f"{type(e).__name__}: {e}")
        raise
    reason = detect(site, html)
    if reason:
        common.alert(site, f"不可用：{reason}")
        raise SiteUnavailable(f"{site}: {reason}")
    common.alert(site, "页面出现疑似首发数据，结构与已知结论不同，需人工复核后再接入")
    raise StructureChanged(f"{site}: 页面结构与 2026-11-04 结论不同（出现首发标记），请人工复核 {url}")
