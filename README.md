# 亚盘（Asian Handicap）每日预测

每日筛选亚洲重点联赛 + 欧洲主流赛事，基于盘口走势与基本面给出 0–5 场亚盘推荐，并持续记录、结算、统计命中率。**仅为数据分析参考，不构成投注建议。**

## 目录结构
```
config/      leagues.yaml(联赛优先级+API-Football id) sources.yaml(数据源分工) settings.yaml(阈值/推送规则) team_aliases.yaml(队名别名)
reports/YYYY/MM/YYYY-MM-DD.json|.md   每日报告（JSON 为源，MD 由模板渲染，内容一致）
data/predictions.csv   总账本  data/stats.json  命中率/ROI 统计
data/cache/   API-Football 缓存与当日用量（gitignore，仅提交 leagues.json 联赛快照）
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
1. `python -m ah.fetch_fixtures [--date D]`（API-Football 优先，失败回退 titan007）/ `python -m ah.fetch_odds`（titan007 亚盘）。
2. 对候选场次调用 `ah.enrich.enrich()` 取伤停/交锋/状态/多公司亚盘，再结合新闻研究，用 `analyze.py` 评分；**无真实盘口不推荐**。
3. 写 `reports/YYYY/MM/DATE.json` → `python -m ah render DATE`（校验 schema、生成 MD、写入账本）。
4. `python -m ah telegram DATE` 推送摘要；`python -m ah site` 生成网站。
5. `python -m ah settle`（有 `API_FOOTBALL_KEY` 时自动回填 90 分钟比分并结算；`--no-autofill` 只结算手填行）→ `python -m ah stats`。

本地：`python -m venv .venv && .venv/bin/pip install -r requirements.txt && PYTHONPATH=src .venv/bin/pytest -q`

## 设置密钥（GitHub → Settings → Secrets and variables → Actions）
- Secrets：`TELEGRAM_BOT_TOKEN`（@BotFather 创建机器人获得）、`TELEGRAM_CHAT_ID`（频道/群/个人 chat id）、`API_FOOTBALL_KEY`（可选，settle 工作流自动回填比分用）
- Variables（可选）：`SITE_URL`（Pages 地址，附在推送消息里）
- Pages：Settings → Pages → Source 选 “GitHub Actions”。
未设置 Telegram 密钥时推送步骤自动跳过。

## 数据源（2026-11-04 更新）

| 数据 | 主源 | 备用 |
|---|---|---|
| 赛程、开球时间、状态 | API-Football `/fixtures?date=`（一次取全天，本地按联赛过滤） | 球探 bfdata |
| 最终比分（结算） | API-Football `/fixtures?id=`（按 90 分钟 `score.fulltime`，加时点球不计） | 球探 bfdata / 手填 |
| 伤停 | API-Football `/injuries?fixture=`（仅欧冠/欧联/五大联赛/英冠/澳超有覆盖） | 新闻预览 |
| 首发 | API-Football `/fixtures/lineups`（开赛前约 20–40 分钟公布） | 官方渠道 |
| 历史交锋 | API-Football `/fixtures/headtohead`（免费档仅 2022–2024 + 当季） | 预览文章 |
| 近期状态 | 本地累积的每日赛程计算（免费档拿不到当季 standings / teams/statistics） | 球探排名、预览 |
| **亚盘盘口/水位** | **球探 goal3.xml（推荐盘口以此为准）** | API-Football `/odds`（Pinnacle、SBO、Bet365 等 10 家，用于交叉核对） |

Key 只从环境变量 `API_FOOTBALL_KEY` 读取，不写进仓库。联赛 id 见 `config/leagues.yaml`（`apif` 字段，已用 `/leagues` 实测核对）。

### 免费档限制（2026-11-04 实测）
- 带 `season=2026/2027` 的查询（`fixtures?league=&season=`、`standings`、`teams/statistics`）返回：`Free plans do not have access to this season, try from 2022 to 2024.`
- `headtohead` 带 `last` 参数返回：`Free plans do not have access to the Last parameter.`（客户端改为取全部后本地截取）
- 不带 season 的 `fixtures?date=`、`fixtures?id=`、`injuries?fixture=`、`fixtures/lineups`、`odds?fixture=` 均可用。
- 亚冠、日职、韩职、中超、沙特、卡塔尔联赛**没有伤停覆盖**（返回空），客户端会跳过这些请求。

### 每日 100 次请求预算（UTC 0 点重置，客户端保留 10 次给结算）
| 用途 | 次数 |
|---|---|
| 日报赛程：今天 + 明天各 1 次（覆盖 36 小时） | 2 |
| 候选场次初筛（约 8 场）：交锋 1 + 多公司亚盘 1 + 伤停 1（仅有覆盖的联赛） | ≈ 20 |
| 赛前 50 分钟复核（≤ 5 场推荐）：首发 1 + 亚盘 1 + 伤停 1 | ≈ 15 |
| 赛后结算（≤ 5 场）：比分 1 | ≈ 5 |
| 临时研究 / 重试缓冲 | ≈ 48 |
| 预留（客户端低于此值拒绝非结算请求） | 10 |

缓存 TTL：赛程 6 小时、交锋 7 天、伤停 3 小时、亚盘 30 分钟、首发公布后永久、完场比分永久；命中缓存不消耗额度。`python -m ah.api_football usage` 查看当日用量。
