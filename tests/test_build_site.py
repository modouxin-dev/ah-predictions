import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ah import build_site, policy
from ah.stats import compute

PAGES = ["index.html", "records.html", "stats.html", "sources.html", "rules.html", "history.html", "reports/2026-11-04.html"]


def _build(tmp_path, stats=None, status=None):
    data = tmp_path / "data"
    data.mkdir()
    if stats is not None:
        (data / "stats.json").write_text(json.dumps(stats, ensure_ascii=False))
    if status is not None:
        (data / "source_status.json").write_text(json.dumps(status, ensure_ascii=False))
    site = tmp_path / "site"
    reports = tmp_path / "reports"  # 固定为 2026-11-04 的报告快照
    shutil.copytree(build_site.REPORTS, reports, ignore=lambda d, fs: [f for f in fs if f[:10] > "2026-11-04" and f[:4] == "2026"])
    build_site.build(site=site, reports=reports, data=data, now=datetime(2026, 11, 4, 12, 30, tzinfo=timezone.utc))
    return site


def test_all_pages_disclaimer_nav_beijing(tmp_path):
    site = _build(tmp_path, stats=compute([]))
    for n in PAGES:
        h = (site / n).read_text()
        assert policy.DISCLAIMER in h and "北京时间" in h and "先知" in h
        for t in ("今日", "推荐记录", "战绩", "数据源", "规则"):
            assert f">{t}</a>" in h
    assert (site / "assets" / "app.js").exists() and (site / "data" / "records.json").exists()


def test_kickoff_in_beijing_and_status_badges(tmp_path):
    h = (_build(tmp_path) / "index.html").read_text()
    assert "2026-11-05 04:00 北京时间" in h          # Porto 20:00Z
    assert "2026-11-04 20:15 北京时间" in h          # Kashima 12:15Z
    assert "st-待确认" in h and "st-已撤销" in h
    assert "🔒 VIP 可见，输入密码解锁" in h and "盘口面板" in h


def test_odds_panel_fields_and_missing():
    p = json.loads(Path("reports/2026/11/2026-11-04.json").read_text())["picks"]
    porto = build_site.odds_panel(p[1])
    assert "-0.5 @ 0.85" in porto and "8 家公司" in porto and "-0.5 @ 0.88" in porto
    assert "Bet365 初→即</dt><dd>暂无" in porto
    k = build_site.odds_panel(p[0])
    assert "多家共识中位盘</dt><dd>暂无" in k and "Pinnacle</dt><dd>暂无" in k
    mv = build_site.odds_panel({**p[1], "bet365_movement": {"open_pick_line": -0.25, "open_pick_water": 0.9,
                                                              "pick_line": -0.5, "pick_water": 0.85}})
    assert "-0.25 @ 0.90 → -0.5 @ 0.85" in mv


def test_empty_states(tmp_path):
    site = _build(tmp_path, stats=compute([]))
    s = (site / "stats.html").read_text()
    assert "暂无已结算推荐" in s and "chart-units'" not in s
    src = (site / "sources.html").read_text()
    assert "暂无运行记录" in src and "Football data by 5DollarFootballAPI" in src


def test_records_page_rows(tmp_path):
    h = (_build(tmp_path) / "records.html").read_text()
    assert h.count("class='rec'") == 2 and "id='filters'" in h and "class='detail'" in h
    recs = json.loads((tmp_path / "site" / "data" / "records.json").read_text())
    assert {r["status"] for r in recs} == {"已撤销", "待确认"}


def test_stats_with_series_and_tables(tmp_path):
    rows = [{"date": "2026-11-01", "match_id": "a", "league": "J1", "line": "-0.5", "confidence": "high",
             "ah_outcome": "win", "units": "0.9", "settled": "true"},
            {"date": "2026-11-03", "match_id": "b", "league": "K1", "line": "0.25", "confidence": "high",
             "ah_outcome": "loss", "units": "-1", "settled": "true"}]
    s = compute(rows)
    assert [x["cum_units"] for x in s["series"]] == [0.9, -0.1]
    assert set(s["by_role"]) == {"让球方（上盘）", "受让方（下盘）"} and "半球 0.5" in s["by_depth"]
    h = (_build(tmp_path, stats=s) / "stats.html").read_text()
    assert "chart-units" in h and "按盘口深度" in h and "上盘 / 下盘" in h
    cd = build_site.chart_data(s)
    assert cd["hit7"] == [1.0, 0.5] and cd["cum"] == [0.9, -0.1]


def test_source_status_rendering(tmp_path):
    st = {"updated_at": "2026-11-04T12:00:00Z", "sources": {
        "odds_api": {"last_ok_at": "2026-11-04T11:43:04Z", "last_call_at": "2026-11-04T11:43:04Z",
                     "quota": {"remaining": 497, "limit": 500, "period": "月", "as_of": "2026-11-04T11:43:04Z"}},
        "fivedollar": {"last_ok_at": "2026-11-04T10:00:00Z", "last_error_at": "2026-11-04T11:00:00Z", "last_error": "HTTP 500"}}}
    h = (_build(tmp_path, status=st) / "sources.html").read_text()
    assert "剩余 497 / 500" in h and "2026-11-04 19:43 北京时间" in h and "最近一次请求出错" in h


def test_vip_hash_only():
    import yaml
    cfg = yaml.safe_load(Path("config/settings.yaml").read_text())
    assert len(cfg["vip_password_sha256"]) == 64
