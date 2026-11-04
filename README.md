# 先知 · 亚盘（Asian Handicap）每日预测

本项目的推送/报告机器人名为「先知」：日报标题为「【先知】亚盘日报 YYYY-MM-DD」，结尾署名「—— 先知」，站点标题「先知 · 亚盘预测」。

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

## The Odds API（多公司亚盘交叉核对）

- 接口：`https://api.the-odds-api.com/v4`，客户端 `src/ah/odds_api.py`，交叉核对入口 `fetch_odds.cross_check_odds_api(sport_key)`（返回各公司主盘 + 中位数共识盘口、公司数）。
- 优先级：API-Football 多公司亚盘为主 → The Odds API 交叉核对 → titan007 备用；失败/无 Key/额度不足时自动跳过，不影响主流程。
- 免费档 500 credits/月，每次 odds 调用花费 = markets × regions（默认 `spreads` × `eu` = 1）；`/sports`、`/events` 免费。用量记录在 `data/cache/odds_api_usage.json`（不提交），剩余 < 20 时拒绝调用。
- 覆盖：J1、K1、澳超、中超、沙特（`soccer_saudi_arabia_pro_league`，注意 `soccer_spl` 是苏超）、欧冠、欧联、五大联赛、英冠；**不含亚冠和卡塔尔联赛**。
- 盘口：spreads 即亚盘，含 ±0.25/±0.75 四分之一盘；水位 = 十进制赔率 − 1（港盘）。

## 密钥文件

所有 Key 均不进仓库。读取顺序：环境变量 → `/home/box/.config/ah-secrets.env`（`KEY=VALUE`，chmod 600；可用 `AH_SECRETS_FILE` 改路径），由 `src/ah/secrets.py` 负责。支持 `API_FOOTBALL_KEY`、`ODDS_API_KEY`、`ODDSPAPI_KEY`、`FOOTBALL_DATA_KEY`、`FIVEDOLLAR_KEY`。CI 中请用 GitHub Secrets 注入同名环境变量。

### 多家公司共识写入日报

`PYTHONPATH=src python -m ah consensus YYYY-MM-DD` 会按推荐所属联赛（每联赛当天最多 1 次 odds 调用，遵守 reserve）拉取 The Odds API 亚盘，按队名（规范化 + `config/team_aliases.yaml` + 模糊匹配阈值 0.75）和开球时间（±15 分钟）匹配，把共识写入 `picks[].market_consensus`（中位盘口、公司数、一致家数、Pinnacle 盘口，均为推荐方视角），再 `render` 即在报告中显示“多家公司共识”。daily.yml 已在 render 前自动执行（需 Secret `ODDS_API_KEY`，缺失则跳过）。

## football-data.org（欧洲联赛基本面 + 第二比分源）

- 接口 `https://api.football-data.org/v4`，请求头 `X-Auth-Token`，Key 为 `FOOTBALL_DATA_KEY`（同上密钥文件）。客户端 `src/ah/football_data.py`。
- 免费档 10 次/分钟：按响应头 `X-Requests-Available-Minute` / `X-RequestCounter-Reset` 自动等待，遇 429 退避重试；结果缓存在 `data/cache/football_data/`。
- 覆盖：英超 PL、英冠 ELC、欧冠 CL、法甲 FL1、德甲 BL1、意甲 SA、西甲 PD（不含欧联和所有亚洲联赛）。
- 用途：`enrich(..., fd_code="CL")` 补当季积分榜、近 5 场状态、交锋（API-Football 免费档拿不到当季数据）；`settle` 回填比分时对欧洲比赛用它交叉核对，两源比分不一致则不结算、日志标记 mismatch 待人工确认；API-Football 匹配不到时用它回填。

## 5DollarFootballAPI（五大联赛 Bet365 盘口变化）

- 接口 `https://api.5dollarfootballapi.com/v1`，请求头 `Authorization: Bearer <FIVEDOLLAR_KEY>`（同上密钥文件），客户端 `src/ah/fivedollar.py`。
- 免费档：60 次/小时（另有 20 次/分钟上限），只含英超/西甲/意甲/德甲/法甲，Bet365 全部盘口（初盘/即时/滚球），近 3 个月历史。按 `X-RateLimit-*` 记录用量（`data/cache/fivedollar_usage.json`），剩余 ≤ 5 时停止，429 按 `Retry-After` 退避；结果缓存在 `data/cache/fivedollar/`。
- `python -m ah consensus DATE` 会为五大联赛的推荐写入 `picks[].bet365_movement`（推荐方视角的初盘→即时盘和水位、是否朝推荐方升盘），报告显示“Bet365 盘口变化”。每个推荐约 2 次请求。
- 免费档对外展示需署名 “Football data by 5DollarFootballAPI”，报告中已自动带上。
- 以后可升级到 Community Access（300 次/小时，可加一个亚洲国家联赛包，如日职/韩K）。

