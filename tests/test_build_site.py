import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ah import build_site, policy
from ah.stats import compute

PAGES = ["index.html", "records.html", "stats.html", "sources.html", "rules.html", "history.html",
         "review.html", "reports/2026-11-04.html"]


def _build(tmp_path, stats=None, status=None, review_docs=None, facts=None):
    data = tmp_path / "data"
    data.mkdir()
    if stats is not None:
        (data / "stats.json").write_text(json.dumps(stats, ensure_ascii=False))
    if status is not None:
        (data / "source_status.json").write_text(json.dumps(status, ensure_ascii=False))
    for name, obj in (facts or {}).items():
        (data / name).write_text(json.dumps(obj, ensure_ascii=False))
    site = tmp_path / "site"
    reports = tmp_path / "reports"  # 固定为 2026-11-04 的报告快照
    shutil.copytree(build_site.REPORTS, reports, ignore=lambda d, fs: [f for f in fs if f[:10] > "2026-11-04" and f[:4] == "2026"])
    build_site.build(site=site, reports=reports, data=data, now=datetime(2026, 11, 4, 12, 30, tzinfo=timezone.utc),
                     review_docs=review_docs)
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
    # 2026-11-07：首页只显示正式推荐，观察名单时间不再出现在首页（后台数据仍保留）。
    assert "2026-11-04 20:15 北京时间" not in h      # Kashima 12:15Z 属观察名单
    # 2026-11-07：首页只显示正式推荐，已撤销不再与推荐混排（后台记录仍保留）。
    assert "st-已结算" in h and "规则档位，未经独立回测" in h
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
    assert {r["status"] for r in recs} == {"已撤销", "已结算"}


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
    # 总体样本 < 30 场：滚动命中率不画点（与复盘层一致），累计盈亏曲线仍保留。
    assert cd["hit7"] == [None, None] and cd["hit30"] == [None, None] and cd["cum"] == [0.9, -0.1]


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


# ---------- 复盘层接入网页 + 命中率结论门槛（2026-11-08 ROUND 4） ----------

def _rows(n, outcome="win"):
    return [{"date": "2026-11-01", "match_id": "m%d" % i, "league": "J1", "line": "-0.5",
             "confidence": "high", "ah_outcome": outcome, "units": "0.9" if outcome == "win" else "-1",
             "settled": "true"} for i in range(n)]


def test_hit_rate_not_quoted_below_min_sample(tmp_path):
    """小样本不得把命中率当结论：站点与复盘层同为「未取得」。"""
    site = _build(tmp_path, stats=compute(_rows(2, "win") + _rows(2, "loss")))
    for name in ("index.html", "stats.html"):
        h = (site / name).read_text()
        assert "50.0%" not in h and "25.0%" not in h and "未取得" in h
    assert "门槛" in (site / "index.html").read_text()
    assert "复盘</a>" in (site / "index.html").read_text()
    assert build_site.site_can_quote(compute(_rows(4))) is False


def test_hit_rate_quoted_when_sample_sufficient(tmp_path):
    site = _build(tmp_path, stats=compute(_rows(30)))
    assert "100.0%" in (site / "index.html").read_text()
    assert "100.0%" in (site / "stats.html").read_text()
    assert build_site.site_can_quote(compute(_rows(30))) is True


def test_telegram_summary_line_unchanged():
    """Telegram 渲染仍用 stats.summary_line，站点改动不得外溢到推送文本。"""
    from ah.stats import summary_line
    s = compute(_rows(2, "win") + _rows(2, "loss"))
    assert "历史命中率 50.0%" in summary_line(s)


