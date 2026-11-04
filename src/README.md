# src/ah — 流水线脚本

| 模块 | 作用 | 状态 |
|---|---|---|
| fetch_fixtures.py | titan007 bfdata 当日赛程/比分 | 可用（字段映射为经验值） |
| fetch_odds.py | titan007 goal3.xml 亚盘初/即、欧赔、大小球 | 可用（单一默认公司） |
| analyze.py | 因子加权评分，只输出过阈值且有真实盘口的场次 | 框架完成，基本面特征需研究代理填写 |
| render_report.py | JSON → Markdown（templates/report.md.j2），schema 校验，写入 predictions.csv | 可用 |
| settle.py | 亚盘结算（含 ±0.25/±0.75 四分之一盘）+ 单位盈亏 | 可用，有单元测试 |
| stats.py | 总体/联赛/信心/盘口 命中率与 ROI → data/stats.json | 可用，有单元测试 |
| publish_telegram.py | 读 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 发送摘要 | 可用（未实发测试） |
| build_site.py | 生成 site/ 静态页（最新/历史/统计） | 可用 |

运行：`PYTHONPATH=src python -m ah render|settle|stats|site|telegram ...`
