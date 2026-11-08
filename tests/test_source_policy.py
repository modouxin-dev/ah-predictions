"""DATA SOURCE: 注册源声明为可选增强；只判注入，不判有效性。"""
from __future__ import annotations

import json

from ah import source_policy as sp

POLICY = [{"source_id": "oddspapi", "env_var": "ODDSPAPI_KEY", "client_file": "src/ah/oddspapi.py",
           "mode": "optional_enhancement", "on_missing": "skip_with_fact",
           "as_of": "2026-11-08", "coverage_status": "test"}]


def test_decisions_only_reflect_injection():
    d = sp.decisions(POLICY, {})
    assert d[0]["decision"] == "skipped_no_credential" and d[0]["credential_present"] is False
    d2 = sp.decisions(POLICY, {"ODDSPAPI_KEY": "any-value"})
    assert d2[0]["decision"] == "active" and d2[0]["credential_present"] is True
    assert "不代表" in d2[0]["note"]


def test_should_skip_only_known_optional_sources():
    assert sp.should_skip("oddspapi", {}, POLICY) is True
    assert sp.should_skip("ODDSPAPI_KEY", {"ODDSPAPI_KEY": "x"}, POLICY) is False
    assert sp.should_skip("unknown", {}, POLICY) is False


def test_static_contract_check_verifies_real_gate():
    out = sp.static_contract_check(POLICY)
    assert out[0]["status"] == "CREDENTIAL_GATE_PRESENT"
    assert out[0]["env_var_referenced"] is True
    bad = [dict(POLICY[0], client_file="src/ah/nonexistent.py")]
    assert sp.static_contract_check(bad)[0]["status"] == "GATE_NOT_VERIFIED"


def test_check_summary_and_boundaries():
    out = sp.check(env={})
    s = out["summary"]
    assert s["declared"] == 5 and s["active"] == 0 and s["skipped_no_credential"] == 5
    assert s["credential_gate_present"] == 5 and s["gate_not_verified"] == 0
    assert "不读值" in out["note"] and "不等于" in out["note"]
    assert any("403" in x for x in out["unverified"])


def test_never_persists_secret_values():
    out = sp.check(env={"ODDSPAPI_KEY": "SUPER_SECRET_VALUE"})
    assert "SUPER_SECRET_VALUE" not in json.dumps(out, ensure_ascii=False)
