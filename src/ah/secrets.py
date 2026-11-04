"""密钥加载：先读环境变量，再回退到 secrets 文件（默认 /home/box/.config/ah-secrets.env，
可用 AH_SECRETS_FILE 覆盖）。文件格式 KEY=VALUE，支持 # 注释与引号。绝不打印/记录值。"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_FILE = "/home/box/.config/ah-secrets.env"
KNOWN = ("API_FOOTBALL_KEY", "ODDS_API_KEY", "ODDSPAPI_KEY", "FOOTBALL_DATA_KEY", "FIVEDOLLAR_KEY")


def _parse(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def get_secret(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v:
        return v
    path = Path(os.environ.get("AH_SECRETS_FILE", DEFAULT_FILE))
    return _parse(path).get(name) or default
