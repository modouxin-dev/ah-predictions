"""统一 schema、校验、告警、队名归一。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import jsonschema
import yaml

from .. import CONFIG, SCHEMA, source_status
from ..match import norm
from . import StructureChanged

BJT = timezone(timedelta(hours=8))
AVAILABLE, MISSING = "已获取", "暂无"


@lru_cache(maxsize=1)
def _schema() -> dict:
    return json.loads((SCHEMA / "lineup.schema.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=4)
def _alias_index(path: str | None = None) -> dict[str, str]:
    """{norm(别名或规范名): 规范名原文}。"""
    p = Path(path) if path else CONFIG / "team_aliases.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out: dict[str, str] = {}
    for canon, alist in (data.get("aliases") or {}).items():
        out[norm(canon)] = canon
        for a in alist or []:
            out.setdefault(norm(a), canon)
    return out


def normalize_team(site_name: str, site_team_id: str | None = None, aliases_path: str | None = None) -> dict:
    canon = _alias_index(aliases_path).get(norm(site_name))
    return {"name": canon or site_name, "site_name": site_name, "site_team_id": site_team_id,
            "alias_matched": canon is not None}


def iso_pair(dt: datetime) -> tuple[str, str]:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), dt.astimezone(BJT).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def status_key(site: str) -> str:
    return f"scraper_{site}"


def alert(site: str, message: str) -> None:
    """写入 data/source_status.json（数据源状态页标红）；连续失败计数。"""
    source_status.record(status_key(site), False, message, extra={"inc_failures": True})


def ok(site: str, extra: dict | None = None) -> None:
    source_status.record(status_key(site), True, extra={"reset_failures": True, **(extra or {})})


def structure_error(site: str, message: str) -> StructureChanged:
    msg = f"页面结构疑似改版：{message}"
    alert(site, msg)
    return StructureChanged(f"{site}: {msg}")


def validate(record: dict, site: str) -> dict:
    """过 JSON Schema；失败视为解析失败 -> 告警并抛 StructureChanged。"""
    try:
        jsonschema.validate(record, _schema())
    except jsonschema.ValidationError as e:
        path = "/".join(str(x) for x in e.absolute_path)
        raise structure_error(site, f"schema 校验失败 {path}: {e.message[:160]}") from None
    return record
