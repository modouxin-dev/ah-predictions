"""模型实验盘：宽容解析、统计、Telegram 段落/周排行/--announce-upgrade、网站「模型实验场」。全部使用示例数据。"""
import csv
from pathlib import Path

import pytest

from ah import build_site, model_lab as ml, policy
from ah import publish_telegram as pt

SAMPLE = Path(__file__).parent / "data" / "model_lab_sample.csv"
REP = {"date": "2026-11-05", "picks": [], "watchlist": [{"home": "A", "away": "B", "confidence": "low"}]}


def row(**kw):
    base = {"date": "2026-11-05", "track": "experiment", "model_id": "A-market", "model_label": "市场模型",
            "market": "ah", "home": "甲队 Alpha", "away": "乙队 Beta", "selection": "home", "line": "-0.5", "odds": "0.95"}
    base.update(kw)
    return ml.normalize(base)


def write_csv(tmp_path, rows):
    p = tmp_path / "lab.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, sorted({k for r in rows for k in r}))
        w.writeheader()
        w.writerows(rows)
    return p


# ---------- 解析 ----------

def test_aliases_and_markets(tmp_path):
    p = write_csv(tmp_path, [
        {"Date": "2026-11-05", "Track": "experiment", "model": "B-poisson", "market": "Totals", "home": "甲", "away": "乙",
         "pick": "over", "line": "2.5", "odds_taken": "0.9", "ah_outcome": "half_win", "pnl": "0.45", "clv_pct": "3.2"},
        {"date": "2026-11-05", "track": "official", "model_id": "X", "market": "ah", "home": "甲", "away": "乙"},
        {"date": "2026-11-05", "model_version": "market-baseline-v0", "market": "亚盘", "home": "甲", "away": "乙",
         "pick_side": "away", "line": "+0.25", "odds": "0.88"},
        {"date": "2026-11-05", "track": "experiment", "model_id": "C-elo", "market": "unknown"},
        {"date": "2026-11-05", "track": "experiment", "model_id": "", "market": "1x2"},
    ])
    rows = ml.load(p)
    assert [r["model_id"] for r in rows] == ["B-poisson", "market-baseline-v0"]   # official / 缺玩法 / 缺模型 被排除
    a, b = rows
    assert a["market"] == "ou" and a["odds"] == 0.9 and a["outcome"] == "half_win" and a["units"] == 0.45
    assert a["clv"] == pytest.approx(0.032)
    assert b["market"] == "ah" and ml.selection_text(b) == "乙" and ml.line_text(b) == "+0.25"


def test_load_missing_file_is_empty(tmp_path):
    assert ml.load(tmp_path / "nope.csv") == []


def test_selection_and_line_text():
    assert ml.selection_text(row(market="1x2", selection="draw")) == "平" and ml.line_text(row(market="1x2")) == ""
    assert ml.selection_text(row(market="ou", selection="under")) == "小" and ml.line_text(row(market="ou", line="2.75")) == "2.75"
    assert ml.selection_text(row()) == "甲队" and ml.line_text(row(line="0")) == "0"
    assert ml.model_code("A-market") == "A" and ml.model_code("market-baseline-v0") == "market-b"


def test_leaderboard_metrics():
    rows = [row(outcome="win", odds="0.9", clv="0.02"), row(outcome="half_loss"), row(outcome="push"),
            row(outcome=""), row(market="1x2", odds="2.5", odds_format="decimal", outcome="loss")]
    b = {(a["model_id"], a["market"]): a for a in ml.leaderboard(rows)}
    ah = b[("A-market", "ah")]
    assert ah["picks"] == 4 and ah["settled"] == 3 and ah["decided"] == 2
    assert ah["hit_rate"] == pytest.approx(0.5) and ah["units"] == pytest.approx(0.4)
    assert ah["roi"] == pytest.approx(0.4 / 3) and ah["clv"] == pytest.approx(0.02) and ah["clv_n"] == 1
    assert b[("A-market", "1x2")]["roi"] == -1


def test_leaderboard_empty():
    assert ml.leaderboard([]) == []


# ---------- Telegram 日报实验盘段 ----------

def test_lab_block_groups_and_omits():
    msg = pt.lab_block([row(), row(market="ou", selection="over", line="2.5")])
    assert "模型实验盘" in msg and "不计入正式命中率" in msg
    assert "<b>亚盘</b>" in msg and "<b>大小球</b>" in msg and "<b>1X2</b>" not in msg   # 空组省略
    assert "甲队-乙队" in msg and "甲队 -0.5" in msg and "@0.95" in msg and "A 市场模型" in msg
    assert pt.lab_block([]) is None and pt.lab_block(None) is None


