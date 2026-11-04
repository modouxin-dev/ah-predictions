"""先知 Telegram 按钮机器人：用假 update 测试处理逻辑（无网络）。"""
import json
import shutil
from pathlib import Path

import pytest

from ah import tg_bot
from ah.policy import DISCLAIMER

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0


@pytest.fixture
def repo(tmp_path):
    reports, data = tmp_path / "reports", tmp_path / "data"
    # 固定为 2026-11-04 的报告快照（之后的真实报告不影响用例）
    shutil.copytree(ROOT / "reports", reports, ignore=lambda d, fs: [f for f in fs if f[:10] > "2026-11-04" and f[:4] == "2026"])
    data.mkdir()
    for f in ("stats.json", "source_status.json"):
        shutil.copy(ROOT / "data" / f, data / f)
    return reports, data


def msg(text, chat_type="group", chat_id=-100, date=NOW, mid=7):
    return {"update_id": 1, "message": {"message_id": mid, "date": date, "text": text,
                                        "chat": {"id": chat_id, "type": chat_type}}}


def cb(data, chat_id=-100, mid=9):
    return {"update_id": 2, "callback_query": {"id": "cq1", "data": data,
                                               "message": {"message_id": mid, "chat": {"id": chat_id}}}}


def lim():
    t = [0.0]
    r = tg_bot.RateLimiter(clock=lambda: t[0])
    r._t = t
    return r


def run(update, repo, limiter=None, **kw):
    return tg_bot.handle(update, limiter or lim(), "LYZKYPbot", reports=repo[0], data=repo[1], now=NOW, **kw)


def buttons(kb):
    return [[b.get("callback_data") or b.get("url") for b in row] for row in kb["inline_keyboard"]]


def test_parse_command():
    assert tg_bot.parse_command("/start") == "start"
    assert tg_bot.parse_command("/Today@LYZKYPbot x") == "today"
    assert tg_bot.parse_command("/start@OtherBot") is None
    assert tg_bot.parse_command("hello") is None


def test_start_shows_menu_keyboard(repo):
    calls = run(msg("/start@LYZKYPbot"), repo)
    assert [m for m, _ in calls] == ["sendMessage"]
    p = calls[0][1]
    assert p["parse_mode"] == "HTML" and p["chat_id"] == -100
    assert buttons(p["reply_markup"]) == [["m:today", "m:sources"], ["m:stats", "m:records"],
                                          ["m:rules", "m:report"], [tg_bot.SITE_URL]]
    assert p["reply_parameters"]["message_id"] == 7
    assert "先知" in p["text"]


def test_menu_alias_and_commands(repo):
    for c, title in [("/menu", "菜单"), ("/today", "今日推荐"), ("/sources", "数据源状态"), ("/stats", "历史命中率"),
                     ("/records", "赛前确认"), ("/rules", "规则说明"), ("/report", "最新报告")]:
        p = run(msg(c), repo)[0][1]
        assert title in p["text"], c
        assert p["text"].rstrip().endswith("—— 先知") and DISCLAIMER in p["text"]


def test_group_unknown_or_plain_ignored(repo):
    assert run(msg("/foo"), repo) == []
    assert run(msg("随便聊聊"), repo) == []
    assert run(msg("/start@OtherBot"), repo) == []


def test_private_any_text_gets_menu(repo):
    p = run(msg("hi", chat_type="private", chat_id=5), repo)[0][1]
    assert "reply_parameters" not in p and buttons(p["reply_markup"])[0] == ["m:today", "m:sources"]


def test_stale_message_ignored(repo):
    assert run(msg("/start", date=NOW - 3600), repo) == []


def test_callback_edits_in_place(repo):
    calls = run(cb("m:sources"), repo)
    assert [m for m, _ in calls] == ["answerCallbackQuery", "editMessageText"]
    p = calls[1][1]
    assert p["message_id"] == 9 and p["chat_id"] == -100
    assert buttons(p["reply_markup"])[-1] == ["m:menu"]
    back = run(cb("m:menu"), repo)[1][1]
    assert buttons(back["reply_markup"])[0] == ["m:today", "m:sources"]


