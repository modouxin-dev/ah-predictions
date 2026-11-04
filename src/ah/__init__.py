"""ah: 亚盘预测流水线 (fetch -> analyze -> render -> publish -> settle -> stats -> site)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
CONFIG = ROOT / "config"
SITE = ROOT / "site"
TEMPLATES = ROOT / "templates"
SCHEMA = ROOT / "schema"