def test_daily_lab_before_disclaimer_and_signature():
    text = "\n".join(pt.daily(REP, None, lab=[row()]))
    i = text.index("🧪")
    assert i < text.index(policy.DISCLAIMER) < text.index("—— 先知")


def test_daily_without_lab_unchanged():
    assert pt.daily(REP, None, lab=[]) == pt.daily(REP, None)
    assert "🧪" not in "\n".join(pt.daily(REP, None))


def test_announce_on_off():
    off, on = pt.daily(REP, None), pt.daily(REP, None, announce=True)
    assert off == pt.daily(REP, None, announce=False)
    assert "看板已升级" not in "\n".join(off)
    first = on[0].split("\n")[0]
    assert "🆕" in first and "看板已升级" in first and 'href="https://modouxin-dev.github.io/ah-predictions/"' in first
    assert on[0].split("\n", 2)[2] == off[0]                              # 只在开头多一行（+空行）
    assert policy.DISCLAIMER in on[-1] and "—— 先知" in on[-1]


# ---------- 周排行 ----------

def test_weekly_with_sample():
    msg = "\n".join(pt.weekly(ml.load(SAMPLE), "2026-11-04"))
    assert "模型周排行" in msg and "10-29 – 11-04" in msg and "不计入正式命中率" in msg
    assert "<pre>" in msg and "ROI" in msg and "CLV" in msg and "样本 &lt; 30" in msg and "4*" in msg
    assert policy.DISCLAIMER in msg and "—— 先知" in msg


def test_weekly_empty():
    msg = "\n".join(pt.weekly([], "2026-11-04"))
    assert "暂无已结算" in msg and "<pre>" not in msg and policy.DISCLAIMER in msg and "—— 先知" in msg


def test_cli_dry_run_never_posts(monkeypatch, capsys):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(pt.requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")))
    pt.main(["daily", "2026-11-05", "--dry-run", "--announce-upgrade", "--lab", str(SAMPLE)])
    out = capsys.readouterr().out
    assert out.startswith("<b>🆕") and "模型实验盘" in out and "—— 先知" in out
    pt.main(["daily", "2026-11-05", "--dry-run", "--lab", str(SAMPLE.parent / "missing.csv")])
    out = capsys.readouterr().out
    assert "看板已升级" not in out and "🧪" not in out
    pt.main(["weekly", "2026-11-04", "--dry-run", "--lab", str(SAMPLE)])
    assert "模型周排行" in capsys.readouterr().out


# ---------- 网站 ----------

def test_web_model_lab_with_data():
    h = build_site.model_lab_html(ml.load(SAMPLE), "2026-11-05", demo=True)
    assert "实验，非正式推荐，不计入正式命中率" in h and "示例数据" in h
    assert "亚盘 · 3 场" in h and "1X2 · 2 场" in h and "大小球 · 3 场" in h
    assert "id='labboard'" in h and "class='vip'" in h                    # 战绩表沿用 VIP 锁
    assert "红星联 vs 蓝湾城" in h.split("模型 × 玩法")[0]                   # 列表公开
    assert policy.DISCLAIMER in h and "—— 先知" in h


def test_web_model_lab_empty_and_group_omitted():
    h = build_site.model_lab_html([], "2026-11-05")
    assert h.count("暂无") >= 2 and "示例数据" not in h and "labboard" not in h
    assert "实验，非正式推荐" in h and policy.DISCLAIMER in h and "—— 先知" in h
    h2 = build_site.model_lab_html([row(market="ou", selection="over", line="2.5")], "2026-11-05")
    assert "大小球 · 1 场" in h2 and "亚盘 ·" not in h2 and "1X2 ·" not in h2


def test_build_writes_page_and_nav(tmp_path):
    build_site.build(site=tmp_path / "site", data=tmp_path / "data", lab=SAMPLE)
    page = (tmp_path / "site" / "model-lab.html").read_text(encoding="utf-8")
    idx = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "href='model-lab.html'" in idx and "实验场" in idx
    assert "class=\"cur\">实验场" in page and "示例数据" in page and policy.DISCLAIMER in page
    build_site.build(site=tmp_path / "site2", data=tmp_path / "data")
    assert "示例数据" not in (tmp_path / "site2" / "model-lab.html").read_text(encoding="utf-8")
