# 亚盘（Asian Handicap）每日预测

每日筛选亚洲重点联赛 + 欧洲主流赛事，基于盘口走势与基本面给出 0–5 场亚盘推荐，并持续记录、结算、统计命中率。**仅为数据分析参考，不构成投注建议。**

## 目录结构
```
config/      leagues.yaml(联赛优先级) sources.yaml(数据源可用性) settings.yaml(阈值/每日上限/时区)
reports/YYYY/MM/YYYY-MM-DD.json|.md   每日报告（JSON 为源，MD 由模板渲染，内容一致）
data/predictions.csv   总账本  data/stats.json  命中率/ROI 统计
schema/report.schema.json  日报 JSON Schema
templates/report.md.j2     日报 Markdown 模板
src/ah/      流水线（见 src/README.md）
tests/       pytest 单元测试
site/        静态站点输出（GitHub Pages）
.github/workflows/  daily.yml / settle.yml / pages.yml
```

## 日报 JSON 格式
`date, generated_at, picks[{match_id, league, home, away, kickoff_utc, pick_side(home/away), line, odds, confidence(high/medium/low), reasons[], risks[], sources[]}], watched[{league, home, away, reason_skipped}]`
- `line`：所选一方的让球数，负=让球（-0.75=让半一），正=受让。
- `odds`：港盘水位（HK，十进制赔率 = 1 + 水位）。

## predictions.csv 列
`date,match_id,league,home,away,kickoff_utc,pick_side,pick,line,odds_if_known,confidence,result,ah_outcome,units,settled`
- `result`：完场比分“主-客”，如 `2-1`；`ah_outcome`：win/half_win/push/half_loss/loss；`units`：每注 1 单位的盈亏。

## 每日流程
1. `python -m ah.fetch_fixtures` / `python -m ah.fetch_odds` 拉取赛程与盘口（titan007）。
2. 研究基本面（伤停、状态、主客场、xG、战意），用 `analyze.py` 评分；**无真实盘口不推荐**。
3. 写 `reports/YYYY/MM/DATE.json` → `python -m ah render DATE`（校验 schema、生成 MD、写入账本）。
4. `python -m ah telegram DATE` 推送摘要；`python -m ah site` 生成网站。
5. 次日填写比分 → `python -m ah settle` → `python -m ah stats`。

本地：`python -m venv .venv && .venv/bin/pip install -r requirements.txt && PYTHONPATH=src .venv/bin/pytest -q`

## 设置密钥（GitHub → Settings → Secrets and variables → Actions）
- Secrets：`TELEGRAM_BOT_TOKEN`（@BotFather 创建机器人获得）、`TELEGRAM_CHAT_ID`（频道/群/个人 chat id）
- Variables（可选）：`SITE_URL`（Pages 地址，附在推送消息里）
- Pages：Settings → Pages → Source 选 “GitHub Actions”。
未设置 Telegram 密钥时推送步骤自动跳过。
