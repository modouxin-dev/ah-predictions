"""the-afc.com 官方比分（ACL Elite / ACL Two）。

合规结论（2026-11-04 核查）：robots.txt 全部允许（只声明 sitemap）；Terms of Service 未明文禁止自动化访问，
但第 7 条禁止未经书面同意复制/转售内容 -> 只抽取「比分」这一事实用于内部核对，不转载文字/图片，不对外发布；
每个赛事落地页一天最多拉 1~2 次（间隔 ≥15 秒），走 PoliteFetcher（robots、诚实 UA、缓存、条件请求、退避）。

可用数据：赛事落地页服务端渲染（SSR）的 SEO 区块里有官方战报标题和集锦视频链接，形如
  「MD4: FC Pohang Steelers (KOR) 3-3 Gamba Osaka (JPN)」「Group E: FC Seoul (KOR) 3-0 The Cong - Viettel FC (VIE)」
  「/video/aclelite-|-al-hilal-sfc-ksa-2-0-shabab-al-ahli-uae」。
覆盖不全：只含最近几场有战报/集锦的比赛；没出现的比赛记「暂无」，绝不推断。
不渲染 JS、不调比赛中心背后的内部接口（数据平台接口未公开授权）。
"""
from __future__ import annotations

import html as htmllib
import re
from datetime import datetime, timezone

from . import SiteUnavailable
from . import common
from .http import PoliteFetcher

SITE = "afc"
BASE = "https://www.the-afc.com"
COMP_PAGES = {
    "acle": f"{BASE}/en/club/afc_champions_league_elite.html",
    "acl2": f"{BASE}/en/club/afc_champions_league_two.html",
}
API_LEAGUES = {17: "acle", 18: "acl2"}       # API-Football league id -> 本站赛事
_TEAM = r"[A-Za-z0-9][\w .'’&\-]{1,60}?"
HEADLINE_RE = re.compile(
    rf"(?:(?P<stage>MD\s*\d+|Group [A-Z]|[A-Z][\w ]{{1,30}}?)\s*:\s*)?(?P<home>{_TEAM})\s+\((?P<hc>[A-Z]{{3}})\)\s+"
    rf"(?P<hg>\d{{1,2}})\s*-\s*(?P<ag>\d{{1,2}})\s+(?P<away>{_TEAM})\s+\((?P<ac>[A-Z]{{3}})\)")
SLUG_RE = re.compile(
    r"/video/acl(?:elite|two)-\|-(?:group-[a-z]-|md\d+-)?(?P<home>[a-z0-9-]+?)-(?P<hc>[a-z]{3})-(?P<hg>\d{1,2})-"
    r"(?P<ag>\d{1,2})-(?P<away>[a-z0-9-]+?)-(?P<ac>[a-z]{3})(?=[\"<?#]|$)")
SSR_MARKERS = ('class="seo-footer"',)          # 落地页 SSR 的 SEO 区块；单页外壳没有


def _text_blocks(html: str) -> list[str]:
    """只看 SSR 的标题类片段：h4.seo-footer、JSON-LD 的 name、图集 alt 之外的纯文本标题。"""
    s = htmllib.unescape(html)
    blocks = re.findall(r"<h\d[^>]*>(.*?)</h\d>", s, re.S)
    blocks += re.findall(r'"name"\s*:\s*"([^"]{5,200})"', s)
    blocks += re.findall(r"<p[^>]*class=\"seo-footer\"[^>]*>([^<]{5,200})</p>", s)
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", b)).strip() for b in blocks]


def parse_scores(html: str, url: str, competition: str, fetched_at: str | None = None) -> list[dict]:
    """返回官方比分声明列表（去重）。页面不是 SSR（单页外壳）-> SiteUnavailable；SSR 标记消失 -> StructureChanged。"""
    from .probes import detect
    if not any(m in html for m in SSR_MARKERS):
        shell = detect("afc", html)
        if shell and "单页应用" in shell:
            common.alert(SITE, f"{url}: {shell}")
            raise SiteUnavailable(f"afc: {url} 只有单页应用外壳，无服务端比分")
        raise common.structure_error(SITE, f"{url}: 找不到 SSR 的 seo-footer / JSON-LD 区块")
    fetched_at = fetched_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, seen = [], set()

    def add(kind, home, hc, hg, ag, away, ac, text, stage=None, link=None):
        key = (common.norm(home), hc.upper(), int(hg), int(ag), common.norm(away), ac.upper())
        if key in seen:
            return
        seen.add(key)
        out.append({"source": SITE, "competition": competition, "kind": kind, "stage": stage,
                    "home": home.strip(), "home_country": hc.upper(), "away": away.strip(), "away_country": ac.upper(),
                    "home_goals": int(hg), "away_goals": int(ag), "text": text, "source_url": link or url,
                    "fetched_at": fetched_at})

    for b in _text_blocks(html):
        for m in HEADLINE_RE.finditer(b):
            add("headline", m["home"], m["hc"], m["hg"], m["ag"], m["away"], m["ac"], m.group(0).strip(),
                stage=(m["stage"] or None))
    for m in SLUG_RE.finditer(htmllib.unescape(html)):
        add("video_slug", m["home"].replace("-", " "), m["hc"], m["hg"], m["ag"], m["away"].replace("-", " "),
            m["ac"], m.group(0), link=f"{url}{m.group(0)}")
    return out


def fetch_scores(competitions=("acle", "acl2"), fetcher: PoliteFetcher | None = None, ttl_s: int = 3600) -> dict:
    """每个赛事 1 次请求。返回 {"claims": [...], "pages": {comp: {url, ok, error}}}。"""
    fetcher = fetcher or PoliteFetcher.from_config(SITE)
    claims, pages = [], {}
    for comp in competitions:
        url = COMP_PAGES[comp]
        try:
            text, meta = fetcher.fetch(url, ttl_s=ttl_s)
            got = parse_scores(text, url, comp, meta.get("fetched_at"))
            claims += got
            pages[comp] = {"url": url, "ok": True, "claims": len(got), "from_cache": meta.get("from_cache")}
        except Exception as e:  # noqa: BLE001 —— 逐页记录失败，不中断其他赛事
            pages[comp] = {"url": url, "ok": False, "error": f"{type(e).__name__}: {e}"}
    if any(p["ok"] for p in pages.values()):
        common.ok(SITE, {"score_claims": len(claims)})
    return {"claims": claims, "pages": pages}
