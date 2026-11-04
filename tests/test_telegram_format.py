import json
import re
from pathlib import Path

from ah import policy, publish_telegram as tg

REP = json.loads((Path(__file__).resolve().parents[1] / "reports/2026/11/2026-11-04.json").read_text(encoding="utf-8"))
PORTO = next(p for p in REP["picks"] if "Porto" in p["home"])


def _balanced(m):
    for t in ("b", "i", "s", "a"):
        assert len(re.findall(f"<{t}[ >]", m)) == m.count(f"</{t}>"), t


def test_daily_order_and_footer():
    m = tg.summary({**REP, "picks": [PORTO]})
    assert m.startswith("<b>【先知】⚽ 亚盘日报 2026-11-04</b>")
    idx = [m.index(k) for k in ("⚽ <b>波尔图", "🎯", "📊", "🔥", "📌", "⚠️", "🔗")]
    assert idx == sorted(idx)
    assert "11-05 04:00 北京时间" in m
    assert f"<i>{policy.DISCLAIMER}</i>" in m and m.rstrip().endswith("—— 先知")
    assert tg.SITE_URL in m and "共识中位：-0.5 @ 0.85（8 家" in m and "Pinnacle：-0.5 @ 0.88" in m
    assert "goal3.xml" not in tg.plain(m)  # 只显示短标签
    _balanced(m)


def test_no_picks_and_escape():
    m = tg.summary({"date": "2026-11-05", "picks": [], "watched": []})
    assert "今日无高信心推荐（宁缺毋滥）" in m and "📈" in m
    p = {**PORTO, "home": "A<b>&", "reasons": ["x < y"]}
    m = tg.pick_block(p)
    assert "A&lt;b&gt;&amp;" in m and "x &lt; y" in m


def test_split_under_limit():
    p = {**PORTO, "reasons": ["长" * 1500] * 5}
    msgs = tg.daily({**REP, "picks": [p, p]})
    assert len(msgs) > 1 and all(len(x) <= tg.LIMIT for x in msgs)


def test_prematch_and_backtest():
    m = "\n".join(tg.prematch(PORTO, {"decision": "调整", "new_line": -0.5, "new_odds": 0.95,
                                       "confidence": "高", "reason": "r"}))
    assert "🔎 赛前确认" in m and "🔄 调整" in m and "波尔图 -0.5 @ 0.95" in m
    kashima = REP["picks"][0]
    assert "❌ 撤销" in "".join(tg.prematch(kashima))
    b = "\n".join(tg.backtest(PORTO, {"score": "2-0", "settlement": "win", "units": 1.06, "review": "ok"}))
    assert "🏁 赛后回测" in b and "<b>赢</b>（+1.06 单位）" in b and "📈" in b
    _balanced(b)
