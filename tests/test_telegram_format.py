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
    # 2026-11-07 契约：只显示推荐卡片；「信心」改称「规则档位」；盘口加中文术语。
    idx = [m.index(k) for k in ("🏆", "<b>波尔图",
                                "🎯 <b>推荐 波尔图 -0.75（半一） @1.06</b> ｜ 规则档位 🟡<b>中</b>", "<pre>",
                                "✅ <b>为什么推</b>", "⚠️ <b>风险在哪</b>", "🔗 <b>数据来源</b>")]
    assert idx == sorted(idx)
    assert "规则档位，未经独立回测" in m
    assert "📈 本联赛" not in m and "历史战绩" not in m and "比赛观察分析" not in m
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
    # 2026-11-07：卡片内「本联赛 / 信心战绩」行已移除，避免空历史被误读为战绩。
    assert "本联赛 2场 100%" not in m
    t = tg.plain(tg.stats_table(st)).splitlines()
    assert "赢-走-输" in t[0] and t[1].startswith("4  3-0-1   75%  +1.50u  总体")




def test_cjk_pad():
    assert tg.wcol("共识ab") == 6 and tg.pad("共", 4) == "共  "


def test_no_picks_and_escape():
    m = tg.summary({"date": "2026-11-05", "picks": [], "watched": []})
    assert policy.no_pick_title() + "（宁缺毋滥）" in m and "规则档位，未经独立回测" in m
    p = {**PORTO, "home": "A<b>&", "reasons": ["x < y"]}
    m = tg.pick_block(p)
    assert "A&lt;b&gt;&amp;" in m and "x &lt; y" in m




def test_scored_observation_is_explicitly_not_a_formal_recommendation():
    rep = {'date': '2026-11-06', 'picks': [], 'watched': [], 'watchlist': [{
        'home': 'Everton & Co', 'away': 'Coventry', 'league': '英超',
        'kickoff_utc': '2026-11-06T20:00:00Z', 'withheld_reason': '评分不足',
        'observation': {'pick_team': 'Everton & Co', 'line': -0.75, 'line_open': -0.5,
                        'odds': 0.91, 'score': 3.9, 'home_form': 'WWWWW', 'away_form': 'DDDDD',
                        'bookmaker': 'pinnacle', 'source': 'oddspapi',
                        'price_ts': '2026-11-06T08:00:00Z', 'freshness': 'stale_downgrade'}}]}
    msg = '\n'.join(tg.daily(rep))
    # 2026-11-07：观察分析不再进群消息；0 场改列「赛程事实清单」如实兜底，绝不出现推荐倾向。
    assert '推荐 0 场' in msg and '赛程事实清单' in msg and '状态：未推荐' in msg
    assert '比赛观察分析' not in msg and '🎯' not in msg and '评分 3.9' not in msg
    assert 'Everton &amp; Co' in msg and '评分不足' in msg
    _balanced(msg)




def test_split_under_limit():
    p = {**PORTO, "match_id": "x"}
    msgs = tg.split(["长" * 3000, "短", "长" * 5000])
    assert len(msgs) > 1 and all(len(x) <= tg.LIMIT for x in msgs)
    assert len("".join(tg.daily({**REP, "picks": [p] * 3}))) > 0


def test_prematch_and_backtest():
    m = "\n".join(tg.prematch(PORTO, {"decision": "调整", "new_line": -0.5, "new_odds": 0.95,
                                       "confidence": "高", "reason": "r"}))
    assert "🔎 赛前确认" in m and "🔄 调整" in m and "仍推 波尔图 -0.5 @0.95</b> ｜ 信心 🟢<b>高</b>" in m
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
    # 2026-11-07：历史战绩 <pre> 表从群消息移除，只剩机构对比 1 个 <pre>。
    assert len(blocks) == 1 and "→" not in "".join(blocks)
    for b in blocks:
        rows = b.splitlines()
        starts = [len(r) - len(re.split(r"\s{2,}", r)[-1]) for r in rows]
        assert len(set(starts)) == 1, rows                          # 标签起始列一致
        assert all(re.fullmatch(r"[ -~]*", r[:starts[0]]) for r in rows), rows  # 数值列全 ASCII
    assert "-0.75  1.06  本推荐" in blocks[0] and "-0.50  0.85  共识中位·8家" in blocks[0]
    # 历史战绩表虽不进群消息，单独调用时仍须保持「未决不写 0%」口径。
    assert "   -  " in tg.stats_table(st)




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


def test_prematch_declutter_first_screen_and_folds():
    """临场复核减负：首屏短结论；首发/临场水位/早盘机构对比默认折叠；不含 main_line。"""
    import json
    from pathlib import Path
    rep = json.loads((Path(__file__).resolve().parents[1] / "reports/2026/11/2026-11-05.json").read_text())
    p = next(x for x in rep["picks"] if x["match_id"] == "apif-1630401")
    msgs = tg.prematch(p)
    m = "\n".join(msgs)
    plain = tg.plain(m)
    assert "决定：<b>🔄 调整</b> · 盘口不变、水位调整" in m
    assert "仍推 町田泽维亚 -1.5 @0.96" in m and "信心" in m and "🟡" in m
    assert "复核 11-05 17:19" in m and "主线一致" in m
    assert "API-Football 已停用" in plain
    # 首发 / 临场水位 / 早盘快照 都在 expandable 里
    assert "展开首发" in m and "临场水位" in m and "早盘快照" in m
    assert m.count("<blockquote expandable>") >= 3
    # 首屏不把 22 人名单或 SBOBet 明细摊开在非 expandable 区块
    # （名单只应出现在 expandable 内）
    import re
    non_exp = re.sub(r"<blockquote expandable>.*?</blockquote>", "", m, flags=re.S)
    assert "谷晃生" not in non_exp and "SBOBet" not in non_exp
    assert "谷晃生" in m and "SBOBet" in m
    # 早盘快照水位是早间 1.02，不是临场 0.96
    assert "-1.50  1.02  本推荐" in m
    assert "main_line" not in m
    assert policy.DISCLAIMER in m and m.rstrip().endswith("—— 先知")
    for tag in ("b", "blockquote", "pre", "a"):
        assert len(re.findall(f"<{tag}[ >]", m)) == m.count(f"</{tag}>"), tag


def test_prematch_pending_still_omits_reason():
    p = {"home": "X", "away": "Y", "pick_side": "home", "line": -0.5, "odds": 0.9, "league": "L"}
    msg = "\n".join(tg.prematch(p, {}, None))
    assert "📌" not in msg and "暂无" in msg and "—— 先知" in msg
    assert "展开首发" not in msg and "早盘快照" not in msg
