"""DATA SOURCE: 第三比分源只读核对，绝不编造或自动改写结算。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ah import score_truth as st

SOURCES = [{"source_id": "s1", "source_url": "https://example.org/a?token=SECRET", "parser": "generic",
            "as_of": "2026-11-08", "coverage_status": "test", "headers": {}},
           {"source_id": "s2", "source_url": "https://example.org/b", "parser": "generic",
            "as_of": "2026-11-08", "coverage_status": "test", "headers": {}}]


def _ledger(p: Path, rows):
    import csv
    cols = ["date", "match_id", "home", "away", "result", "settled"]
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in rows: w.writerow({c: r.get(c, "") for c in cols})


def test_url_query_is_never_persisted():
    claims = st.parse_claims("Alpha 2-1 Beta\n", SOURCES[0])
    assert claims and "token" not in json.dumps(claims, ensure_ascii=False)
    assert claims[0]["source_url"] == "https://example.org/a"


def test_unparseable_text_yields_no_claims():
    assert st.parse_claims("<html>no scores here</html>", SOURCES[0]) == []
    assert st.parse_claims("", SOURCES[0]) == []
    assert st.parse_claims("a 99-88 b", SOURCES[0]) == []   # 非足球比分被拒


def test_fetch_failure_is_fact_not_invention():
    def boom(url, headers, timeout): raise OSError("network down")
    claims, facts = st.fetch_claims(SOURCES, fetcher=boom)
    assert claims == []
    assert all(f["status"] == "NO_EXPLICIT_SCORE_FOUND" for f in facts)
    assert facts[0]["error_type"] == "OSError"


def test_agree_and_disagree(tmp_path):
    def fake(url, headers, timeout): return "Alpha 2-1 Beta\nAlpha 3-0 Beta\n"
    claims, _ = st.fetch_claims(SOURCES, fetcher=fake)
    row = {"match_id": "m1", "home": "Alpha", "away": "Beta", "result": "2-1"}
    # 同一 source 给了两个不同比分 -> DISAGREE（多源不一致也不粉饰）
    v = st.verify_row(row, claims)
    assert v["verdict"] == "DISAGREE" and "不自动改写" in v["note"]
    one = [c for c in claims if c["score"] == "2-1"]
    assert st.verify_row(row, one)["verdict"] == "AGREE"


def test_no_match_is_not_reported_as_agree():
    claims = st.parse_claims("Alpha 2-1 Beta", SOURCES[0])
    row = {"match_id": "m2", "home": "Gamma", "away": "Delta", "result": "2-1"}
    assert st.verify_row(row, claims)["verdict"] == "THIRD_UNAVAILABLE"


def test_fallback_requires_two_independent_sources_and_switch():
    c1 = st.parse_claims("Alpha 2-1 Beta", SOURCES[0])
    c2 = st.parse_claims("Alpha 2-1 Beta", SOURCES[1])
    row = {"match_id": "m1", "home": "Alpha", "away": "Beta", "result": ""}
    assert st.fallback_score(row, c1 + c2, enabled=False) is None
    assert st.fallback_score(row, c1, enabled=True) is None
    assert st.fallback_score(row, c1 + c2, enabled=True) == "2-1"
    # 主源已有比分绝不覆盖
    assert st.fallback_score({**row, "result": "1-1"}, c1 + c2, enabled=True) is None


def test_check_is_readonly_and_records_facts(tmp_path):
    p = tmp_path / "predictions.csv"
    _ledger(p, [{"date": "2026-11-04", "match_id": "m1", "home": "Alpha", "away": "Beta",
                 "result": "2-1", "settled": "true"}])
    out = st.check(p, SOURCES, fetcher=lambda u, h, t: "Alpha 2-1 Beta", date="2026-11-04")
    assert out["ledger_rows"] == 1 and out["claims_total"] == 2
    assert out["verdict_counts"]["AGREE"] == 1  # 两个独立源给出一致比分，与主源一致
    assert out["verdict_counts"]["DISAGREE"] == 0
    assert "不补造" in out["note"]
    assert p.read_text(encoding="utf-8").count("2-1") >= 1


def test_source_declaration_requires_full_metadata(tmp_path):
    p = tmp_path / "sources.yaml"
    p.write_text("score_truth:\n  sources:\n    - source_id: bad\n      source_url: not-a-url\n      enabled: true\n", encoding="utf-8")
    assert st.load_sources(p) == []
