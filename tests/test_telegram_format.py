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
    assert m.startswith("<b>【先知】⚽ 亚盘日报 2026-11-04</b>\n📅 11-04 周三 · 筛选 11 场 · 推荐 1 场")
    idx = [m.index(k) for k in ("🏆", "<b>波尔图", "🎯 <b>推荐 波尔图 -0.75 @1.06 · 🟡中</b>", "<pre>", "✅", "⚠️", "📈 本联赛", "🔗", "历史战绩")]
    assert idx == sorted(idx)
    assert "11-05 04:00" in m and "比共识深 0.25" in m and "共识中位(8家)" in m and "Pinnacle" in m
    assert "球探 初→即" in m and "Bet365" not in m and "🏟" not in m and "概率" not in m
    assert f"<i>{policy.DISCLAIMER}</i>" in m and m.rstrip().endswith("—— 先知")
    assert tg.SITE_URL in m and "goal3.xml" not in tg.plain(m)
    _balanced(m)


def test_stats_table_and_context():
    st = {"overall": {"picks": 4, "decided": 4, "win": 3, "loss": 1, "hit_rate": .75, "units": 1.5},
          "by_confidence": {"中": {"picks": 2, "decided": 2, "win": 1, "loss": 1, "hit_rate": .5, "units": -0.1}},
          "by_league": {PORTO["league"]: {"picks": 2, "decided": 2, "win": 2, "hit_rate": 1.0, "units": 2.0}}}
    m = tg.summary({**REP, "picks": [PORTO]}, stats=st)
    assert "本联赛 2场 100% +2.00u ｜ 中信心 2场 50% -0.10u" in m
    t = tg.plain(tg.stats_table(st)).splitlines()
    assert t[1].lstrip().startswith("场次") or "场次" in t[1]
    assert len({tg.wcol(x) for x in t[1:] if x.strip()}) >= 1


def test_cjk_pad():
    assert tg.wcol("共识ab") == 6 and tg.pad("共", 4) == "共  "


def test_no_picks_and_escape():
    m = tg.summary({"date": "2026-11-05", "picks": [], "watched": []})
    assert "今日无高信心推荐（宁缺毋滥）" in m and "📈" in m
    p = {**PORTO, "home": "A<b>&", "reasons": ["x < y"]}
    m = tg.pick_block(p)
    assert "A&lt;b&gt;&amp;" in m and "x &lt; y" in m


def test_split_under_limit():
    p = {**PORTO, "match_id": "x"}
    msgs = tg.split(["长" * 3000, "短", "长" * 5000])
    assert len(msgs) > 1 and all(len(x) <= tg.LIMIT for x in msgs)
    assert len("".join(tg.daily({**REP, "picks": [p] * 3}))) > 0


def test_prematch_and_backtest():
    m = "\n".join(tg.prematch(PORTO, {"decision": "调整", "new_line": -0.5, "new_odds": 0.95,
                                       "confidence": "高", "reason": "r"}))
    assert "🔎 赛前确认" in m and "🔄 调整" in m and "推荐 波尔图 -0.5 @0.95 · 🟢高" in m
    kashima = REP["picks"][0]
    assert "❌ 撤销" in "".join(tg.prematch(kashima))
    b = "\n".join(tg.backtest(PORTO, {"score": "2-0", "settlement": "win", "units": 1.06, "review": "ok"}))
    assert "🏁 赛后回测" in b and "<b>赢</b> · +1.06u" in b and "📈" in b
    _balanced(b)