def test_unknown_callback_only_answers(repo):
    assert [m for m, _ in run(cb("x:evil"), repo)] == ["answerCallbackQuery"]


def test_rate_limit_per_chat(repo):
    r = lim()
    assert run(msg("/start"), repo, r)
    assert run(msg("/start"), repo, r) == []          # 同一 chat 太快
    assert run(msg("/start", chat_id=-200), repo, r)   # 其他 chat 不受影响
    c = run(cb("m:today"), repo, r)
    assert c == [("answerCallbackQuery", {"callback_query_id": "cq1", "text": "操作太频繁，请稍候再试"})]
    r._t[0] += 2
    assert run(msg("/start"), repo, r)


def test_today_published_only(repo):
    text, _ = tg_bot.render("today", repo[0], repo[1])
    assert "波尔图" in text
    assert "鹿岛" not in text  # 已撤销不在今日推荐


def test_today_no_picks(repo, tmp_path):
    p = next(repo[0].glob("*/*/*.json"))
    r = json.loads(p.read_text(encoding="utf-8"))
    r["date"] = "2026-11-06"
    r["picks"][1]["confidence"] = "medium"   # 新规则下中信心不发布
    r["watchlist"] = [{"home": "WATCHED", "away": "X"}]
    (p.parent / "2026-11-06.json").write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    text, _ = tg_bot.render("today", repo[0], repo[1])
    assert "今日无高信心推荐（宁缺毋滥）" in text
    assert "波尔图" not in text and "WATCHED" not in text


def test_records_show_prematch_and_result(repo):
    text, _ = tg_bot.render("records", repo[0], repo[1])
    assert "❌ 撤销" in text and "取消" in text and "待确认" in text


def test_report_has_url_buttons(repo):
    _, kb = tg_bot.render("report", repo[0], repo[1])
    urls = [b["url"] for row in kb["inline_keyboard"] for b in row if "url" in b]
    assert tg_bot.SITE_URL in urls and any(u.endswith("reports/2026-11-04.html") for u in urls)


def test_sources_no_secrets(repo, monkeypatch):
    st = json.loads((repo[1] / "source_status.json").read_text())
    st["sources"]["odds_api"]["last_error"] = "HTTP 401 for https://api.the-odds-api.com/v4?apiKey=SECRETVALUE123"
    st["sources"]["odds_api"]["last_error_at"] = "2026-11-04T23:00:00Z"
    (repo[1] / "source_status.json").write_text(json.dumps(st))
    monkeypatch.setenv("ODDS_API_KEY", "SECRETVALUE123")
    text, _ = tg_bot.render("sources", repo[0], repo[1])
    assert "SECRETVALUE123" not in text and "🔴" in text


def test_messages_fit_limit(repo):
    for a in ("menu", "today", "sources", "stats", "records", "rules", "report"):
        assert len(tg_bot.render(a, repo[0], repo[1])[0]) <= 4096


def test_log_redacts_token(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABCDEF")
    tg_bot.log("error at https://api.telegram.org/bot123:ABCDEF/getUpdates")
    assert "ABCDEF" not in capsys.readouterr().out


def test_execute_edit_fallback():
    sent = []

    class FakeApi:
        def call(self, m, p=None, timeout=30):
            sent.append(m)
            if m == "editMessageText":
                return {"ok": False, "description": "Bad Request: message can't be edited"}
            return {"ok": True}

    tg_bot.execute(FakeApi(), [("editMessageText", {"chat_id": 1, "message_id": 2, "text": "x"})])
    assert sent == ["editMessageText", "sendMessage"]


def test_no_side_effect_imports():
    src = (ROOT / "src" / "ah" / "tg_bot.py").read_text(encoding="utf-8")
    for bad in ("git push", "git commit", "publish_telegram import send", "pt.send(", "odds_api", "oddspapi", "fivedollar."):
        assert bad not in src
