"""官网首发 vs API-Football 首发逐场核对。

判定（每队、整场）：
- 已验证：两源首发 11 人号码集合一致，且同号球员姓名可对应（罗马字/缩写差异容忍）。
- 冲突：号码集合不同，或同号姓名明显不同；写明两边来源和差异，不进入推荐。
  姓名规则（先知 2026-11-04 审定）：同队同号，姓名有共同词 -> 一致；否则取姓氏（小写、去变音符号与标点；
  「HAYASHI Haruki」取全大写段，「H. Hayashu」取缩写之后的段，其余取最后一段）比编辑距离，
  ≤ 2 -> 判已验证并标注 name_variant；其他情况仍判冲突。
- 暂无：任一来源缺失（未公布、未对上 fixture、API 未返回）。
- unverified：没有官网来源（A-Leagues/AFC 比赛中心需 JS、沙特联条款禁止爬虫、亚冠非日本球队无 J.League 映射等），
  只有 API-Football 单一来源的首发；可用但未经第二来源验证，与上面三种区分开。
绝不补全或猜测缺失数据。
"""
from __future__ import annotations

import re
import unicodedata

from ..match import team_similarity
from .http import utcnow_iso

VERIFIED, CONFLICT, MISSING = "已验证", "冲突", "暂无"
UNVERIFIED = "unverified"
VERDICTS = (VERIFIED, CONFLICT, MISSING, UNVERIFIED)


def _name_tokens(name: str | None) -> list[str]:
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return [t for t in re.split(r"[^a-z0-9]+", s) if len(t) > 1]


def names_compatible(a: str | None, b: str | None) -> bool | None:
    """None = 至少一边没有姓名，无法判断。"""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return None
    if set(ta) & set(tb):
        return True
    ca, cb = "".join(ta), "".join(tb)
    return ca in cb or cb in ca


MAX_SURNAME_DISTANCE = 2


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def surname(name: str | None) -> str:
    """取姓氏并标准化（小写、去变音符号和标点）。
    规则：有全大写段且不是整名全大写 -> 全大写段（「HAYASHI Haruki」-> hayashi）；
    有缩写（单字母或带点，如「H.」）-> 最后一个缩写之后的段（「H. Hayashu」-> hayashu）；否则最后一段。"""
    toks = [t for t in _fold(name).split() if re.sub(r"[^A-Za-z]", "", t)]
    if not toks:
        return ""
    letters = [re.sub(r"[^A-Za-z]", "", t) for t in toks]
    upper = [i for i, t in enumerate(letters) if len(t) >= 2 and t.isupper()]
    if upper and len(upper) < len(toks):
        picked = [letters[i] for i in upper]
    else:
        abbr = [i for i, t in enumerate(toks) if len(letters[i]) == 1 or (t.endswith(".") and len(letters[i]) <= 3)]
        if abbr and abbr[-1] < len(toks) - 1:
            picked = letters[abbr[-1] + 1:]
        else:
            picked = letters[-1:]
    return "".join(picked).lower()


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def name_variant(a: str | None, b: str | None) -> dict | None:
    """同号姓名不一致时的姓氏容错：姓氏编辑距离 ≤ 2 返回标注明细，否则 None。"""
    sa, sb = surname(a), surname(b)
    if not sa or not sb:
        return None
    d = edit_distance(sa, sb)
    return {"site_surname": sa, "api_surname": sb, "distance": d} if d <= MAX_SURNAME_DISTANCE else None


def _api_xi(team_entry: dict) -> list[dict]:
    return [{"name": (x.get("player") or {}).get("name"), "number": (x.get("player") or {}).get("number"),
             "pos": (x.get("player") or {}).get("pos")} for x in team_entry.get("startXI") or []]


def _compare_team(site_xi, api_xi) -> dict:
    if not site_xi and api_xi and len(api_xi) == 11:
        return {"verdict": UNVERIFIED, "reason": "官网尚未公布首发，仅 API-Football 单一来源", "api_count": len(api_xi)}
    if not site_xi or not api_xi:
        return {"verdict": MISSING, "reason": "官网未提供首发" if not site_xi else "API-Football 未提供首发"}
    s_by = {p["number"]: p for p in site_xi}
    a_by = {p["number"]: p for p in api_xi}
    only_site = sorted(set(s_by) - set(a_by), key=lambda x: (x is None, x))
    only_api = sorted(set(a_by) - set(s_by), key=lambda x: (x is None, x))
    mism, unknown, variants = [], [], []
    for n in sorted(set(s_by) & set(a_by), key=lambda x: (x is None, x)):
        c = names_compatible(s_by[n]["name"], a_by[n]["name"])
        if c is False:
            v = name_variant(s_by[n]["name"], a_by[n]["name"])
            if v:
                variants.append({"number": n, "site": s_by[n]["name"], "api_football": a_by[n]["name"], **v})
            else:
                mism.append({"number": n, "site": s_by[n]["name"], "api_football": a_by[n]["name"]})
        elif c is None:
            unknown.append(n)
    out = {"verdict": VERIFIED if not (only_site or only_api or mism) else CONFLICT,
           "site_count": len(site_xi), "api_count": len(api_xi)}
    if only_site or only_api:
        out["only_site"] = [{"number": n, "name": s_by[n]["name"]} for n in only_site]
        out["only_api_football"] = [{"number": n, "name": a_by[n]["name"]} for n in only_api]
    if mism:
        out["name_mismatch"] = mism
    if variants:
        out["name_variant"] = variants          # 姓氏拼写差异（编辑距离 ≤ 2），按已验证处理
    if unknown:
        out["name_unchecked_numbers"] = sorted(unknown)
    return out


