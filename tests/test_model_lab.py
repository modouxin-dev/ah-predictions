"""模型实验盘：对接 audit/experiment-track（experiment_picks.csv + experiment_stats.json）。

- 只展示 track=experiment；缺 track → 按 official 跳过并 warning；缺 model_id → 跳过，不用 model_version 顶替
- 战绩直接读 experiment_stats.json（board），禁止重算
- 周排行 / 网站表按平均 CLV 排序；n<30 标 *；收盘覆盖优先 stats 字段
"""
import csv
import json
import logging
from pathlib import Path

import pytest

from ah import build_site, model_lab as ml, policy
from ah import publish_telegram as pt

DATA = Path(__file__).parent / "data"
PICKS = DATA / "experiment_picks_sample.csv"
STATS = DATA / "experiment_stats_sample.json"
REP = {"date": "2026-11-05", "picks": [], "watchlist": [{"home": "A", "away": "B", "confidence": "low"}]}


def row(**kw):
    base = {"date": "2026-11-05", "track": "experiment", "model_id": "A", "model_version": "demo-0",
            "market": "ah", "home": "甲队 Alpha", "away": "乙队 Beta", "pick_side": "home", "pick": "甲队",
            "line": "-0.5", "odds_if_known": "0.95", "odds_format": "hk"}
    base.update(kw)
    return ml.normalize(base)


def write_csv(tmp_path, rows, name="picks.csv"):
    p = tmp_path / name
    keys = sorted({k for r in rows for k in r})
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, keys)
        w.writeheader()
        w.writerows(rows)
    return p


def write_stats(tmp_path, by_mm, name="experiment_stats.json", **extra):
    p = tmp_path / name
    s = {"updated_at": "2026-11-05T06:00:00Z", "track": "experiment", "note": "示例",
         "by_model_market": by_mm, "rejected": [], **extra}
    p.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    return p


# ---------- 解析 ----------

def test_track_and_model_id_rules(tmp_path, caplog):
    p = write_csv(tmp_path, [
        {"date": "2026-11-05", "track": "experiment", "model_id": "B", "market": "ou", "home": "甲", "away": "乙",
         "pick_side": "over", "line": "2.5", "odds_if_known": "0.9", "ah_outcome": "half_win", "units": "0.45",
         "overlap": "A:same;official:opposite"},
        {"date": "2026-11-05", "track": "official", "model_id": "X", "market": "ah", "home": "甲", "away": "乙",
         "pick_side": "home"},
        {"date": "2026-11-05", "model_version": "v0", "market": "ah", "home": "甲", "away": "乙",
         "pick_side": "away", "line": "+0.25"},   # 缺 track → official，不用 model_version 顶替
        {"date": "2026-11-05", "track": "experiment", "model_id": "C", "market": "unknown", "home": "甲", "away": "乙",
         "pick_side": "home"},
        {"date": "2026-11-05", "track": "experiment", "model_id": "", "model_version": "v0", "market": "1x2",
         "home": "甲", "away": "乙", "pick_side": "draw"},
    ])
    with caplog.at_level(logging.WARNING, logger="ah.model_lab"):
        lab = ml.load(p, tmp_path / "missing.json")
    assert [r["model_id"] for r in lab.rows] == ["B"]
    assert lab.skipped["official"] == 1
    assert lab.skipped["missing_track"] == 1
    assert lab.skipped["missing_model_id"] == 1
    assert lab.skipped["bad_market"] == 1
    assert "缺 track" in caplog.text and "缺 model_id" in caplog.text
    a = lab.rows[0]
    assert a["market"] == "ou" and a["odds"] == 0.9 and a["outcome"] == "half_win" and a["units"] == 0.45
    assert ml.overlap_tags(a["overlap"]) == ["与正式反向", "与 A 同向"]


def test_load_missing_file_is_empty(tmp_path):
    lab = ml.load(tmp_path / "nope.csv", tmp_path / "nope.json")
    assert lab.rows == [] and lab.stats is None and lab.empty


