"""v3 版式：无推荐状态按 watchlist 自动计数；TG 必含免责声明与先知署名；空字段不编造。"""
from ah import build_site, policy
from ah import publish_telegram as pt

REP = {"date": "2026-11-05", "picks": [], "no_pick_reason": "中信心仅 1 场",
       "watchlist": [{"home": "A", "away": "B", "confidence": "medium", "withheld_category": "深盘"},
                     {"home": "C", "away": "D", "confidence": "中", "withheld_category": "深盘"},
                     {"home": "E", "away": "F", "confidence": "low", "withheld_category": "信号不足"},
                     {"home": "G", "away": "H"}]}


def test_tg_no_pick_counts_from_watchlist_without_names():
    msg = "\n".join(pt.daily(REP, None))
    assert policy.no_pick_title() + "（宁缺毋滥）" in msg
    assert "观察 4 场" in msg and "中 2" in msg and "低 1" in msg and "未定 1" in msg
    assert "<code>2</code>  深盘" in msg and "<code>1</code>  信号不足" in msg
    assert "中信心仅 1 场" not in msg            # 不用手写原因里的错误计数
    assert "A vs B" not in msg and "<b>A" not in msg  # 中/低信心不点名推送
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
    rep = {"date": "2026-11-04", "picks": [{"home": "K", "away": "M", "pick_side": "away", "line": -0.75,
                                             "confidence": "cancelled", "cancelled": True}]}
    msg = "\n".join(pt.daily(rep, None))
    assert "赛前已撤销" in msg and "<s>K</s> vs <s>M</s>" in msg


def test_web_no_pick_card():
    h = build_site.no_pick_html(REP)
    assert policy.no_pick_title() in h and "观察名单 4 场" in h and "中信心 2 场" in h
    assert "深盘 2" in h and "中信心仅 1 场" not in h
