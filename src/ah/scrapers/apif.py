"""爬虫入口用的 API-Football 访问（先读本地缓存，再按需调用；统计本次消耗）。Key 只经 secrets.get_secret 读取，不打印。"""
from __future__ import annotations

import json
from pathlib import Path

from ..api_football import FINISHED, Client


class Api:
    def __init__(self, allow_api: bool = True, client: Client | None = None):
        self.allow_api = allow_api
        self._client = client
        self.calls = 0

    @property
    def client(self) -> Client:
        if self._client is None:
            from ..secrets import get_secret
            self._client = Client(key=get_secret("API_FOOTBALL_KEY") or "")
        return self._client

    def _get(self, endpoint: str, params: dict, **kw) -> dict:
        if not self.allow_api:
            raise LookupError(f"本地无缓存且已禁止调用 API-Football（{endpoint} {params}）")
        before = self.client.usage().get("used", 0)
        try:
            return self.client.get(endpoint, params, **kw)
        finally:
            self.calls += max(0, self.client.usage().get("used", 0) - before)

    def _read(self, path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))["data"]
        except (OSError, ValueError, KeyError):
            return None

    # ---------- fixture ----------
    def cached_fixture(self, fid: int) -> dict | None:
        """按「完场结果缓存 -> 各日期赛程缓存（取最新写入的）」顺序找；不发请求。"""
        c = self.client
        d = self._read(c._cache_path("result", {"id": fid}))
        if d and d.get("response"):
            return d["response"][0]
        best, best_ts = None, -1.0
        for p in (c.cache_dir / "fixtures").glob("*.json"):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for f in (raw.get("data") or {}).get("response") or []:
                if f["fixture"]["id"] == fid and raw.get("ts", 0) > best_ts:
                    best, best_ts = f, raw.get("ts", 0)
        return best

    def fixture(self, fid: int, need_final: bool = False) -> tuple[dict | None, str]:
        """need_final=True（比分核对）：缓存里不是完场状态就调 /fixtures?id=（1 次；完场后永久缓存）。"""
        f = self.cached_fixture(fid)
        if f and (not need_final or f["fixture"]["status"]["short"] in FINISHED):
            return f, "cache"
        if not self.allow_api:
            return f, "cache-stale" if f else "none"
        d = self._get("fixtures", {"id": fid}, ttl_key="result", essential=False)
        r = d.get("response") or []
        return (r[0] if r else None), "api"

    def fixtures_by_date(self, date: str) -> tuple[list, str]:
        params = {"date": date, "timezone": "UTC"}
        if self.allow_api:
            return self._get("fixtures", params, force=True).get("response") or [], "api"
        d = self._read(self.client._cache_path("fixtures", params))
        return ((d or {}).get("response") or []), ("cache" if d else "none")

    # ---------- lineups ----------
    def lineups(self, fid: int) -> tuple[list | None, str]:
        """非空首发永久缓存；空结果（未公布）不缓存，下次再调。"""
        d = self._read(self.client._cache_path("fixtures/lineups", {"fixture": fid}))
        if d and d.get("response"):
            return d["response"], "cache"
        if not self.allow_api:
            return None, "none"
        return self._get("fixtures/lineups", {"fixture": fid}).get("response") or [], "api"
