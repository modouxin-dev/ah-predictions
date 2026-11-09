"""外部定时一键脚本：payload 精确、密钥不外泄、重复运行不重复建。"""
import json

from scripts import setup_cronjob_org as s


def test_dry_run_never_echoes_secrets():
    o = s.run(dry_run=True, key="CJO_SECRET_ABC", pat="GH_PAT_SECRET_XYZ")
    blob = json.dumps(o, ensure_ascii=False)
    assert "CJO_SECRET_ABC" not in blob
    assert "GH_PAT_SECRET_XYZ" not in blob
    assert "<REDACTED>" in blob
    assert o["credentials"]["cronjob_api_key_present"] is True
    assert o["credentials"]["gh_dispatch_pat_present"] is True
    assert len(o["planned_payloads"]) == 4


def test_four_payloads_match_bj_schedule_and_slot_bodies():
    o = s.run(dry_run=True, key="K", pat="P")
    hours = [p["job"]["schedule"]["hours"][0] for p in o["planned_payloads"]]
    assert hours == [6, 11, 16, 22]
    for p, slot in zip(o["planned_payloads"], ["1", "2", "3", "4"]):
        j = p["job"]
        assert j["requestMethod"] == 1                       # POST
        assert j["schedule"]["timezone"] == "Asia/Shanghai"
        assert j["schedule"]["minutes"] == [0]
        assert json.loads(j["extendedData"]["body"]) == {"ref": "main", "inputs": {"slot": slot}}
        assert j["url"].endswith("/actions/workflows/daily.yml/dispatches")
        assert j["notification"]["onFailure"] is True
        assert j["saveResponses"] is True


def test_missing_credentials_create_nothing(monkeypatch):
    called = []
    o = s.run(dry_run=False, key="", pat="", _api=lambda *a, **k: called.append(a) or (200, {}))
    assert "未取得" in o["error"]
    assert called == []


def test_existing_titles_are_not_created_twice():
    calls = []

    def fake(method, path, key, body=None):
        calls.append((method, path))
        if method == "GET" and path == "/jobs":
            return 200, {"jobs": [{"jobId": 1, "title": s.job_title(j)} for j in s.JOBS]}
        if method == "PUT":
            return 200, {"jobId": 99}
        return 200, {}

    o = s.run(dry_run=False, key="K", pat="P", _api=fake)
    assert not any(m == "PUT" for m, _ in calls)
    assert all(j["action"] == "already_exists_skipped" for j in o["jobs"])


def test_creates_four_jobs_and_reads_history():
    calls = []

    def fake(method, path, key, body=None):
        calls.append((method, path))
        if method == "GET" and path == "/jobs":
            return 200, {"jobs": []}
        if method == "PUT":
            return 201, {"jobId": 700 + len([c for c in calls if c[0] == "PUT"])}
        if "/history" in path:
            return 200, {"history": []}
        return 200, {}

    o = s.run(dry_run=False, want_history=True, key="K", pat="P", _api=fake)
    assert len([c for c in calls if c[0] == "PUT"]) == 4
    assert all(j["status"] == 201 and "job_id" in j for j in o["jobs"])
    assert all(j.get("history_status") == 200 for j in o["jobs"])
    assert "Test run 无 REST API 端点" in " ".join(o["unverified"])
