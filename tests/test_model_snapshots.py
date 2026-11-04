"""盘口快照只读读取器（src/ah/model/snapshots.py）+ 基准纸面交易 margin 标记。"""
import subprocess
from datetime import datetime, timezone

import pytest

from ah.model import snapshots as sn

HDR = ("match_id,captured_at,stage,bookmaker,ah_home_line,odds_home,odds_away,odds_format,source,verification,"
       "match_verification,is_main,home_hk,away_hk,as_of,price_ts,report_date,fixture_id,kickoff_utc,league_id,"
       "league,home,away,status_detail,source_ref\n")


def row(mid="apif-1", cap="2026-11-04T13:01:31Z", stage="report", book="pinnacle", line="-0.5", oh="1.95",
        oa="1.95", src="oddspapi", ver="verified", pts="2026-11-04T12:58:00Z", ko="2026-11-04T20:00:00Z"):
    return f"{mid},{cap},{stage},{book},{line},{oh},{oa},decimal,{src},{ver},{ver},1,,,{cap},{pts},2026-11-05,1,{ko},x,L,H,A,,\n"


def test_parse_and_missing_rows_dropped():
    txt = HDR + row() + "apif-2,2026-11-04T13:01:31Z,report,,,,,decimal,,missing,missing,,,,,,2026-11-05,2,,x,L,H,A,no,\n"
    rows = sn.parse_rows(txt)
    assert len(rows) == 1 and rows[0].ah_home_line == -0.5 and rows[0].bookmaker == "pinnacle"
    assert rows[0].captured_at == datetime(2026, 11, 4, 13, 1, 31, tzinfo=timezone.utc)


def test_missing_required_column_rejected():
    with pytest.raises(ValueError):
        sn.parse_rows("match_id,stage\napif-1,report\n")


def test_pick_price_prefers_oddspapi_and_respects_before():
    txt = HDR + row(src="api_football", oh="1.90", oa="1.90") + row(oh="1.95", oa="1.95") + \
        row(cap="2026-11-04T21:00:00Z", oh="2.5", oa="1.5")
    rows = sn.parse_rows(txt)
    ko = datetime(2026, 11, 4, 20, tzinfo=timezone.utc)
    r = sn.pick_price(rows, "apif-1", -0.5, before=ko)
    assert r.source == "oddspapi" and r.odds_home == 1.95  # 开赛后的 21:00 记录被排除
    assert sn.pick_price(rows, "apif-1", -0.25) is None   # 不存在的盘口：不估算
    assert sn.pick_price(rows, "apif-1", -0.5, stage="close") is None


def test_find_file_new_then_old(tmp_path):
    (tmp_path / "data/odds_snapshots").mkdir(parents=True)
    (tmp_path / "data/odds_snapshots/2026-11-05.csv").write_text(HDR)
    assert sn.find_snapshot_file(tmp_path, "2026-11-05").parts[-2] == "odds_snapshots"
    (tmp_path / "data/snapshots/odds").mkdir(parents=True)
    (tmp_path / "data/snapshots/odds/2026-11-05.csv").write_text(HDR)
    assert sn.find_snapshot_file(tmp_path, "2026-11-05").parts[-3:-1] == ("snapshots", "odds")
    assert sn.find_snapshot_file(tmp_path, "2026-11-06") is None


def test_read_from_git_old_path(tmp_path):
    g = ["git", "-C", str(tmp_path)]
    subprocess.run(g + ["init", "-q"], check=True)
    (tmp_path / "data/odds_snapshots").mkdir(parents=True)
    (tmp_path / "data/odds_snapshots/2026-11-05.csv").write_text(HDR + row())
    subprocess.run(g + ["add", "."], check=True)
    subprocess.run(g + ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"], check=True)
    text, label = sn.read_text_from_git(tmp_path, "HEAD", "2026-11-05")
    assert "data/odds_snapshots/2026-11-05.csv" in label and len(sn.parse_rows(text)) == 1


def test_margin():
    assert sn.margin(2.0, 2.0) == pytest.approx(0.0)
    assert sn.margin(1.884, 1.813) == pytest.approx(1 / 1.884 + 1 / 1.813 - 1)
