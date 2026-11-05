"""the-afc.com 官方比分（ACL Elite / ACL Two）——仅限内部比分核对（tos_note: internal-crosscheck-only）。

合规（先知 2026-11-04 审定）：robots.txt 全部允许；Terms of Service 未明文禁止自动化访问，但第 7 条禁止未经书面同意
复制/转售内容。因此：
- 只提取「对阵 + 比分」（队名、国家代码、阶段标记、进球数、来源链接），不保存页面正文、战报文字或标题原文；
- 页面 HTML 只在内存里解析，不落盘（sources.yaml afc.cache_raw: false）；落盘的只有抽取结果（claims 缓存）；
- 数据不展示、不转载；每次核对最多 2 页（ACLE/ACL2 落地页），同站间隔 ≥15 秒，走 PoliteFetcher；
- 停用开关：config/sources.yaml 里 scrapers.afc.scores.enabled: false，或环境变量 AH_AFC_SCORES_DISABLED=1，
  立即生效，不再发任何请求。
可用数据：赛事落地页服务端渲染（SSR）的 SEO 区块里的官方战报标题（「MD4: A (KOR) 3-3 B (JPN)」）和集锦视频链接 slug。
覆盖不全：只含最近有战报/集锦的比赛；没出现的比赛记「暂无」，绝不推断。不渲染 JS、不调内部接口。
"""
from __future__ import annotations

import html as htmllib
import json
import os
import re
from datetime import datetime, timezone

from . import SiteUnavailable
from . import common
from .http import CACHE_ROOT, PoliteFetcher, load_site_config

SITE = "afc"
BASE = "https://www.the-afc.com"
COMP_PAGES = {
    "acle": f"{BASE}/en/club/afc_champions_league_elite.html",
    "acl2": f"{BASE}/en/club/afc_champions_league_two.html",
}
MAX_PAGES = 2
API_LEAGUES = {17: "acle", 18: "acl2"}       # API-Football league id -> 本站赛事
KILL_ENV = "AH_AFC_SCORES_DISABLED"
CLAIMS_CACHE = CACHE_ROOT / SITE             # 只存抽取出的对阵+比分
_TEAM = r"[A-Za-z0-9][\w .'’&\-]{1,60}?"
HEADLINE_RE = re.compile(
    rf"(?:(?P<stage>MD\s*\d+|Group [A-Z]|[A-Z][\w ]{{1,30}}?)\s*:\s*)?(?P<home>{_TEAM})\s+\((?P<hc>[A-Z]{{3}})\)\s+"
    rf"(?P<hg>\d{{1,2}})\s*-\s*(?P<ag>\d{{1,2}})\s+(?P<away>{_TEAM})\s+\((?P<ac>[A-Z]{{3}})\)")
SLUG_RE = re.compile(
    r"/video/acl(?:elite|two)-\|-(?:group-[a-z]-|md\d+-)?(?P<home>[a-z0-9-]+?)-(?P<hc>[a-z]{3})-(?P<hg>\d{1,2})-"
    r"(?P<ag>\d{1,2})-(?P<away>[a-z0-9-]+?)-(?P<ac>[a-z]{3})(?=[\"<?#]|$)")
SSR_MARKERS = ('class="seo-footer"',)          # 落地页 SSR 的 SEO 区块；单页外壳没有


def scores_enabled(cfg: dict | None = None) -> tuple[bool, str]:
    """停用开关：环境变量优先，其次 sources.yaml（站点 enabled / scores.enabled）。"""
    if os.environ.get(KILL_ENV, "").strip() not in ("", "0", "false", "no"):
        return False, f"环境变量 {KILL_ENV} 已设置"
    try:
        cfg = cfg if cfg is not None else load_site_config(SITE)
    except Exception:  # noqa: BLE001 —— 配置读不到时按停用处理（宁停勿抓）
        return False, "读不到 sources.yaml 的 afc 配置"
    if not cfg.get("enabled", True):
        return False, "sources.yaml scrapers.afc.enabled=false"
    if not (cfg.get("scores") or {}).get("enabled", True):
        return False, "sources.yaml scrapers.afc.scores.enabled=false"
    return True, ""


def _text_blocks(html: str) -> list[str]:
    s = htmllib.unescape(html)
    blocks = re.findall(r"<h\d[^>]*>(.*?)</h\d>", s, re.S)
    blocks += re.findall(r'"name"\s*:\s*"([^"]{5,200})"', s)
    blocks += re.findall(r"<p[^>]*class=\"seo-footer\"[^>]*>([^<]{5,200})</p>", s)
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", b)).strip() for b in blocks]


