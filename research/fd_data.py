"""football-data.co.uk 历史数据：下载（缓存到 research/data/raw/）+ 统一读取。"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import requests

RAW = Path(__file__).resolve().parent / "data" / "raw"
BASE = "https://www.football-data.co.uk"
MAIN = {"E0": "英超", "E1": "英冠", "SP1": "西甲", "I1": "意甲", "D1": "德甲", "F1": "法甲", "N1": "荷甲", "P1": "葡超"}
SEASONS = ["1920", "2021", "2122", "2223", "2324", "2425", "2526", "2627"]
NEW = {"JPN": "日职联", "CHN": "中超"}
UA = {"User-Agent": "ah-predictions research (modouxin-dev)"}


def download(force: bool = False) -> list[Path]:
    RAW.mkdir(parents=True, exist_ok=True)
    targets = [(f"{BASE}/mmz4281/{s}/{d}.csv", RAW / f"{d}_{s}.csv") for s in SEASONS for d in MAIN]
    targets += [(f"{BASE}/new/{c}.csv", RAW / f"new_{c}.csv") for c in NEW]
    out = []
    for url, path in targets:
        if path.exists() and not force:
            out.append(path)
            continue
        r = requests.get(url, headers=UA, timeout=60)
        if r.status_code == 200 and r.content[:3] in (b"Div", b"Cou", b"\xef\xbb\xbf"):
            path.write_bytes(r.content)
            out.append(path)
        time.sleep(1)
    return out


def _num(df, cols):
    for c in cols:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def load_main(div: str) -> pd.DataFrame:
    frames = []
    for s in SEASONS:
        p = RAW / f"{div}_{s}.csv"
        if not p.exists():
            continue
        df = pd.read_csv(p, encoding="latin-1", on_bad_lines="skip")
        df = df.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"])
        df = df.copy()
        df["season"] = s
        frames.append(df)
    df = pd.concat(frames, ignore_index=True).copy()
    df["date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
    df = df.rename(columns={"HomeTeam": "home", "AwayTeam": "away", "FTHG": "hg", "FTAG": "ag"})
    df = _num(df, ["hg", "ag", "PSH", "PSD", "PSA", "PSCH", "PSCD", "PSCA", "AHh", "PAHH", "PAHA",
                   "AHCh", "PCAHH", "PCAHA", "MaxCAHH", "MaxCAHA", "AvgAHH", "AvgAHA"])
    df["league"] = div
    return df.sort_values("date").reset_index(drop=True)


def load_new(code: str) -> pd.DataFrame:
    df = pd.read_csv(RAW / f"new_{code}.csv", encoding="latin-1", on_bad_lines="skip")
    df = df.dropna(subset=["Home", "Away", "HG", "AG"])
    df["date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
    df = df.rename(columns={"Home": "home", "Away": "away", "HG": "hg", "AG": "ag", "Season": "season"})
    df = _num(df, ["hg", "ag", "PSCH", "PSCD", "PSCA"])
    df["league"] = code
    df["season"] = df["season"].astype(str)
    return df.sort_values("date").reset_index(drop=True)