## OddsPapi（亚洲公司亚盘共识，补亚冠/卡塔尔）

- 接口 `https://api.oddspapi.io/v4`，query 参数 `apiKey`，Key 只从环境变量 `ODDSPAPI_KEY` 读取（不写入任何文件）。客户端 `src/ah/oddspapi.py`。
- 免费档 250 次/月（每月 1 日重置），每次计费调用算 1 次（报错也算）；`/historical-odds` 免费，`/account` 不计数。本地计数在 `data/cache/oddspapi_usage.json`，剩余 ≤ 30 时停止；`Client().sync_usage()` 用 `/account` 校准。
- 覆盖（tournamentId）：亚冠精英 463、亚冠二 668、卡塔尔联 825、沙特联 955、日职 196、日职乙 402、韩K 410、中超 649、澳超 136、欧冠 7、英冠 18。
- 亚盘：每家公司取主盘（两边赔率最接近的线），与其他来源同结构。默认只请求亚洲公司（Pinnacle、SBOBet、Singbet、Dafabet、3et、Saba、PS3838、198bet）。
- `python -m ah consensus DATE`：The Odds API 不覆盖或没匹配上的推荐（如亚冠、卡塔尔）改用 OddsPapi，报告中“多家公司共识”标注来源 OddsPapi。每联赛每天 1 次 `/fixtures` + 每个推荐 1 次 `/odds`。

## 准确率优先策略（先知）

命中率优先于推送数量：宁可少推，不推不确定的场次。配置见 `config/settings.yaml` 的 `policy`，实现见 `src/ah/policy.py`。

- 只发布信心「高」的推荐，每日最多 3 场，且必须有真实赔率。
- 推荐盘口与多家公司中位数相差超过 0.25 时降一档，超过 0.5 直接撤下。
- 中/低信心的场次只写入报告 JSON 的内部字段 `watchlist`，不推送 Telegram，也不在网站上作为推荐展示。
- 赛前最终确认时信心下降直接撤销（`policy.cancel_on_drop`，或 `final_check.decision: cancel`）。
- 策略自 2026-11-05 起生效；2026-11-04 的报告保留为历史记录，不改写（已撤销的场次不再展示）。
- 战绩统计（`python -m ah stats` → `data/stats.json`，网站「统计」页）：总体、分联赛、分信心等级（高/中/低）、盈亏单位。命中率 = (赢 + 0.5×赢半) / (场次 − 走 − 取消)。Telegram 消息附带一行战绩摘要（`stats.summary_line`）。
- 所有 Telegram 消息、报告与网站页脚都附带：「基于数据的分析参考，亚盘有风险，不构成投注建议。」

## Web 看板（GitHub Pages）

`python -m ah site` 生成 `site/`：今日 / 推荐记录 / 战绩 / 数据源 / 规则（另有历史日报）。所有时间为北京时间。
排序、筛选、图表与 VIP 门禁由 `templates/site/app.js` 在浏览器完成（无外部依赖）。
VIP 密码只以 SHA-256 存在 `config/settings.yaml: vip_password_sha256`；**仅前端隐藏，数据仍公开在仓库与站点中。**

### 赛前确认 / 赛后回测记录（写入 reports/YYYY/MM/YYYY-MM-DD.json 每个 pick）

```bash
PYTHONPATH=src python -m ah prematch-record --date 2026-11-04 --match-id titan007-3073152 \
  --decision 保留|调整|撤销 --reason "…" [--new-line -0.5 --new-odds 0.92] [--confidence 高|中|低] [--source URL]
PYTHONPATH=src python -m ah backtest-record --date 2026-11-04 --match-id titan007-3073152 \
  --score 2-1 --review "复盘…" [--source URL]          # 已撤销的推荐可省略 --score
PYTHONPATH=src python -m ah settle                      # 自动回填比分后同步到报告 JSON（不覆盖复盘）
PYTHONPATH=src python -m ah sources                     # 从本地用量文件刷新 data/source_status.json
```

两个命令都会同步 `data/predictions.csv` 并重算 `data/stats.json`。复核信心低于原信心时自动改为「撤销」（作废，不计命中率）。
