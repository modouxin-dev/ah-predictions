"""v3 版式：无推荐状态按 watchlist 自动计数；TG 必含免责声明与先知署名；空字段不编造。"""
from ah import build_site, policy
from ah import publish_telegram as pt

REP = {"date": "2026-11-05", "picks": [], "no_pick_reason": "中信心仅 1 场",
       "watchlist": [{"home": "A", "away": "B", "confidence": "medium", "withheld_category": "深盘"},
                     {"home": "C", "away": "D", "confidence": "中", "withheld_category": "深盘"},
                     {"home": "E", "away": "F", "confidence": "low", "withheld_category": "信号不足"},
                     {"home": "G", "away": "H"}]}


def test_tg_no_pick_counts_from_watchlist_and_lists_facts_only():
    msg = "\n".join(pt.daily(REP, None))
    assert policy.no_pick_title() + "（宁缺毋滥）" in msg
    assert "观察 4 场" in msg and "中 2" in msg and "低 1" in msg and "未定 1" in msg
    assert "<code>2</code>  深盘" in msg and "<code>1</code>  信号不足" in msg
    assert "中信心仅 1 场" not in msg            # 不用手写原因里的错误计数
    # 现契约：0 场推荐时只列「赛程事实清单」（非推荐、无预测结论）；不得出现推荐/倾向字样
    assert "赛程事实清单" in msg and "A vs B" in msg and "状态：未推荐" in msg
    assert "<b>A" not in msg and "🎯" not in msg and "推荐 A" not in msg
    assert policy.DISCLAIMER in msg and "—— 先知" in msg


def test_tg_backtest_pending_omits_empty_fields():
    p = {"home": "X", "away": "Y", "pick_side": "home", "line": -0.5, "odds": 0.9, "league": "L"}
    msg = "\n".join(pt.backtest(p, {}, None))
    assert "📌" not in msg and "— Y" not in msg and "暂无" in msg
    assert policy.DISCLAIMER in msg and "—— 先知" in msg


def test_tg_prematch_pending_omits_reason():
    p = {"home": "X", "away": "Y", "pick_side": "home", "line": -0.5, "odds": 0.9, "league": "L"}
    msg = "\n".join(pt.prematch(p, {}, None))
    assert "📌" not in msg and "暂无" in msg and "—— 先知" in msg


def test_daily_lists_cancelled():
    # 2026-11-07：群消息只显示正式推荐，已撤销从渲染层移除；报告 JSON 仍保留，不销毁数据。
    rep = {"date": "2026-11-04", "picks": [{"home": "K", "away": "M", "pick_side": "away", "line": -0.75,
                                              "confidence": "cancelled", "cancelled": True}]}
    msg = "\n".join(pt.daily(rep, None))
    assert "赛前已撤销" not in msg and "🎯" not in msg
    assert "规则档位，未经独立回测" in msg and policy.DISCLAIMER in msg




def test_web_no_pick_card():
    h = build_site.no_pick_html(REP)
    assert policy.no_pick_title() in h and "观察名单 4 场" in h and "中信心 2 场" in h
    assert "深盘 2" in h and "中信心仅 1 场" not in h


def test_web_freeze_stamp_on_index_and_report(tmp_path):
    """首页与报告页首屏带冻结印戳；文案固定；不动 main_line 相关逻辑。"""
    from datetime import datetime, timezone
    import shutil
    data = tmp_path / "data"; data.mkdir()
    (data / "stats.json").write_text("{}", encoding="utf-8")
    reports = tmp_path / "reports"
    shutil.copytree(build_site.REPORTS, reports,
                    ignore=lambda d, fs: [f for f in fs if f[:10] > "2026-11-04" and f[:4] == "2026"])
    site = tmp_path / "site"
    build_site.build(site=site, reports=reports, data=data,
                     now=datetime(2026, 11, 4, 12, 30, tzinfo=timezone.utc))
    stamp = build_site.FREEZE_STAMP
    assert stamp == "赛前研究·已冻结·非临场"
    for name in ("index.html", "reports/2026-11-04.html"):
        h = (site / name).read_text(encoding="utf-8")
        assert stamp in h and "freeze-stamp" in h
        assert policy.DISCLAIMER in h and "先知" in h
    # 非日报页（战绩等）不带印戳
    assert stamp not in (site / "stats.html").read_text(encoding="utf-8")