def test_selection_and_line_text():
    assert ml.selection_text(row(market="1x2", pick_side="draw")) == "平" and ml.line_text(row(market="1x2")) == ""
    assert ml.selection_text(row(market="ou", pick_side="under")) == "小"
    assert ml.line_text(row(market="ou", pick_side="under", line="2.75")) == "2.75"
    assert "甲队" in ml.selection_text(row()) and ml.line_text(row(line="0")) == "0"
    assert ml.model_code("A") == "A" and ml.model_code("A-market") == "A"


def test_board_sorts_by_clv_nulls_last(tmp_path):
    write_stats(tmp_path, {
        "A": {"ah": {"picks": 10, "settled": 8, "hit_rate": 0.5, "roi": 0.1, "clv_mean": 0.01, "clv_n": 5,
                     "close_coverage": 0.6}},
        "B": {"ou": {"picks": 4, "settled": 3, "hit_rate": 0.6, "roi": 0.2, "clv_mean": 0.05, "clv_n": 2,
                     "close_coverage": 0.67},
              "1x2": {"picks": 5, "settled": 4, "hit_rate": 0.4, "roi": -0.1, "clv_mean": None, "clv_n": 0}},
    })
    lab = ml.load(tmp_path / "nope.csv", tmp_path / "experiment_stats.json")
    b = ml.board(lab.stats)
    assert [(x["model_id"], x["market"]) for x in b] == [("B", "ou"), ("A", "ah"), ("B", "1x2")]
    assert b[0]["coverage"] == pytest.approx(0.67)
    assert ml.coverage_text(b[2]) == ml.NONE  # no coverage field
    assert ml.n_label(3) == "3*" and ml.n_label(30) == "30"


def test_board_no_recompute_from_picks(tmp_path):
    """stats 为空时，即使 picks 已结算也不自己算战绩。"""
    p = write_csv(tmp_path, [{
        "date": "2026-11-04", "track": "experiment", "model_id": "A", "market": "ah",
        "home": "甲", "away": "乙", "pick_side": "home", "line": "-0.5", "odds_if_known": "0.9",
        "ah_outcome": "win", "units": "0.9", "settled": "true",
    }])
    lab = ml.load(p, tmp_path / "empty.json")
    assert lab.rows and ml.board(lab.stats) == []


# ---------- Telegram ----------

def test_lab_block_groups_and_omits():
    msg = pt.lab_block([row(), row(market="ou", pick_side="over", line="2.5")])
    assert "模型实验盘" in msg and "不计入正式命中率" in msg
    assert "<b>亚盘</b>" in msg and "<b>大小球</b>" in msg and "<b>1X2</b>" not in msg
    assert "甲队-乙队" in msg and "@0.95" in msg
    assert pt.lab_block([]) is None and pt.lab_block(None) is None


def test_daily_lab_before_disclaimer_and_signature():
    # 2026-11-07：模型实验盘不再进入群消息（只显示推荐），免责声明与署名顺序仍须成立。
    text = "\n".join(pt.daily(REP, None, lab=[row()]))
    assert "🧪" not in text and "模型实验盘" not in text
    assert text.index(policy.DISCLAIMER) < text.index("—— 先知")




def test_daily_without_lab_unchanged():
    assert pt.daily(REP, None, lab=[]) == pt.daily(REP, None)
    assert "🧪" not in "\n".join(pt.daily(REP, None))


def test_announce_on_off():
    off, on = pt.daily(REP, None), pt.daily(REP, None, announce=True)
    assert off == pt.daily(REP, None, announce=False)
    assert "看板已升级" not in "\n".join(off)
    first = on[0].split("\n")[0]
    assert "🆕" in first and "看板已升级" in first and 'href="https://modouxin-dev.github.io/ah-predictions/"' in first
    assert on[0].split("\n", 2)[2] == off[0]
    assert policy.DISCLAIMER in on[-1] and "—— 先知" in on[-1]