def test_review_page_exports_and_no_fabrication(tmp_path):
    acc = {"schema": 1, "snapshots": [{
        "date": "2026-11-08", "generated_at": "2026-11-08T12:50:05Z", "run_id": "42482027896",
        "sample_status": "INSUFFICIENT_SAMPLE", "reconciliation": "RECONCILED", "decided": 14,
        "pending": 9, "missing_result": 0, "hit_rate": "未取得",
        "alerts": ["SETTLE_BACKLOG", "INSUFFICIENT_SAMPLE"]}]}
    lab = {"walk_forward": {"overall": {"status": "INSUFFICIENT_SAMPLE"}},
           "prediction_quality": {"status": "NOT_COMPUTED"}, "live_model_modified": False,
           "joined_decided_rows": 9}
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "2026-11-08.md").write_text(
        "# 预测准确率日复盘 · 2026-11-08\n\n| 项目 | 场数 |\n| --- | --- |\n| 已结算 | 14 |\n\n"
        "- 样本状态：**INSUFFICIENT_SAMPLE**\n- 历史累计命中率：未取得\n", encoding="utf-8")
    site = _build(tmp_path, review_docs=docs,
                  facts={"accuracy_review.json": acc, "rule_lab.json": lab})
    h = (site / "review.html").read_text()
    assert "复盘" in h and "INSUFFICIENT_SAMPLE" in h and "NOT_COMPUTED" in h
    assert "未取得" in h and "42482027896" in h and "RECONCILED" in h
    assert "review/2026-11-08.html" in h and "原始事实 JSON" in h
    assert "25.0%" not in h and "准确率提升" not in h          # 不编造结论数字
    assert "复盘</a>" in h
    # 事实卡状态：累计型取最新快照、缺口型取 alert 首词，不误写「暂无」。
    assert build_site._fact_status(acc) == "INSUFFICIENT_SAMPLE"
    assert build_site._fact_status({"alert": "SAMPLE_INSUFFICIENT 样本未达门槛"}) == "SAMPLE_INSUFFICIENT"
    assert build_site._fact_status({"counters": {"clv_price_computed": 0}}) == "事实文件（无状态字段）"
    assert "事实文件（无状态字段）" not in h            # 本例两个文件都有状态
    sub = (site / "review" / "2026-11-08.html").read_text()
    assert "<table" in sub and "已结算" in sub and "未取得" in sub
    exported = json.loads((site / "data" / "accuracy_review.json").read_text())
    assert exported == acc                                     # 逐字一致，不改写
    assert (site / "data" / "rule_lab.json").exists()
    assert "资料缺失" not in h or "暂无" in h


def test_review_page_degrades_when_artifacts_missing(tmp_path):
    site = _build(tmp_path)
    h = (site / "review.html").read_text()
    assert "暂无复盘快照" in h and "暂无（仓库未落盘）" in h and "未取得" in h
    assert (site / "review").is_dir()


# ---------- ROUND 6：收盘 join / 评估边界 / 联赛分桶 / 告警上网页 ----------

def test_review_page_shows_eval_boundary_and_clv_join(tmp_path):
    facts = {
        "eval_facts.json": {
            "model_scope": {"boundary": "研究层与线上发布不是同一套可互证结论；不能迁移",
                            "research_facts": ["rule_lab.live_model_modified：False"],
                            "live_publication_note": "规则档位，未经独立回测",
                            "files": [], "status": "FACTS_ONLY"},
            "league_facts": {"min_sample": 30, "consecutive_data_gap_days": 3,
                             "by_league": {"日职乙": {"settled_rows": 1, "decided": 1,
                                                     "data_gap_withheld": 11,
                                                     "hit_rate_conclusion": "未取得",
                                                     "sample_status": "INSUFFICIENT_SAMPLE"}},
                             "note": "不输出联赛优劣"},
            "alerts": [{"code": "UPSTREAM_SCREEN_CAP", "severity": "info",
                        "message": "单日最多 47 场未进候选", "action": "notify_only",
                        "evidence": "selection_funnel.json"}],
            "alert_policy": "notify_only：不自动补发"},
        "clv_join_facts.json": {
            "close_snapshot_rows_in_repo": 815, "close_snapshot_unique_matches": 26,
            "joined_unique_match_ids": 8, "joined_match_ids": ["fd-561930"],
            "price_clv_status_by_row": {"收盘盘口≠下注盘口（按口径不算 CLV）": 7,
                                       "无收盘快照": 17},
            "note": "线差只写 line_move，绝不冒充 CLV"},
    }
    site = _build(tmp_path, facts=facts)
    h = (site / "review.html").read_text()
    assert "研究层 ≠ 线上发布层" in h and "不能迁移" in h
    assert "按联赛事实分桶" in h and "日职乙" in h and "未取得" in h
    assert "结构化告警" in h and "UPSTREAM_SCREEN_CAP" in h and "notify_only" in h
    assert "收盘快照 join 事实" in h and "815" in h and "绝不冒充" in h
    assert "25.0%" not in h and "准确率提升" not in h
    assert (site / "data" / "eval_facts.json").exists()
    assert (site / "data" / "clv_join_facts.json").exists()
    assert "收盘快照 join" in (site / "funnel.html").read_text()


def test_review_page_degrades_when_round6_files_missing(tmp_path):
    h = (_build(tmp_path) / "review.html").read_text()
    assert "暂无（待 eval-facts 步骤产出）" in h
    assert "暂无（待 CLV join 步骤产出）" in h
