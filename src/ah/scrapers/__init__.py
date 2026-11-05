"""官网首发/伤停爬虫（数据官负责）。

原则（见 config/sources.yaml 的 scrapers 段与路线图「爬虫补充数据源」）：
- 先读 robots.txt（支持 * 和 $ 通配），被禁止的路径一律不抓；条款明确禁止自动化访问的站点不接。
- 同一站点请求间隔 >= min_interval_s（默认 10 秒，即每分钟 <= 6 次），诚实 UA，不伪装浏览器。
- 原始响应缓存到 data/cache/scrapers/<site>/，带 ETag / If-Modified-Since 条件请求。
- 429/5xx 指数退避重试（尊重 Retry-After）；401/403/验证码页直接判失败，不绕过。
- 解析结果过 schema/lineup.schema.json；结构变化时写 data/source_status.json 并抛 StructureChanged。
- 只记录页面上真实存在的数据；拿不到的字段为 null 并在 field_status 里标「暂无」，绝不编造。
"""
from __future__ import annotations


class ScraperError(RuntimeError):
    """爬虫通用错误。"""


class RobotsDisallowed(ScraperError):
    """robots.txt 禁止该路径。"""


class SourceDisabled(ScraperError):
    """站点因条款/合规原因被停用（不发请求）。"""


class SiteUnavailable(ScraperError):
    """站点拦截请求、要求登录、或数据依赖 JS 渲染导致拿不到（不硬抓）。"""


class StructureChanged(ScraperError):
    """页面结构与解析器预期不一致（疑似改版）。"""
