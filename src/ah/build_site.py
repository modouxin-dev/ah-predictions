"""生成 site/ 静态网站：index(最新报告) / history / stats，以及每日报告页。"""
from __future__ import annotations

import html
import json
from pathlib import Path

from . import DATA, REPORTS, SITE

CONF = {"high": "高", "medium": "中", "low": "低"}
CSS = """body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;max-width:860px;margin:auto;padding:16px;line-height:1.6;color:#222}
nav a{margin-right:16px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:6px;text-align:left}
.pick{border:1px solid #ccc;border-radius:8px;padding:12px;margin:12px 0}.tag{background:#0a7;color:#fff;border-radius:4px;padding:2px 6px}
small{color:#777}"""


def page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>"
            f"<nav><a href='index.html'>最新</a><a href='history.html'>历史</a><a href='stats.html'>统计</a></nav>"
            f"<h1>{html.escape(title)}</h1>{body}<hr><small>仅为数据分析参考，不构成投注建议。</small></body></html>")


def report_html(r: dict) -> str:
    e = html.escape
    out = [f"<p><small>生成于 {e(r['generated_at'])} · {e(r.get('odds_format', ''))}</small></p>"]
    if not r["picks"]:
        out.append("<p>今日无推荐。</p>")
    for p in r["picks"]:
        team = p.get("pick_team", p[p["pick_side"]])
        odds = f" @ {p['odds']}" if p.get("odds") is not None else ""
        out.append(f"<div class='pick'><b>{e(p['home'])} vs {e(p['away'])}</b> <small>{e(p['league'])} · {e(p['kickoff_utc'])}</small>"
                   f"<p><span class='tag'>{e(team)} {p['line']:+g}{odds}</span> 信心：{CONF[p['confidence']]}</p>"
                   "<b>理由</b><ol>" + "".join(f"<li>{e(x)}</li>" for x in p["reasons"]) + "</ol>"
                   "<b>风险</b><ul>" + "".join(f"<li>{e(x)}</li>" for x in p["risks"]) + "</ul>"
                   "<b>来源</b><ul>" + "".join(f"<li><a href='{e(s)}'>{e(s)}</a></li>" for s in p["sources"]) + "</ul></div>")
    out.append("<h2>观察但不推荐</h2><table><tr><th>赛事</th><th>对阵</th><th>原因</th></tr>" +
               "".join(f"<tr><td>{e(w['league'])}</td><td>{e(w['home'])} vs {e(w['away'])}</td><td>{e(w['reason_skipped'])}</td></tr>" for w in r["watched"]) +
               "</table>")
    return "".join(out)


def stats_html(s: dict) -> str:
    def table(title, d):
        rows = "".join(f"<tr><td>{html.escape(k)}</td><td>{v['picks']}</td><td>{v['win']}/{v['half_win']}/{v['push']}/{v['half_loss']}/{v['loss']}</td>"
                       f"<td>{v['hit_rate']:.1%}</td><td>{v['units']:+.2f}</td><td>{v['roi']:+.1%}</td></tr>" for k, v in d.items())
        return (f"<h2>{title}</h2><table><tr><th></th><th>场次</th><th>赢/赢半/走/输半/输</th><th>命中率</th><th>盈亏(单位)</th><th>ROI</th></tr>{rows}</table>")
    return (table("总体", {"全部": s["overall"]}) + table("按联赛", s.get("by_league", {})) +
            table("按信心", s.get("by_confidence", {})) + table("按盘口", s.get("by_line", {})) +
            f"<p><small>更新于 {html.escape(s.get('updated_at', ''))}</small></p>")


def build(site: Path = SITE) -> list[str]:
    (site / "reports").mkdir(parents=True, exist_ok=True)
    files = sorted(REPORTS.glob("*/*/*.json"))
    reps = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    for r in reps:
        (site / "reports" / f"{r['date']}.html").write_text(
            page(f"亚盘日报 {r['date']}", report_html(r)).replace("href='index.html'", "href='../index.html'")
            .replace("href='history.html'", "href='../history.html'").replace("href='stats.html'", "href='../stats.html'"), encoding="utf-8")
    latest = reps[-1] if reps else None
    (site / "index.html").write_text(page(f"亚盘日报 {latest['date']}" if latest else "亚盘日报", report_html(latest) if latest else "<p>暂无报告</p>"), encoding="utf-8")
    hist = "<ul>" + "".join(f"<li><a href='reports/{r['date']}.html'>{r['date']}</a> · 推荐 {len(r['picks'])} 场</li>" for r in reversed(reps)) + "</ul>"
    (site / "history.html").write_text(page("历史报告", hist), encoding="utf-8")
    sp = DATA / "stats.json"
    s = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else None
    (site / "stats.html").write_text(page("战绩统计", stats_html(s) if s else "<p>暂无统计</p>"), encoding="utf-8")
    (site / ".nojekyll").write_text("")
    return [r["date"] for r in reps]


if __name__ == "__main__":
    print("built site for", build())
