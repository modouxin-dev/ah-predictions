import json
from pathlib import Path
from ah import publish_telegram, render_report

R = json.loads(Path("reports/2026/11/2026-11-04.json").read_text())

def test_telegram_prefix():
    assert publish_telegram.summary(R).startswith("【先知】")

def test_md_brand():
    md = render_report.render_md(R)
    assert "# 【先知】亚盘日报 2026-11-04" in md and md.rstrip().endswith("—— 先知")
