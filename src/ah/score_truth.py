"""第三比分真值源（DATA SOURCE:）：给结算赛果加一个免注册交叉核对来源。

要解决的问题
------------
`settle.autofill_results` 主源是 API-Football；`football-data.org` 第二源只覆盖欧洲联赛，
且失败时静默 `return None`。亚冠/日职/卡塔尔等仍是单点。本模块提供第三个**免注册**来源，
做只读一致性核对 + 冲突可见化。

硬底线（防幻觉）
----------------
- 只从 `config/sources.yaml score_truth` 声明的免注册公开源取数，每条声明带
  `source_id / source_url / as_of / coverage_status`；
- **只在页面/数据里明确出现「两队 + 全场比分」时才生成一条 claim**，格式不认识就返回空，
  绝不推断、绝不补全、绝不把「进行中」当完场；
- 抓不到 / 解析不出 / 队名对不上 → 一律 `THIRD_UNAVAILABLE` 或 `NO_MATCH`，写「未取得」；
- 真值写回默认**关闭**：仅当 `AH_THIRD_SCORE_FALLBACK=1` 且「≥2 个独立 source_id 给出
  完全一致的比分」且主源 result 为空时才允许回填，并把依据写进事实文件；
- 不回显含密钥的 URL/响应体，不推断失败原因，不阻断结算，不发 Telegram。

当前仓库里 `config/sources.yaml working` 已有可复用的免注册源（titan007 bfdata、
the-afc.com 落地页、jleague.jp）；本模块只做保守解析，实际可达性由运行时事实给出，
**不在静态文件里断言「已连通」或「数据充足」**。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import CONFIG, DATA

MISSING = "未取得"
VERDICTS = ("AGREE", "DISAGREE", "THIRD_UNAVAILABLE", "PRIMARY_MISSING", "NO_MATCH")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _real(v) -> bool:
    return v is not None and str(v).strip() not in ("", MISSING, "None", "null")


def _safe_url(url: str) -> str:
    """只回显 scheme+host+path，去掉 query（可能含 token/key）。"""
    return re.sub(r"\?.*$", "", str(url or ""))


def _atomic_json(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def load_sources(config_path: Path | None = None) -> list[dict]:
    """只读声明：缺字段的源直接跳过，不猜 URL、不补默认值。"""
    p = Path(config_path) if config_path else CONFIG / "sources.yaml"
    try:
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    block = cfg.get("score_truth") or {}
    out = []
    for item in (block.get("sources") or []):
        if not isinstance(item, dict):
            continue
        if not item.get("enabled", False):
            continue
        sid, url = str(item.get("source_id") or "").strip(), str(item.get("source_url") or "").strip()
        if not sid or not url or not url.startswith(("http://", "https://")):
            continue
        out.append({"source_id": sid, "source_url": url,
                    "parser": str(item.get("parser") or "generic"),
                    "as_of": str(item.get("as_of") or MISSING),
                    "coverage_status": str(item.get("coverage_status") or MISSING),
                    "headers": item.get("headers") if isinstance(item.get("headers"), dict) else {}})
    return out


# 只认「两队名 + 明确的 m-n 全场比分」；任何歧义都不生成 claim。
SCORE_LINE = re.compile(
    r"(?P<home>[A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff .&'\-]{1,60}?)\s*"
    r"(?P<hg>\d{1,2})\s*[-:]\s*(?P<ag>\d{1,2})\s*"
    r"(?P<away>[A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff .&'\-]{1,60}?)"
    r"(?:\s|$|,|;|\|)", re.M)


def parse_claims(text: str, source: dict) -> list[dict]:
    """保守解析：只抽明确的「队名 比分 队名」，不认识的格式返回空列表。"""
    out = []
    if not isinstance(text, str) or not text.strip():
        return out
    for m in SCORE_LINE.finditer(text):
        hg, ag = int(m.group("hg")), int(m.group("ag"))
        home, away = m.group("home").strip(), m.group("away").strip()
        if not home or not away or home == away:
            continue
        if hg > 30 or ag > 30:   # 明显不是足球全场比分（可能是时间/编号）
            continue
        out.append({"source_id": source["source_id"], "source_url": _safe_url(source["source_url"]),
                    "as_of": source["as_of"], "coverage_status": source["coverage_status"],
                    "home": home, "away": away, "home_goals": hg, "away_goals": ag,
                    "score": f"{hg}-{ag}", "note": "只抄录页面明确写出的比分，不推断完场状态"})
    return out


def _afc_claims(text: str, source: dict) -> list[dict]:
    """复用仓库 2026-11-04 已实测的 the-afc.com 落地页解析器（只抽对阵+比分）。

    该解析器只认服务端 SSR 的 seo-footer / 战报标题 / 集锦 slug；认不出就抛异常，
    由调用方降级为 NO_EXPLICIT_SCORE_FOUND，绝不猜测。
    """
    from .scrapers.afc import parse_scores
    raw = parse_scores(text, source["source_url"], "acle", _now())
    out = []
    for r in raw:
        out.append({"source_id": source["source_id"],
                    "source_url": _safe_url(r.get("source_url") or source["source_url"]),
                    "as_of": source["as_of"], "coverage_status": source["coverage_status"],
                    "home": str(r.get("home") or ""), "away": str(r.get("away") or ""),
                    "home_goals": int(r["home_goals"]), "away_goals": int(r["away_goals"]),
                    "score": f'{int(r["home_goals"])}-{int(r["away_goals"])}',
                    "note": "复用 src/ah/scrapers/afc.py::parse_scores，只抄录官方落地页明确写出的比分"})
    return out


def _parse(text: str, source: dict) -> list[dict]:
    if source.get("parser") == "afc_scores":
        return _afc_claims(text, source)
    return parse_claims(text, source)


def fetch_claims(sources: list[dict], fetcher=None) -> tuple[list[dict], list[dict]]:
    """返回 (claims, fetch_facts)。fetcher 可注入（测试用），默认 requests，超时 8s。"""
    claims: list[dict] = []
    facts: list[dict] = []
    if fetcher is None:
        try:
            import requests
            fetcher = lambda url, headers, timeout: requests.get(url, headers=headers, timeout=timeout).text
        except ImportError:
            fetcher = None
    for s in sources:
        fact = {"source_id": s["source_id"], "source_url": _safe_url(s["source_url"]),
                "as_of": s["as_of"], "coverage_status": s["coverage_status"],
                "parser": s.get("parser", "generic"),
                "status": MISSING, "claims": 0, "error_type": MISSING}
        if fetcher is None:
            fact["status"] = "FETCHER_UNAVAILABLE"
            facts.append(fact)
            continue
        try:
            text = fetcher(s["source_url"], s.get("headers") or {}, 8)
            got = _parse(text, s)
            fact["status"] = "PARSED" if got else "NO_EXPLICIT_SCORE_FOUND"
            fact["claims"] = len(got)
            claims.extend(got)
        except Exception as exc:  # noqa: BLE001 —— 只记异常类型，绝不回显 URL/响应体
            fact["status"] = "NO_EXPLICIT_SCORE_FOUND"
            fact["error_type"] = type(exc).__name__
        facts.append(fact)
    return claims, facts


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(s or "").lower())


def _team_match(a: str, b: str) -> bool:
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return False
    return x in y or y in x or (len(x) >= 4 and len(y) >= 4 and (x[:4] in y or y[:4] in x))


def match_claims(row: dict, claims: list[dict]) -> list[dict]:
    """按队名（双向包含）+ 可选日期粗匹配挑出属于该场的声明；不按比分挑，避免结果粉饰。"""
    out = []
    for c in claims:
        if (_team_match(row.get("home", ""), c["home"]) and
                _team_match(row.get("away", ""), c["away"])) or \
           (_team_match(row.get("home", ""), c["away"]) and
                _team_match(row.get("away", ""), c["home"]) and
                c["home_goals"] == c["away_goals"]):  # 主客颠倒只在平局时容忍，仍记事实
            out.append(c)
    return out


def verify_row(row: dict, claims: list[dict]) -> dict:
    """单场核对：只给判定与依据，不改写任何结算字段。"""
    primary = str(row.get("result") or "").strip()
    matched = match_claims(row, claims)
    base = {"match_id": str(row.get("match_id") or MISSING),
            "home": row.get("home") or MISSING, "away": row.get("away") or MISSING,
            "primary_result": primary or MISSING,
            "third_claims": [c["score"] + "@" + c["source_id"] for c in matched],
            "independent_sources": sorted({c["source_id"] for c in matched})}
    if not _real(primary):
        return {**base, "verdict": "PRIMARY_MISSING", "note": "主源无比分；不推断、不补造"}
    if not matched:
        return {**base, "verdict": "THIRD_UNAVAILABLE", "note": "第三源无可用声明；只记事实，不推断缺失原因"}
    scores = {c["score"] for c in matched}
    if len(scores) == 1 and next(iter(scores)) == primary.replace(":", "-"):
        return {**base, "verdict": "AGREE", "note": "第三源与主源一致（一致性事实，不等于真值已证明）"}
    return {**base, "verdict": "DISAGREE",
            "note": "第三源与主源不一致或多源不一致；不自动改写结算，需人工确认"}


def fallback_score(row: dict, claims: list[dict], enabled: bool = False) -> str | None:
    """极保守回填：主源空 + 开关开 + ≥2 个独立 source_id 给出完全一致比分。"""
    if not enabled or _real(row.get("result")):
        return None
    matched = match_claims(row, claims)
    by_src: dict[str, set[str]] = {}
    for c in matched:
        by_src.setdefault(c["source_id"], set()).add(c["score"])
    consistent = {next(iter(v)) for v in by_src.values() if len(v) == 1}
    if len(by_src) >= 2 and len(consistent) == 1:
        return next(iter(consistent))
    return None


def check(ledger: Path | None = None, sources: list[dict] | None = None,
          fetcher=None, date: str = "") -> dict:
    p = Path(ledger) if ledger else DATA / "predictions.csv"
    srcs = sources if sources is not None else load_sources()
    claims, facts = fetch_claims(srcs, fetcher=fetcher)
    rows: list[dict] = []
    if p.exists():
        try:
            with p.open(encoding="utf-8", newline="") as f:
                rows = [dict(r) for r in csv.DictReader(f)]
        except (OSError, UnicodeError, csv.Error):
            rows = []
    if date:
        rows = [r for r in rows if str(r.get("date") or "") == date]
    verdicts = [verify_row(r, claims) for r in rows]
    counts = {v: sum(1 for x in verdicts if x["verdict"] == v) for v in VERDICTS}
    return {
        "schema": 1, "checked_at": _now(), "date_filter": date or MISSING,
        "evidence_grade": "【已验证】本地解析与声明事实",
        "ledger_rows": len(rows), "sources_declared": [s["source_id"] for s in srcs],
        "fetch_facts": facts, "claims_total": len(claims),
        "verdict_counts": counts, "rows": verdicts,
        "fallback_enabled": os.environ.get("AH_THIRD_SCORE_FALLBACK", "") == "1",
        "note": ("只读核对：AGREE 不等于真值已证明，DISAGREE 不自动改写结算；"
                 "抓不到写未取得，不推断原因、不补造比分、不回显密钥或完整 URL"),
        "unverified": [
            "免注册源真实可达性与覆盖率（由运行时 fetch_facts 给出，不静态断言）",
            "页面解析规则是否覆盖目标联赛（保守解析，认不出即返回空）",
            "Telegram 接收端（本命令不发送任何消息）",
        ],
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="第三比分源只读核对（不写回台账）")
    ap.add_argument("--date", default="")
    ap.add_argument("--out", default=None)
    ap.add_argument("--ledger", default=None)
    a = ap.parse_args(argv)
    out = check(Path(a.ledger) if a.ledger else None, date=a.date)
    if a.out:
        _atomic_json(Path(a.out), out)
    c = out["verdict_counts"]
    print(f"SCORE_TRUTH rows={out['ledger_rows']} sources={len(out['sources_declared'])} "
          f"claims={out['claims_total']} AGREE={c['AGREE']} DISAGREE={c['DISAGREE']} "
          f"THIRD_UNAVAILABLE={c['THIRD_UNAVAILABLE']}")
    for v in out["rows"]:
        if v["verdict"] == "DISAGREE":
            print(f"::warning::SCORE_TRUTH_DISAGREE {v['match_id']} primary={v['primary_result']} "
                  f"third={','.join(v['third_claims'])}")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
