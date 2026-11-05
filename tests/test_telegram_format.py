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
    idx = [m.index(k) for k in ("🏆", "<b>波尔图", "🎯 <b>推荐 波尔图 -0.75 @1.06</b> ｜ 信心 🟡<b>中</b>", "<pre>",
                                "📈 本联赛", "✅ <b>为什么推</b>", "⚠️ <b>风险在哪</b>", "🔗 <b>数据来源</b>", "历史战绩")]
    assert idx == sorted(idx)
    assert "11-05 04:00" in m and "比共识深 0.25" in m and "共识中位·8家" in m and "Pinnacle" in m
    assert "球探 初盘" in m and "Bet365" not in m and "🏟" not in m and "概率" not in m
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
    assert "赢-走-输" in t[0] and t[1].startswith("4  3-0-1   75%  +1.50u  总体")


def test_cjk_pad():
    assert tg.wcol("共识ab") == 6 and tg.pad("共", 4) == "共  "


def test_no_picks_and_escape():
    m = tg.summary({"date": "2026-11-05", "picks": [], "watched": []})
    assert policy.no_pick_title() + "（宁缺毋滥）" in m and "📈" in m
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
    assert "🔎 赛前确认" in m and "🔄 调整" in m and "推荐 波尔图 -0.5 @0.95</b> ｜ 信心 🟢<b>高</b>" in m
    kashima = REP["picks"][0]
    assert "❌ 撤销" in "".join(tg.prematch(kashima))
    b = "\n".join(tg.backtest(PORTO, {"score": "2-0", "settlement": "win", "units": 1.06, "review": "ok"}))
    assert "🏁 赛后回测" in b and "<b>赢</b> · <code>+1.06u</code>" in b and "<blockquote" in b and "📈" in b
    _balanced(b)


# ---------- v4 对齐 / 富文本 ----------

def _pre_blocks(m):
    return [tg.plain(x) for x in re.findall(r"<pre>(.*?)</pre>", m, re.S)]


def test_pre_tables_ascii_columns_then_label():
    """<pre> 里每行：前面的数值列全是 ASCII 且等长（列对齐），中文只出现在行尾标签。"""
    st = {"overall": {"picks": 12, "decided": 11, "win": 7, "half_win": 1, "loss": 3, "hit_rate": .7, "units": 3.2},
          "by_league": {"英超（英超）": {"picks": 1, "decided": 0}}}
    m = "\n".join(tg.daily({**REP, "picks": [PORTO]}, st))
    blocks = _pre_blocks(m)
    assert len(blocks) == 2 and "→" not in "".join(blocks)
    for b in blocks:
        rows = b.splitlines()
        starts = [len(r) - len(re.split(r"\s{2,}", r)[-1]) for r in rows]
        assert len(set(starts)) == 1, rows                          # 标签起始列一致
        assert all(re.fullmatch(r"[ -~]*", r[:starts[0]]) for r in rows), rows  # 数值列全 ASCII
    assert "-0.75  1.06  本推荐" in blocks[0] and "-0.50  0.85  共识中位·8家" in blocks[0]
    assert "   -  " in blocks[1]                                   # 未决场次命中率不写 0%


def test_rich_html_structure():
    m = "\n".join(tg.daily({**REP, "picks": [PORTO]}))
    assert "<blockquote>• " in m and "<blockquote expandable>" in m and "tg-spoiler" not in m
    i = [m.index(k) for k in ("🎯", "为什么推", "风险在哪", "数据来源", policy.DISCLAIMER, "—— 先知")]
    assert i == sorted(i)
    _balanced(m)
    for t in ("pre", "blockquote", "code"):
        assert len(re.findall(f"<{t}[ >]", m)) == m.count(f"</{t}>"), t


def test_long_team_names_wrap_and_truncate():
    one = tg.teams_line("波尔图 Porto", "那不勒斯 Napoli")
    assert "\n" not in one
    two = tg.teams_line("武里南联 Buriram United", "鹿岛鹿角 Kashima Antlers")
    assert two.count("\n") == 1 and "\nvs " in two
    assert tg.team_name("布拉格斯拉维亚 Slavia Praha Prague FC") == "布拉格斯拉维亚"
    long_en = tg.team_name("X" * 60)
    assert tg.wcol(long_en) <= tg.TEAM_MAX and long_en.endswith("…")
    assert tg.league_name("UEFA Champions League（欧冠 联赛阶段第4轮）") == "欧冠 联赛阶段第4轮"


def test_escape_in_rich_blocks():
    p = {**PORTO, "home": "A<&>B", "reasons": ["x<y & z"], "risks": ["<script>"]}
    m = tg.compact_block(p)
    assert "A&lt;&amp;&gt;B" in m and "x&lt;y &amp; z" in m and "&lt;script&gt;" in m and "<script>" not in m


def test_split_utf16_and_rebalances_tags():
    """超长块按行拆条：每条 ≤ 4096（UTF-16 计，emoji 记 2），被拆开的 <blockquote>/<pre> 在每条里各自闭合。"""
    body = "\n".join(f"⚽ 第{i}行 <b>粗体</b> &amp; 文本" + "长" * 40 for i in range(200))
    blk = "🔗 <b>列表</b>\n<blockquote expandable>" + body + "</blockquote>\n<pre>" + "\n".join(["-0.75  1.06  x"] * 300) + "</pre>"
    msgs = tg.split(["<b>head</b>", blk, "tail"])
    assert len(msgs) > 2
    for x in msgs:
        assert tg.tlen(x) <= tg.LIMIT
        for t in ("b", "blockquote", "pre"):
            assert len(re.findall(f"<{t}[ >]", x)) == x.count(f"</{t}>"), (t, x[:80])
        assert not re.search(r"&[a-z]*$", x.split("<")[-1] if "<" in x else x)
    assert sum(x.count("<blockquote expandable>") for x in msgs) >= 2   # 续条重新打开原标签
    assert tg.plain("".join(msgs)).count("长" * 40) == 200
    assert tg.tlen("📈") == 2 and tg.tlen("中") == 1


def test_split_hard_cuts_single_long_line_outside_entities():
    line = ("&amp;" * 3000)
    msgs = tg.split([line])
    assert len(msgs) > 1 and all(tg.tlen(x) <= tg.LIMIT for x in msgs)
    assert all(not x.endswith("&") and not x.startswith("amp;") for x in msgs)