def crosscheck_lineup(rec: dict, api_lineups: list | None) -> dict:
    """rec：官网记录（ah.scrapers 统一 schema）；api_lineups：API-Football /fixtures/lineups 的 response 列表。"""
    fid = rec.get("match_id")
    res = {"match_id": fid, "site_match_id": rec["site_match_id"], "home": rec["home"]["name"],
           "away": rec["away"]["name"], "kickoff_bjt": rec["kickoff_bjt"], "checked_at": utcnow_iso(),
           "sources": {rec["source"]: rec["source_url"],
                       "api_football": f"API-Football /fixtures/lineups?fixture={fid}" if fid else None},
           "teams": {}}
    if fid is None:
        res["verdict"] = MISSING
        res["reason"] = "官网比赛未对上 API-Football fixture id"
        return res
    api_by_side: dict[str, dict] = {}
    for entry in api_lineups or []:
        tname = (entry.get("team") or {}).get("name", "")
        best = max(("home", "away"), key=lambda s: max(team_similarity(tname, rec[s]["name"]),
                                                       team_similarity(tname, rec[s]["site_name"])))
        if max(team_similarity(tname, rec[best]["name"]), team_similarity(tname, rec[best]["site_name"])) >= 0.75:
            api_by_side[best] = entry
    for side in ("home", "away"):
        site_xi = rec["lineups"][side]["starting_xi"]
        api_xi = _api_xi(api_by_side[side]) if side in api_by_side else None
        res["teams"][side] = _compare_team(site_xi, api_xi)
    verdicts = {t["verdict"] for t in res["teams"].values()}
    res["verdict"] = next(v for v in (CONFLICT, MISSING, UNVERIFIED, VERIFIED) if v in verdicts)
    variants = [{"side": side, **v} for side, t in res["teams"].items() for v in t.get("name_variant", [])]
    if variants:
        res["name_variant"] = variants
    if res["verdict"] == UNVERIFIED:
        res["reason"] = "官网尚未公布首发，仅 API-Football 单一来源"
    if res["verdict"] == VERIFIED and variants:
        res["reason"] = "两源首发一致；" + "、".join(f'{v["number"]} 号姓名拼写差异（{v["site"]} / {v["api_football"]}）'
                                              for v in variants) + "，按 name_variant 标注"
    if res["verdict"] == CONFLICT:
        res["reason"] = "两源首发不一致，见 teams 明细；不进入推荐"
    # 替补 / 停赛：官网目前拿不到，如实标注
    res["substitutes"] = MISSING if rec["lineups"]["home"]["substitutes"] is None else "未核对"
    res["absences"] = MISSING if rec["lineups"]["home"]["absences"] is None else "未核对"
    return res


def single_source_lineup(fixture: dict, api_lineups: list | None, reason: str) -> dict:
    """只有 API-Football 一个来源时的首发状态：两队都有 11 人首发 -> unverified；否则 暂无。
    fixture：API-Football fixture 原始对象；reason：为何没有官网来源。"""
    from ..match import parse_dt
    from .common import BJT
    fid = fixture["fixture"]["id"]
    home, away = fixture["teams"]["home"]["name"], fixture["teams"]["away"]["name"]
    res = {"match_id": fid, "site_match_id": None, "home": home, "away": away,
           "kickoff_bjt": parse_dt(fixture["fixture"]["date"]).astimezone(BJT).isoformat(), "checked_at": utcnow_iso(),
           "sources": {"api_football": f"API-Football /fixtures/lineups?fixture={fid}", "official": None},
           "official_source": reason, "teams": {}}
    by_side = {}
    for entry in api_lineups or []:
        tname = (entry.get("team") or {}).get("name", "")
        sims = {s: team_similarity(tname, fixture["teams"][s]["name"]) for s in ("home", "away")}
        best = max(sims, key=sims.get)
        if sims[best] >= 0.75:
            by_side[best] = entry
    for side in ("home", "away"):
        xi = _api_xi(by_side[side]) if side in by_side else []
        res["teams"][side] = ({"verdict": UNVERIFIED, "api_count": len(xi), "starting_xi": xi} if len(xi) == 11 else
                              {"verdict": MISSING, "reason": "API-Football 未提供首发" if not xi
                               else f"API-Football 首发只有 {len(xi)} 人"})
    if all(t["verdict"] == UNVERIFIED for t in res["teams"].values()):
        res["verdict"], res["reason"] = UNVERIFIED, f"仅 API-Football 单一来源（{reason}）"
    else:
        res["verdict"], res["reason"] = MISSING, "API-Football 首发未公布或不完整"
    res["substitutes"] = res["absences"] = MISSING
    return res
