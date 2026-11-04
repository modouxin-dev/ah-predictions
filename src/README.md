# src/ah — 流水线脚本

| 模块 | 作用 | 状态 |
|---|---|---|
| api_football.py | API-Football v3 客户端：额度跟踪(预留10)、磁盘缓存、赛程/伤停/首发/交锋/比分/亚盘 | 可用，有离线测试 |
| match.py | API-Football ↔ 球探/报告 比赛映射（开球±15分钟+队名模糊匹配+team_aliases.yaml） | 可用 |
| enrich.py | 为候选场生成伤停/交锋/状态/多公司亚盘及建议因子分 | 可用 |
| fetch_fixtures.py | API-Football 优先，titan007 bfdata 备用 | 可用 |
| fetch_odds.py | titan007 goal3.xml 亚盘初/即、欧赔、大小球 | 可用（单一默认公司） |
| analyze.py | 因子加权评分，只输出过阈值且有真实盘口的场次 | 框架完成，基本面特征需研究代理填写 |
| render_report.py | JSON → Markdown（templates/report.md.j2），schema 校验，写入 predictions.csv | 可用 |
| settle.py | API-Football 自动回填比分 + 亚盘结算（含四分之一盘）+ 单位盈亏 | 可用，有单元测试 |
| stats.py | 总体/联赛/信心/盘口 命中率与 ROI → data/stats.json | 可用，有单元测试 |
| publish_telegram.py | 读 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 发送摘要 | 可用（未实发测试） |
| build_site.py | 生成 site/ 静态页（最新/历史/统计） | 可用 |

运行：`PYTHONPATH=src python -m ah render|settle|stats|site|telegram ...`
