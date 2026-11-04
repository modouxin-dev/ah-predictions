"""J.League 官网 (www.jleague.jp) 比赛页首发解析。

数据来源：比赛详情页服务端渲染的 Next.js RSC 数据（HTML 内 self.__next_f.push(...) 片段），
即页面本身携带的官方 JSON，不调用任何未公开接口。默认抓英文页 /en/match/<赛事>/<年>/<id>/，
球员名为罗马字，便于与 API-Football 核对。覆盖 J1/J2/J3、联赛杯，以及日本球队参加的 ACLE/ACL2。

能拿到：开赛时间、主客队（含站点 teamId）、比赛状态、双方首发 XI（位置+号码+姓名）。
拿不到：替补（控え）和停赛（出場停止）——它们在 Lineup 标签页由前端 JS 加载，SSR 不含；
按规定不做 JS 渲染/不调内部接口，记为「暂无」。伤病名单官网不公布。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from .. import DATA
from ..match import find_fixture, parse_dt
from . import common
from .common import AVAILABLE, MISSING
from .http import PoliteFetcher, load_site_config, utcnow_iso

SITE = "jleague"
BASE = "https://www.jleague.jp"
OUT_DIR = DATA / "snapshots" / "lineups"
MATCH_PATH = re.compile(r"^/(?:en/)?match/(?P<comp>[a-z0-9-]+)/(?P<year>\d{4})/(?P<id>\d{6})/?$")
STATUS_MAP = {"pre-game": "pre-game", "in-game": "in-game", "post-game": "post-game",
              "cancelled": "cancelled", "cancelled-pre-game": "cancelled"}
HEADER_KEYS = {"type", "tournament", "homeTeam", "awayTeam", "date"}
TEAM_KEYS = {"name"}
PLAYER_KEYS = {"name", "position"}
POS_RE = re.compile(r"^(GK|DF|MF|FW)\s+(\d{1,3})$")
SUBS_REASON = "J.League 官网的控え（替补）在 Lineup 标签页由前端 JS 加载，服务端 HTML 不含；不做 JS 渲染抓取"
ABS_REASON = "J.League 官网不公布伤病名单；出場停止（停赛）在 Lineup 标签页由前端 JS 加载，服务端 HTML 不含"


def match_url(comp: str, year: str | int, mid: str, lang: str = "en") -> str:
    prefix = "/en" if lang == "en" else ""
    return f"{BASE}{prefix}/match/{comp}/{year}/{mid}/"


def extract_flight(html: str) -> str:
    """拼接页面内所有 self.__next_f.push([1, "<chunk>"]) 的字符串片段。"""
    out = []
    for m in re.finditer(r"self\.__next_f\.push\((\[.*?\])\)\s*</script>", html, re.S):
        try:
            arr = json.loads(m.group(1))
        except ValueError:
            continue
        if len(arr) > 1 and isinstance(arr[1], str):
            out.append(arr[1])
    return "".join(out)


def _decode_at(text: str, start: int):
    try:
        obj, _ = json.JSONDecoder().raw_decode(text, start)
        return obj
    except ValueError:
        return None


def find_header(flight: str) -> dict | None:
    for m in re.finditer(r'\{"type"\s*:\s*"[a-z-]+"', flight):
        obj = _decode_at(flight, m.start())
        if isinstance(obj, dict) and HEADER_KEYS <= obj.keys():
            return obj
    return None


def find_lineup(flight: str) -> dict | None:
    for m in re.finditer(r'"homeTeam":\{', flight):
        for start in range(m.start() - 1, max(m.start() - 80, -1), -1):
            if flight[start] != "{":
                continue
            obj = _decode_at(flight, start)
            if isinstance(obj, dict) and isinstance(obj.get("homeTeam"), dict) \
                    and isinstance(obj["homeTeam"].get("players"), list) \
                    and isinstance(obj.get("awayTeam"), dict):
                return obj
    return None


WATCH_HEADER = {"type", "tournament", "tournamentName", "homeTeam", "awayTeam", "date", "section", "stadium"}
WATCH_TEAM = {"name", "teamId", "score", "nameS", "code"}
WATCH_PLAYER = {"name", "position"}


def signature(header: dict, lineup: dict | None) -> str:
    """结构指纹：只看我们依赖/关注的键是否存在（不看取值，也不看随比赛变化的可选键，如 liveMatchInfo、href、
    legacyPlayerPhotoLookup），避免误报；指纹变化说明页面结构改了。"""
    parts = [sorted(WATCH_HEADER & header.keys()), sorted(WATCH_TEAM & header["homeTeam"].keys())]
    if lineup:
        pl = lineup["homeTeam"]["players"]
        parts += [sorted({"homeTeam", "awayTeam"} & lineup.keys()), sorted(WATCH_PLAYER & pl[0].keys()) if pl else []]
    return hashlib.sha1(json.dumps(parts).encode()).hexdigest()[:12]


def _players(raw: list, side: str) -> list[dict]:
    out = []
    for p in raw:
        if not isinstance(p, dict) or not PLAYER_KEYS <= p.keys():
            raise common.structure_error(SITE, f"{side} 球员条目缺少 {sorted(PLAYER_KEYS)}")
        m = POS_RE.match(str(p.get("position") or "").strip())
        if not m:
            raise common.structure_error(SITE, f"{side} 位置/号码格式异常: {str(p.get('position'))[:20]!r}")
        lk = p.get("legacyPlayerPhotoLookup") or {}
        out.append({"name": (p.get("name") or "").strip() or None, "number": int(m.group(2)), "pos": m.group(1),
                    "site_player_id": lk.get("playerId")})
    return out


def parse_match(html: str, url: str, fetched_at: str | None = None, raw_cache: str | None = None) -> dict:
    """解析比赛页 -> 统一 schema 记录（match_id 先留空）。结构异常抛 StructureChanged 并告警。"""
    path = re.sub(r"^https?://[^/]+", "", url)
    pm = MATCH_PATH.match(path)
    if not pm:
        raise ValueError(f"不是 J.League 比赛页: {url}")
    flight = extract_flight(html)
    if not flight:
        raise common.structure_error(SITE, "页面不含 __next_f 数据（可能不再服务端渲染）")
    header = find_header(flight)
    if not header:
        raise common.structure_error(SITE, "找不到比赛头部对象（type/tournament/homeTeam/awayTeam/date）")
    for side in ("homeTeam", "awayTeam"):
        if not isinstance(header[side], dict) or not TEAM_KEYS <= header[side].keys():
            raise common.structure_error(SITE, f"头部 {side} 缺少 name")
    raw_date = str(header["date"]).removeprefix("$D")
    try:
        ko = parse_dt(raw_date)
    except ValueError:
        raise common.structure_error(SITE, f"开赛时间格式异常: {raw_date[:30]!r}") from None
    ko_utc, ko_bjt = common.iso_pair(ko)
    status = STATUS_MAP.get(header["type"], "unknown")

    lineup = find_lineup(flight)
    sides = {}
    fs_xi = {"status": MISSING, "reason": ""}
    if lineup:
        home_xi = _players(lineup["homeTeam"]["players"], "home")
        away_xi = _players(lineup["awayTeam"]["players"], "away")
        for side, xi in (("home", home_xi), ("away", away_xi)):
            if len(xi) != 11:
                raise common.structure_error(SITE, f"{side} 首发 {len(xi)} 人（应为 11）")
        sides = {"home": home_xi, "away": away_xi}
        fs_xi = {"status": AVAILABLE, "reason": "官网比赛页服务端数据（首发公布后才有）"}
        unnamed = sum(1 for xi in sides.values() for p in xi if not p["name"])
        if unnamed:
            fs_xi["reason"] += f"；官网有 {unnamed} 名首发球员未给出姓名（保留号码，姓名为 null）"
    elif status == "pre-game":
        fs_xi["reason"] = "首发未公布（官网通常开赛前约 1 小时公布）"
    else:
        fs_xi["reason"] = "页面未包含首发数据"

    rec = {
        "source": SITE,
        "site_match_id": f"{pm['comp']}/{pm['year']}/{pm['id']}",
        "match_id": None,
        "competition": header.get("tournament") or pm["comp"],
        "competition_name": header.get("tournamentName"),
        "round": header.get("section"),
        "status": status,
        "kickoff_utc": ko_utc,
        "kickoff_bjt": ko_bjt,
        "home": common.normalize_team(header["homeTeam"]["name"], str(header["homeTeam"].get("teamId") or "") or None),
        "away": common.normalize_team(header["awayTeam"]["name"], str(header["awayTeam"].get("teamId") or "") or None),
        "score": ({"home": header["homeTeam"].get("score"), "away": header["awayTeam"].get("score")}
                  if status in ("in-game", "post-game") else None),
        "lineups": {s: {"starting_xi": sides.get(s), "substitutes": None, "absences": None} for s in ("home", "away")},
        "field_status": {"starting_xi": fs_xi,
                         "substitutes": {"status": MISSING, "reason": SUBS_REASON},
                         "absences": {"status": MISSING, "reason": ABS_REASON}},
        "source_url": url,
        "fetched_at": fetched_at or utcnow_iso(),
        "raw_cache": raw_cache,
        "structure_signature": signature(header, lineup),
    }
    return common.validate(rec, SITE)


def discover(html: str, comps: set[str] | None = None) -> list[str]:
    """从日程页提取比赛路径 /match/<comp>/<year>/<id>/（去重、保序）。"""
    seen, out = set(), []
    for m in re.finditer(r'href="(/(?:en/)?match/[a-z0-9-]+/\d{4}/\d{6}/)"', html):
        pm = MATCH_PATH.match(m.group(1))
        key = (pm["comp"], pm["year"], pm["id"])
        if key in seen or (comps and pm["comp"] not in comps):
            continue
        seen.add(key)
        out.append(f"{pm['comp']}/{pm['year']}/{pm['id']}")
    return out


def map_fixture(rec: dict, api_fixtures: list) -> dict:
    """用开赛时间(±15 分钟)+队名别名/模糊匹配对上 API-Football fixture id；对不上保持 None。"""
    f = find_fixture(api_fixtures or [], rec["home"]["name"], rec["away"]["name"], rec["kickoff_utc"])
    if f is None:  # 再用站点原名试一次
        f = find_fixture(api_fixtures or [], rec["home"]["site_name"], rec["away"]["site_name"], rec["kickoff_utc"])
    rec["match_id"] = int(f["fixture"]["id"]) if f else None
    return rec


def _check_signature(fetcher: PoliteFetcher, rec: dict) -> None:
    p = fetcher.cache_dir / "structure.json"
    try:
        known = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    key = f'{rec["status"]}:' + ("with_lineup" if rec["lineups"]["home"]["starting_xi"] else "no_lineup")
    old = known.get(key)
    if old and old != rec["structure_signature"]:
        common.alert(SITE, f"页面结构指纹变化（{key}: {old} -> {rec['structure_signature']}），解析仍通过，请人工复核")
    known[key] = rec["structure_signature"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(known, indent=1), encoding="utf-8")


def scrape_match(site_match_id: str, *, fetcher: PoliteFetcher | None = None, api_fixtures: list | None = None,
                 ttl_s: float = 0, out_dir: Path | None = OUT_DIR) -> dict:
    """抓取一场比赛页 -> 记录（并写入 data/snapshots/lineups/<北京日期>/）。"""
    comp, year, mid = site_match_id.strip("/").split("/")
    fetcher = fetcher or PoliteFetcher.from_config(SITE)
    url = match_url(comp, year, mid)
    try:
        html, meta = fetcher.fetch(url, ttl_s=ttl_s)
    except Exception as e:  # noqa: BLE001
        common.alert(SITE, f"{type(e).__name__}: {e}")
        raise
    rec = parse_match(html, url, fetched_at=meta.get("fetched_at"), raw_cache=meta.get("body_file"))
    if api_fixtures is not None:
        map_fixture(rec, api_fixtures)
    _check_signature(fetcher, rec)
    common.ok(SITE, {"last_url": url})
    if out_dir is not None:
        write_record(rec, out_dir)
    return rec


def write_record(rec: dict, out_dir: Path = OUT_DIR) -> Path:
    day = rec["kickoff_bjt"][:10]
    p = Path(out_dir) / day / f"{rec['source']}-{rec['site_match_id'].replace('/', '-')}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return p


def config() -> dict:
    return load_site_config(SITE)


def kickoff_dt(rec: dict) -> datetime:
    return parse_dt(rec["kickoff_utc"])