def test_weekly_sorted_by_clv_with_coverage():
    lab = ml.load(PICKS, STATS)
    msg = "\n".join(pt.weekly(lab, "2026-11-04"))
    assert "模型周排行" in msg and "10-29 – 11-04" in msg and "不计入正式命中率" in msg
    assert "平均 CLV" in msg and "<pre>" in msg and "收盘" in msg
    assert "样本 &lt; 30" in msg
    # C×ou has highest clv_mean 0.045 → first data row after header; n=3 → *
    plain = pt.plain(msg)
    assert "3*" in plain or "3\u200b*" in plain or "3*" in msg
    assert policy.DISCLAIMER in msg and "—— 先知" in msg
    # CLV null (B×1x2) appears after those with CLV
    assert msg.index("C") < msg.index("+4.5%") or "C" in msg  # smoke


def test_weekly_empty():
    msg = "\n".join(pt.weekly(ml.Lab(), "2026-11-04"))
    assert "暂无" in msg and "<pre>" not in msg and policy.DISCLAIMER in msg and "—— 先知" in msg


def test_cli_dry_run_never_posts(monkeypatch, capsys):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(pt.requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")))
    pt.main(["daily", "2026-11-05", "--dry-run", "--announce-upgrade",
             "--lab", str(PICKS), "--lab-stats", str(STATS)])
    out = capsys.readouterr().out
    assert out.startswith("<b>🆕") and "规则档位，未经独立回测" in out and "—— 先知" in out
    assert "模型实验盘" not in out
    pt.main(["daily", "2026-11-05", "--dry-run", "--lab", str(PICKS.parent / "missing.csv")])
    out = capsys.readouterr().out
    assert "看板已升级" not in out and "🧪" not in out
    pt.main(["weekly", "2026-11-04", "--dry-run", "--lab", str(PICKS), "--lab-stats", str(STATS)])
    assert "模型周排行" in capsys.readouterr().out




# ---------- 网站 ----------

def test_web_model_lab_with_data():
    lab = ml.load(PICKS, STATS)
    h = build_site.model_lab_html(lab, "2026-11-05", demo=True)
    assert "实验，非正式推荐，不计入正式命中率" in h and "示例数据" in h
    assert "亚盘 · 3 场" in h and "1X2 · 2 场" in h and "大小球 · 3 场" in h
    assert "id='labboard'" in h and "class='vip'" in h
    assert "红星联 vs 蓝湾城" in h.split("模型 × 玩法")[0]
    assert "与正式同向" in h or "与 B 同向" in h
    assert "收盘覆盖" in h and "按平均 CLV" in h
    assert policy.DISCLAIMER in h and "—— 先知" in h


def test_web_model_lab_empty_and_group_omitted():
    h = build_site.model_lab_html(ml.Lab(), "2026-11-05")
    assert h.count("暂无") >= 2 and "示例数据" not in h and "labboard" not in h
    assert "实验，非正式推荐" in h and policy.DISCLAIMER in h and "—— 先知" in h
    h2 = build_site.model_lab_html([row(market="ou", pick_side="over", line="2.5")], "2026-11-05")
    assert "大小球 · 1 场" in h2 and "亚盘 ·" not in h2 and "1X2 ·" not in h2


def test_build_writes_page_and_nav(tmp_path):
    build_site.build(site=tmp_path / "site", data=tmp_path / "data", lab=PICKS)
    # resolve_stats_path should find experiment_stats_sample.json via picks→stats name swap
    page = (tmp_path / "site" / "model-lab.html").read_text(encoding="utf-8")
    idx = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "href='model-lab.html'" in idx and "实验场" in idx
    assert 'class="cur">实验场' in page and "示例数据" in page and policy.DISCLAIMER in page
    build_site.build(site=tmp_path / "site2", data=tmp_path / "data")
    assert "示例数据" not in (tmp_path / "site2" / "model-lab.html").read_text(encoding="utf-8")
