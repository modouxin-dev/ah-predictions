"""选场漏斗：只读事实、不猜测、0 场也出表、不夸大结论。"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ah import build_site, funnel


def _report(tmp_path, date, picks=0, watch=None, not_covered=None, notes=None, errors=None):
    d = tmp_path / "reports" / date[:4] / date[5:7]
    d.mkdir(parents=True, exist_ok=True)
    obj = {
        "date": date,
        "generated_at": f"{date}T23:00:00Z",
        "picks": [{"match_id": f"m{i}"} for i in range(picks)],
        "watchlist": watch or [],
        "watched": [],
        "collection_notes": notes or [],
        "collection_errors": errors or [],
        "not_covered": not_covered or "",
    }
    (d / f"{date}.json").write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return tmp_path / "reports"


def _w(cat, league="日职联"):
    return {"match_id": "x", "league": league, "home": "A", "away": "B",
            "withheld_category": cat, "withheld_reason": "逐字保留的原因文本"}


def test_category_whitelist_and_no_guessing(tmp_path):
    reports = _report(tmp_path, "2026-11-09", picks=3, watch=[
        _w("近期状态数据不足"), _w("盘口报价过期，降级后不足"),
        _w("盘口跨来源核验未通过"), _w("基本面与盘口评分不足"),
        _w("盘口过深"), _w("已开球"), _w("深盘+机构分歧"), _w("某个没见过的类别"),
    ])
    day = funnel.build(reports=reports)["days"][0]
    assert day["picks"] == 3 and day["watchlist"] == 8
    assert day["buckets"] == {"data_gap": 2, "verification": 1, "model_judgment": 2,
                              "timing": 1, "unknown": 2}
    # 复合类别与陌生类别都不猜测归属，且原类别名逐字保留
    assert "深盘+机构分歧" in day["categories"] and "某个没见过的类别" in day["categories"]
    assert day["data_gap_by_league"]["日职联"] == 2


def test_zero_pick_day_still_emits_row_and_observation(tmp_path):
    reports = _report(tmp_path, "2026-11-09", picks=0, watch=[
        _w("近期状态数据不足") for _ in range(6)])
    o = funnel.build(reports=reports)
    d = o["days"][0]
    assert d["zero_pick_day"] is True and d["picks"] == 0 and d["watchlist"] == 6
    assert o["summary"]["total_picks"] == 0
    assert "ZERO_PICK_DAY" in o["summary"]["alerts"]
    assert any("0 场推荐日" in x for x in o["summary"]["observations"])


def test_upstream_only_from_report_text_never_from_config(tmp_path):
    # 有完整文本：严格正则取数
    reports = _report(tmp_path, "2026-11-09", picks=1,
                      not_covered="筛查上限 32 场；窗口内共发现 79 场。其余说明。")
    up = funnel.build(reports=reports)["days"][0]["upstream"]
    assert up["window_fixtures"] == 79 and up["screening_cap"] == 32
    assert up["outside_screen"] == 47 and up["status"] == "RECORDED"
    # 只写了一半：整组写「未取得」，不拿 settings 的 32 倒推
    reports = _report(tmp_path, "2026-11-10", picks=1, not_covered="窗口内共发现 60 场。")
    up = funnel.build(reports=reports)["days"][-1]["upstream"]
    assert up["window_fixtures"] == 60
    assert up["screening_cap"] == funnel.MISSING
    assert up["outside_screen"] == funnel.MISSING and up["status"] == "NOT_RECORDED"
    # 完全没有文本
    reports = _report(tmp_path, "2026-11-11", picks=1)
    assert funnel.build(reports=reports)["days"][-1]["upstream"]["status"] == "NOT_RECORDED"


def test_verification_facts_and_notes_are_verbatim(tmp_path):
    notes = ["API-Football 未启用",
             "近期状态不足：A vs B（两库均不足 3 场）",
             "盘口核验：no_quotes 1场；stale>7200s 20场；verified:0<1 6场。"]
    reports = _report(tmp_path, "2026-11-09", picks=2, notes=notes, errors=["x"])
    d = funnel.build(reports=reports)["days"][0]
    assert d["collection_notes"] == notes
    assert d["state_gap_note_count"] == 1
    assert d["verification_facts"] == ["no_quotes 1场", "stale>7200s 20场", "verified:0<1 6场。"]
    assert d["collection_errors"] == 1


def test_persistent_data_gap_is_a_fact_sentence(tmp_path):
    reports = _report(tmp_path, "2026-11-09", picks=1, watch=[_w("近期状态数据不足") for _ in range(9)])
    _report(tmp_path, "2026-11-10", picks=1, watch=[_w("近期状态数据不足") for _ in range(10)])
    o = funnel.build(reports=reports)
    assert any("连续 2 天" in x and "【已验证】" in x for x in o["summary"]["observations"])
    assert "DATA_GAP_PRESENT" in o["summary"]["alerts"]
    assert o["summary"]["buckets"]["data_gap"] == 19


def test_forbidden_claims_fail_closed(monkeypatch, tmp_path):
    reports = _report(tmp_path, "2026-11-09", picks=1)
    monkeypatch.setattr(funnel, "_observations", lambda days, summary: ["准确率提升，模型更准"])
    with pytest.raises(RuntimeError, match="禁止断言"):
        funnel.build(reports=reports)


def test_cmd_writes_atomically_and_is_pure(tmp_path):
    reports = _report(tmp_path, "2026-11-09", picks=2)
    out = tmp_path / "data" / "selection_funnel.json"
    obj = funnel.cmd(out_path=out, reports=reports,
                     now=datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc))
    assert out.exists() and json.loads(out.read_text(encoding="utf-8")) == obj
    assert obj["checked_at"] == "2026-11-09T12:00:00Z"
    assert not list((tmp_path / "data").glob(".funnel-*"))   # 临时文件已清理
    # 纯函数不写仓库
    assert not (tmp_path / "reports" / "selection_funnel.json").exists()


# ---------- 站点集成 ----------

def test_site_generates_funnel_page_without_fact_file(tmp_path):
    """定时班次还没跑时，站点即时自算，绝不白屏、绝不编造。"""
    reports = _report(tmp_path, "2026-11-09", picks=0, watch=[
        _w("近期状态数据不足") for _ in range(6)],
        not_covered="筛查上限 32 场；窗口内共发现 51 场。")
    data = tmp_path / "data"
    data.mkdir()
    site = tmp_path / "site"
    build_site.build(site=site, reports=reports, data=data,
                     now=datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc),
                     review_docs=tmp_path / "no-such-dir")
    h = (site / "funnel.html").read_text(encoding="utf-8")
    assert "选场漏斗" in h and "按日漏斗" in h and "0 场" in h
    assert "站点即时自算" in h and "19" in h       # 51 - 32
    assert "准确率提升" not in h and "25.0%" not in h
    assert "funnel.html" in (site / "review.html").read_text(encoding="utf-8")
    exported = json.loads((site / "data" / "selection_funnel.json").read_text(encoding="utf-8"))
    assert exported["summary"]["total_picks"] == 0
    assert exported["days"][0]["zero_pick_day"] is True


def test_site_prefers_fact_file_when_present(tmp_path):
    reports = _report(tmp_path, "2026-11-09", picks=0)
    data = tmp_path / "data"
    data.mkdir()
    fact = {"schema": 1, "checked_at": "2026-11-09T01:00:00Z", "note": "只读取报告字段",
            "report_files": 1, "bucket_labels": funnel.BUCKET_CN,
            "days": [{"date": "2026-11-09", "picks": 4, "watchlist": 0, "zero_pick_day": False,
                      "categories": {}, "buckets": {k: 0 for k in funnel.BUCKETS},
                      "data_gap_by_league": {}, "upstream": {"window_fixtures": funnel.MISSING,
                      "screening_cap": funnel.MISSING, "outside_screen": funnel.MISSING,
                      "status": "NOT_RECORDED"},
                      "verification_facts": [], "state_gap_note_count": 0,
                      "collection_notes": [], "collection_errors": 0,
                      "not_covered_text": funnel.MISSING}],
            "summary": {"days": 1, "total_picks": 4, "total_watchlist": 0,
                        "buckets": {k: 0 for k in funnel.BUCKETS}, "data_gap_by_league": {},
                        "max_outside_screen": funnel.MISSING, "observations": [], "alerts": []},
            "unverified": []}
    (data / "selection_funnel.json").write_text(json.dumps(fact, ensure_ascii=False), encoding="utf-8")
    site = tmp_path / "site"
    build_site.build(site=site, reports=reports, data=data,
                     now=datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc),
                     review_docs=tmp_path / "no-such-dir")
    h = (site / "funnel.html").read_text(encoding="utf-8")
    assert "定时班次落盘" in h and "累计发布场次" in h
    assert json.loads((site / "data" / "selection_funnel.json").read_text(encoding="utf-8")) == fact


# ---------- ROUND 6 / A2：结构化字段优先，文本正则只兼容旧报告 ----------

def test_structured_upstream_fields_take_priority(tmp_path):
    reports = _report(tmp_path, "2026-11-09", picks=2,
                      not_covered="筛查上限 999 场；窗口内共发现 999 场。旧文本不该被采用。")
    d = tmp_path / "reports" / "2026" / "11" / "2026-11-09.json"
    obj = json.loads(d.read_text(encoding="utf-8"))
    obj.update({"window_fixtures": 51, "screening_cap": 32, "outside_screen": 19})
    d.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    up = funnel.build(reports=reports)["days"][0]["upstream"]
    assert up == {"window_fixtures": 51, "screening_cap": 32, "outside_screen": 19,
                  "status": "RECORDED", "source": "report 结构化字段"}


def test_text_regex_still_used_for_legacy_reports(tmp_path):
    reports = _report(tmp_path, "2026-11-09", picks=1,
                      not_covered="筛查上限 32 场；窗口内共发现 79 场。")
    up = funnel.build(reports=reports)["days"][0]["upstream"]
    assert up["outside_screen"] == 47 and "文本正则" in up["source"]


def test_structured_fields_partial_do_not_backderive(tmp_path):
    reports = _report(tmp_path, "2026-11-09", picks=1)
    d = tmp_path / "reports" / "2026" / "11" / "2026-11-09.json"
    obj = json.loads(d.read_text(encoding="utf-8"))
    obj["window_fixtures"] = 60       # 缺 screening_cap：不拿配置倒推
    d.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    up = funnel.build(reports=reports)["days"][0]["upstream"]
    assert up["screening_cap"] == funnel.MISSING and up["outside_screen"] == funnel.MISSING
