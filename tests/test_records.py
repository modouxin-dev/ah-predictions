import copy
import json
from pathlib import Path

import pytest

from ah import records

R = json.loads(Path("reports/2026/11/2026-11-04.json").read_text())
POL = {"min_confidence_to_publish": "高", "max_picks_per_day": 3, "require_real_odds": True,
       "alignment_tolerance": 0.25, "prematch_cancel_on_confidence_drop": True, "effective_from": "2026-11-05"}


def rep():
    return copy.deepcopy(R)


def test_backfilled_kashima_cancelled_and_void():
    p = records.find_pick(rep(), "titan007-3068888")
    assert records.status(p) == "已撤销"
    pm = records.prematch_of(p)
    assert pm["decision"] == "撤销" and pm["checked_at"] == "2026-11-04T11:33:00Z"
    assert p["result"]["settlement"] == "void" and p["result"]["units"] == 0


def test_porto_pending():
    p = records.find_pick(rep(), "titan007-3073152")
    assert records.status(p) == "待确认" and records.prematch_of(p) is None


def test_legacy_final_check_mapping():
    p = {"final_check": {"checked_at": "2026-11-04T11:33:00Z", "decision": "cancel", "reason": "x"}}
    assert records.prematch_of(p)["decision"] == "撤销" and records.status(p) == "已撤销"


def test_prematch_keep_then_settle():
    r = rep()
    records.set_prematch(r, "titan007-3073152", "保留", "盘口稳定", checked_at="2026-11-04T19:10:00Z", pol=POL)
    p = records.find_pick(r, "titan007-3073152")
    assert records.status(p) == "已确认"
    res = records.set_result(r, "titan007-3073152", "2-0", review="主队控场")
    assert res["settlement"] == "win" and res["units"] == 1.06 and res["settlement_cn"] == "赢"
    assert records.status(p) == "已结算"
    res = records.set_result(r, "titan007-3073152", "1-0")         # 复盘保留
    assert res["settlement"] == "half_win" and res["review"] == "主队控场"


def test_prematch_adjust_uses_new_line():
    r = rep()
    records.set_prematch(r, "titan007-3073152", "adjust", "降盘", new_line=-0.5, new_odds=0.9, pol=POL)
    res = records.set_result(r, "titan007-3073152", "1-1")
    assert res["line"] == -0.5 and res["settlement"] == "loss"


def test_confidence_drop_auto_cancels():
    r = rep()
    pm = records.set_prematch(r, "titan007-3073152", "保留", "首发变化", confidence="低", pol=POL)
    assert pm["decision"] == "撤销" and "自动撤销" in pm["reason"]
    assert records.set_result(r, "titan007-3073152", None)["settlement"] == "void"


@pytest.mark.parametrize("kw", [{"decision": "maybe", "reason": "x"}, {"decision": "调整", "reason": "x"},
                                {"decision": "保留", "reason": ""}, {"decision": "调整", "reason": "x", "new_line": -0.3}])
def test_prematch_validation(kw):
    with pytest.raises(ValueError):
        records.set_prematch(rep(), "titan007-3073152", pol=POL, **kw)


def test_sync_csv_roundtrip(tmp_path):
    import shutil
    c = tmp_path / "p.csv"
    shutil.copy("data/predictions.csv", c)
    r = rep()
    records.set_prematch(r, "titan007-3073152", "保留", "ok", pol=POL)
    records.set_result(r, "titan007-3073152", "0-2", review="r")
    assert records.sync_csv(r, c) == 2
    import csv
    rows = {x["match_id"]: x for x in csv.DictReader(c.open())}
    assert rows["titan007-3073152"]["ah_outcome"] == "loss" and rows["titan007-3073152"]["settled"] == "true"
    assert rows["titan007-3068888"]["ah_outcome"] == "void"


def test_sync_from_csv(tmp_path):
    reports = tmp_path / "reports"
    (reports / "2026" / "11").mkdir(parents=True)
    (reports / "2026" / "11" / "2026-11-04.json").write_text(json.dumps(R, ensure_ascii=False))
    c = tmp_path / "p.csv"
    c.write_text(Path("data/predictions.csv").read_text().replace(",medium,,,,false", ",medium,3-1,win,1.06,true"))
    assert records.sync_from_csv(c, reports) == ["2026-11-04 titan007-3073152"]
    r = records.load_report("2026-11-04", reports)
    assert records.find_pick(r, "titan007-3073152")["result"]["score"] == "3-1"
