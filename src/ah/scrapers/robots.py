"""robots.txt 解析（RFC 9309 + Google 通配扩展：* 与 $；最长匹配优先，等长时 Allow 优先）。

标准库 urllib.robotparser 不支持通配符（J.League 的 Disallow: /match/*/201* 会被当成字面量），故自实现。
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


def _pattern_to_regex(p: str) -> re.Pattern:
    anchored = p.endswith("$")
    if anchored:
        p = p[:-1]
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in p)
    return re.compile(rx + ("$" if anchored else ""))


class Robots:
    def __init__(self, text: str):
        self.groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
        agents: list[str] = []
        rules: list[tuple[bool, str]] = []
        last_was_agent = False
        for raw in (text or "").splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if k == "user-agent":
                if not last_was_agent and (agents or rules):
                    self.groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(v.lower())
                last_was_agent = True
            elif k in ("allow", "disallow"):
                last_was_agent = False
                if agents:
                    rules.append((k == "allow", v))
            else:
                last_was_agent = False if k != "sitemap" else last_was_agent
        if agents:
            self.groups.append((agents, rules))

    def _rules_for(self, user_agent: str) -> list[tuple[bool, str]]:
        token = user_agent.split("/", 1)[0].strip().lower()
        specific = [r for a, r in self.groups if any(x != "*" and x and x in token for x in a)]
        if specific:
            return [x for r in specific for x in r]
        return [x for a, r in self.groups if "*" in a for x in r]

    def can_fetch(self, user_agent: str, url: str) -> bool:
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        best_len, allowed = -1, True
        for allow, pat in self._rules_for(user_agent):
            if pat == "":
                continue  # 空 Disallow = 全部允许
            m = _pattern_to_regex(pat).match(path)
            if m:
                ln = len(pat)
                if ln > best_len or (ln == best_len and allow):
                    best_len, allowed = ln, allow
        return allowed
