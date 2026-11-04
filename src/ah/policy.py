"""准确率优先的发布策略（config/settings.yaml: policy）。

- 只发布信心「高」的推荐；其余进入 report["watchlist"]（内部字段，不推送、不在网站作为推荐展示）。
- 必须有真实赔率；推荐盘口偏离多家公司中位数 > 0.25 时降级一档，偏离 > 0.5 直接撤下。
- 赛前最终确认信心下降 -> 撤销（cancel_on_drop）。
- 生效日之前的历史报告保持原样（作为历史记录）。
"""
from __future__ import annotations

from pathlib import Path

import yaml

CN = {"high": "高", "medium": "中", "low": "低", "高": "高", "中": "中", "低": "低"}
EN = {"高": "high", "中": "medium", "低": "low", "high": "high", "medium": "medium", "low": "low"}
ORDER = {"high": 0, "medium": 1, "low": 2}
DOWN = {"high": "medium", "medium": "low", "low": None}
DISCLAIMER = "基于数据的分析参考，亚盘有风险，不构成投注建议。"
DEFAULT = {"min_confidence_to_publish": "高", "max_picks_per_day": 3, "require_real_odds": True,
           "consensus_alignment": "pick line within 0.25 of multi-book median, else downgrade or skip",
           "alignment_tolerance": 0.25, "prematch_cancel_on_confidence_drop": True, "effective_from": "2026-11-05"}


def load(path: Path | None = None) -> dict:
    path = path or Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        cfg = {}
    return {**DEFAULT, **(cfg.get("policy") or {})}


def applies(report: dict, pol: dict) -> bool:
    return str(report.get("date", "")) >= str(pol.get("effective_from", ""))


def check(p: dict, pol: dict) -> tuple[str | None, str]:
    """返回 (调整后信心 or None=不发布, 原因)。"""
    conf = EN.get(p.get("confidence", ""), None)
    if conf is None:
        return None, "无信心等级"
    if pol["require_real_odds"] and p.get("odds") is None:
        return None, "无真实赔率"
    mc = p.get("market_consensus") or {}
    if mc.get("pick_line") is not None and p.get("line") is not None:
        gap = abs(float(p["line"]) - float(mc["pick_line"]))
        tol = float(pol.get("alignment_tolerance", 0.25))
        if gap > 2 * tol:
            return None, f"盘口偏离多家中位数 {gap:g}"
        if gap > tol:
            conf = DOWN[conf]
            if conf is None:
                return None, f"盘口偏离多家中位数 {gap:g}，降级后不足"
    if ORDER[conf] > ORDER[EN[pol["min_confidence_to_publish"]]]:
        return None, f"信心{CN[conf]}，低于发布门槛{CN[pol['min_confidence_to_publish']]}"
    return conf, ""


def apply(report: dict, pol: dict | None = None) -> dict:
    """就地过滤 report["picks"]；被拦下的进 report["watchlist"]。历史报告不改。"""
    pol = pol or load()
    if not applies(report, pol):
        return report
    keep, wl = [], list(report.get("watchlist", []))
    for p in report.get("picks", []):
        conf, why = check(p, pol)
        if conf is None:
            wl.append({**p, "withheld_reason": why})
        else:
            keep.append({**p, "confidence": conf})
    keep.sort(key=lambda p: -float(p.get("score", 0)))
    for p in keep[pol["max_picks_per_day"]:]:
        wl.append({**p, "withheld_reason": "超过每日上限"})
    report["picks"] = keep[: pol["max_picks_per_day"]]
    report["watchlist"] = wl
    return report


def published(report: dict, pol: dict | None = None) -> list[dict]:
    """可公开展示/推送的推荐（不修改报告）。历史报告原样返回。"""
    pol = pol or load()
    live = [p for p in report.get("picks", []) if not p.get("cancelled") and p.get("confidence") != "cancelled"
            and (p.get("final_check") or {}).get("decision") != "cancel"]
    if not applies(report, pol):
        return live
    return [p for p in live if check(p, pol)[0] is not None]


def cancel_on_drop(p: dict, new_conf: str, pol: dict | None = None, reason: str = "") -> bool:
    """赛前最终确认：信心下降则撤销。返回是否撤销。"""
    pol = pol or load()
    old, new = EN.get(p.get("confidence", ""), "low"), EN.get(new_conf, "low")
    if pol["prematch_cancel_on_confidence_drop"] and ORDER[new] > ORDER[old]:
        p["cancelled"] = True
        p.setdefault("final_check", {})["decision"] = "cancel"
        p["cancel_reason"] = reason or f"赛前信心由{CN[old]}降至{CN[new]}"
        return True
    return False