def parse_scores(html: str, url: str, competition: str, fetched_at: str | None = None) -> list[dict]:
    """返回官方比分声明（只含对阵+比分，不含任何正文/标题原文）。
    单页外壳 -> SiteUnavailable；SSR 标记消失 -> StructureChanged。"""
    from .probes import detect
    if not any(m in html for m in SSR_MARKERS):
        shell = detect("afc", html)
        if shell and "单页应用" in shell:
            common.alert(SITE, f"{url}: {shell}")
            raise SiteUnavailable(f"afc: {url} 只有单页应用外壳，无服务端比分")
        raise common.structure_error(SITE, f"{url}: 找不到 SSR 的 seo-footer 区块")
    fetched_at = fetched_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, seen = [], set()

    def add(kind, home, hc, hg, ag, away, ac, stage=None, link=None):
        key = (common.norm(home), hc.upper(), int(hg), int(ag), common.norm(away), ac.upper())
        if key in seen:
            return
        seen.add(key)
        out.append({"source": SITE, "competition": competition, "kind": kind, "stage": stage,
                    "home": home.strip(), "home_country": hc.upper(), "away": away.strip(), "away_country": ac.upper(),
                    "home_goals": int(hg), "away_goals": int(ag), "source_url": link or url,
                    "fetched_at": fetched_at})

    for b in _text_blocks(html):
        for m in HEADLINE_RE.finditer(b):
            add("headline", m["home"], m["hc"], m["hg"], m["ag"], m["away"], m["ac"], stage=(m["stage"] or None))
    for m in SLUG_RE.finditer(htmllib.unescape(html)):
        add("video_slug", m["home"].replace("-", " "), m["hc"], m["hg"], m["ag"], m["away"].replace("-", " "),
            m["ac"], link=f"{url}{m.group(0)}")
    return out


def _claims_path(comp: str, cache_dir=None):
    return (cache_dir or CLAIMS_CACHE) / f"claims-{comp}.json"


def fetch_scores(competitions=("acle", "acl2"), fetcher: PoliteFetcher | None = None, ttl_s: int = 3600,
                 cache_dir=None) -> dict:
    """每个赛事最多 1 次请求（总共 ≤ MAX_PAGES 页）。HTML 只在内存解析；TTL 内复用抽取结果缓存（不发请求）。
    返回 {"claims": [...], "pages": {comp: {...}}, "enabled": bool}。"""
    ok_, why = scores_enabled()
    if not ok_:
        return {"claims": [], "enabled": False, "disabled_reason": why,
                "pages": {c: {"url": COMP_PAGES[c], "ok": False, "error": f"已停用：{why}"} for c in competitions}}
    competitions = list(dict.fromkeys(competitions))[:MAX_PAGES]
    fetcher = fetcher or PoliteFetcher.from_config(SITE)
    cdir = cache_dir or fetcher.cache_dir
    claims, pages = [], {}
    for comp in competitions:
        url = COMP_PAGES[comp]
        cp = _claims_path(comp, cdir)
        try:
            cached = json.loads(cp.read_text(encoding="utf-8")) if cp.exists() else None
            if cached and ttl_s and fetcher.now() - cached.get("ts", 0) <= ttl_s:
                got = cached["claims"]
                pages[comp] = {"url": url, "ok": True, "claims": len(got), "from_cache": True}
            else:
                text, meta = fetcher.fetch(url, ttl_s=0)
                got = parse_scores(text, url, comp, meta.get("fetched_at"))
                del text
                cp.parent.mkdir(parents=True, exist_ok=True)
                cp.write_text(json.dumps({"ts": fetcher.now(), "url": url, "claims": got}, ensure_ascii=False,
                                         indent=1), encoding="utf-8")
                pages[comp] = {"url": url, "ok": True, "claims": len(got), "from_cache": False}
            claims += got
        except Exception as e:  # noqa: BLE001 —— 逐页记录失败，不中断其他赛事
            pages[comp] = {"url": url, "ok": False, "error": f"{type(e).__name__}: {e}"}
    if any(p["ok"] for p in pages.values()):
        common.ok(SITE, {"score_claims": len(claims)})
    return {"claims": claims, "pages": pages, "enabled": True}
