"""礼貌抓取：robots 检查、按站点限速、诚实 UA、原始响应缓存、条件请求、退避重试。"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests
import yaml

from .. import CONFIG, DATA
from . import RobotsDisallowed, ScraperError, SiteUnavailable, SourceDisabled
from .robots import Robots

CACHE_ROOT = DATA / "cache" / "scrapers"
DEFAULT_UA = ("ah-predictions-scraper/0.1 (+https://github.com/modouxin-dev/ah-predictions; "
              "modouxin-dev@users.noreply.github.com)")
DEFAULT_MIN_INTERVAL = 10.0   # 秒；= 每分钟 <= 6 次（路线图约定）
ROBOTS_TTL = 24 * 3600
RETRY_STATUSES = {429, 500, 502, 503, 504}
BLOCK_STATUSES = {401, 403, 407, 451}
CHALLENGE_MARKERS = ("cf-challenge", "challenge-platform", "g-recaptcha", "hcaptcha", "captcha-delivery",
                     "Attention Required! | Cloudflare", "px-captcha")


def utcnow_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_site_config(site: str, path: Path | None = None) -> dict:
    p = path or (CONFIG / "sources.yaml")
    data = yaml.safe_load(Path(p).read_text(encoding="utf-8")) or {}
    cfg = (data.get("scrapers") or {}).get(site)
    if cfg is None:
        raise ScraperError(f"config/sources.yaml 未登记爬虫源 {site}")
    return cfg


class PoliteFetcher:
    """一个站点一个实例。session/sleep/now 可注入以便离线测试。"""

    _last_call: dict[str, float] = {}   # host -> 上次真实请求时间（进程内共享）

    def __init__(self, site: str, base_url: str, *, min_interval_s: float = DEFAULT_MIN_INTERVAL,
                 user_agent: str = DEFAULT_UA, cache_dir: Path | None = None, session=None,
                 max_retries: int = 3, backoff_base_s: float = 2.0, timeout: float = 30.0,
                 enabled: bool = True, disabled_reason: str = "", cache_raw: bool = True,
                 sleep=time.sleep, now=time.time):
        self.site = site
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = float(min_interval_s)
        self.user_agent = user_agent
        self.cache_dir = Path(cache_dir or CACHE_ROOT / site)
        self.session = session or requests.Session()
        self.max_retries = int(max_retries)
        self.backoff_base_s = float(backoff_base_s)
        self.timeout = timeout
        self.enabled = enabled
        self.disabled_reason = disabled_reason
        self.cache_raw = cache_raw      # False：页面正文只在内存处理、不落盘（robots.txt 除外），也就没有 TTL/条件请求
        self.sleep = sleep
        self.now = now
        self.requests_made = 0
        self._robots: Robots | None = None

    @classmethod
    def from_config(cls, site: str, cfg: dict | None = None, **kw) -> "PoliteFetcher":
        cfg = cfg or load_site_config(site)
        return cls(site, cfg["base_url"], min_interval_s=cfg.get("min_interval_s", DEFAULT_MIN_INTERVAL),
                   user_agent=cfg.get("user_agent", DEFAULT_UA), max_retries=cfg.get("max_retries", 3),
                   backoff_base_s=cfg.get("backoff_base_s", 2.0), enabled=bool(cfg.get("enabled", True)),
                   disabled_reason=cfg.get("disabled_reason", ""), cache_raw=bool(cfg.get("cache_raw", True)), **kw)

    # ---------- 缓存 ----------
    def _paths(self, url: str) -> tuple[Path, Path]:
        h = hashlib.sha1(url.encode()).hexdigest()[:16]
        return self.cache_dir / f"{h}.body", self.cache_dir / f"{h}.meta.json"

    def cached(self, url: str) -> tuple[str, dict] | None:
        body, meta = self._paths(url)
        if not self.cache_raw and not url.endswith("/robots.txt"):
            body.unlink(missing_ok=True)        # 不落盘的站点：清掉以前留下的正文，不从正文缓存读
            return None
        if not (body.exists() and meta.exists()):
            return None
        return body.read_text(encoding="utf-8"), json.loads(meta.read_text(encoding="utf-8"))

    def _store(self, url: str, text: str, resp) -> dict:
        body, meta = self._paths(url)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        keep = self.cache_raw or url.endswith("/robots.txt")
        if keep:
            body.write_text(text, encoding="utf-8")
        else:
            body.unlink(missing_ok=True)
        m = {"url": url, "status": resp.status_code, "fetched_at": utcnow_iso(self.now()), "ts": self.now(),
             "etag": resp.headers.get("ETag"), "last_modified": resp.headers.get("Last-Modified"),
             "content_type": resp.headers.get("Content-Type"),
             "sha256": hashlib.sha256(text.encode()).hexdigest(), "body_file": str(body) if keep else None}
        meta.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        return m

    # ---------- robots ----------
    def robots(self) -> Robots:
        if self._robots is not None:
            return self._robots
        url = f"{self.base_url}/robots.txt"
        hit = self.cached(url)
        if hit and self.now() - hit[1].get("ts", 0) < ROBOTS_TTL:
            self._robots = Robots(hit[0])
            return self._robots
        r = self._request(url, {})
        if r.status_code == 404:
            text = ""          # 无 robots.txt = 全部允许（RFC 9309）
        elif r.status_code != 200:
            raise SiteUnavailable(f"{self.site} robots.txt HTTP {r.status_code}，按不可抓处理")
        else:
            text = r.text
        self._store(url, text, r)
        self._robots = Robots(text)
        return self._robots

    def allowed(self, url: str) -> bool:
        return self.robots().can_fetch(self.user_agent, url)

    # ---------- 请求 ----------
    def _throttle(self, host: str) -> None:
        last = self._last_call.get(host)
        if last is not None:
            wait = self.min_interval_s - (self.now() - last)
            if wait > 0:
                self.sleep(wait)
        self._last_call[host] = self.now()

    def _retry_after(self, resp, attempt: int) -> float:
        ra = resp.headers.get("Retry-After") if resp is not None else None
        if ra:
            try:
                return min(float(ra), 300.0)
            except ValueError:
                try:
                    return min(max((parsedate_to_datetime(ra).timestamp() - self.now()), 0.0), 300.0)
                except (TypeError, ValueError):
                    pass
        return self.backoff_base_s * (2 ** attempt)

    def _request(self, url: str, headers: dict):
        host = urlsplit(url).netloc
        h = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate", **headers}
        last_exc = None
        for attempt in range(self.max_retries + 1):
            self._throttle(host)
            try:
                r = self.session.get(url, headers=h, timeout=self.timeout)
                self.requests_made += 1
            except requests.RequestException as e:   # 网络错误也退避重试
                last_exc, r = e, None
            if r is not None and r.status_code not in RETRY_STATUSES:
                return r
            if attempt < self.max_retries:
                self.sleep(self._retry_after(r, attempt))
        if r is not None:
            return r
        raise ScraperError(f"{self.site} 请求失败（已重试 {self.max_retries} 次）: {type(last_exc).__name__}")

    def fetch(self, url: str, *, ttl_s: float = 0) -> tuple[str, dict]:
        """返回 (正文, 元数据)。meta['from_cache'] 标明是否未发请求/304。"""
        if not self.enabled:
            raise SourceDisabled(f"{self.site} 已停用：{self.disabled_reason}")
        if not self.allowed(url):
            raise RobotsDisallowed(f"{self.site} robots.txt 禁止抓取 {urlsplit(url).path}")
        hit = self.cached(url)
        if hit and ttl_s and self.now() - hit[1].get("ts", 0) <= ttl_s:
            return hit[0], {**hit[1], "from_cache": True}
        cond = {}
        if hit:
            if hit[1].get("etag"):
                cond["If-None-Match"] = hit[1]["etag"]
            if hit[1].get("last_modified"):
                cond["If-Modified-Since"] = hit[1]["last_modified"]
        r = self._request(url, cond)
        if r.status_code == 304 and hit:
            meta = {**hit[1], "ts": self.now(), "revalidated_at": utcnow_iso(self.now())}
            self._paths(url)[1].write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            return hit[0], {**meta, "from_cache": True, "not_modified": True}
        if r.status_code in BLOCK_STATUSES:
            raise SiteUnavailable(f"{self.site} HTTP {r.status_code}（拦截/需登录），不绕过")
        if r.status_code != 200:
            raise ScraperError(f"{self.site} HTTP {r.status_code} {urlsplit(url).path}")
        text = r.text
        if any(m in text[:20000] for m in CHALLENGE_MARKERS):
            raise SiteUnavailable(f"{self.site} 返回验证码/反爬挑战页，不绕过")
        meta = self._store(url, text, r)
        return text, {**meta, "from_cache": False}
