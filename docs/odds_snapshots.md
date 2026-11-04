# 盘口快照 data/odds_snapshots/（数据官）

代码：`src/ah/odds_snapshots.py`；测试：`tests/test_odds_snapshots.py`（全部 mock，不耗额度）。
用途：每场筛选比赛的开盘 / 日报时 / 临场（收盘）各公司亚盘，供回测审计员算 CLV。**查不到就标 missing，不估算、不编造。**

## 文件

| 文件 | 内容 |
|---|---|
| `data/odds_snapshots/<报告日期>.csv` | 每行一条记录（某场 × 时点 × 来源 × 公司 × 盘口线），UTF-8，逗号分隔，带表头 |
| `data/odds_snapshots/<报告日期>.summary.json` | 每场每个时点的场级验证状态、各来源共识线、计数、每次运行的请求数和额度 |
| `data/odds_snapshots/<报告日期>.map.json` | match_id → OddsPapi fixtureId / The Odds API event id（映射一次后复用，省额度） |

筛选清单 = `reports/YYYY/MM/<报告日期>.json` 的 `picks + watched + watchlist`（按 match_id 去重）。没有 match_id 或 kickoff_utc 的条目写入 summary 的 `skipped`。

## CSV 字段

回测审计员约定的必备字段在前 8 列。

| 字段 | 说明 |
|---|---|
| `match_id` | 与报告/API-Football 一致，如 `apif-1635606` |
| `captured_at` | 本次抓取时间（UTC，ISO8601，`Z` 结尾） |
| `stage` | `open`（开盘）/ `report`（日报时）/ `close`（临场=开赛前最后一次） |
| `bookmaker` | 公司小写标识：`pinnacle`、`sbobet`、`singbet`、`dafabet`、`1xbet`… missing 行为空 |
| `ah_home_line` | **主队视角让球数，负数 = 主队让球**（-0.5 = 主让半球；+0.25 = 主受让平半）。与 API-Football `Home -0.5`、报告 watchlist 的 `ah_home_line` 同号；注意 titan007 原始字段和 odds_api.normalize_spreads 的 `ah_home_line` 是相反号（正=主让），本文件不用那个约定 |
| `odds_home` / `odds_away` | 该盘口线的主队 / 客队赔率 |
| `odds_format` | 固定 `decimal`（十进制，含本金）；港盘水位 = 十进制 − 1，另见 `home_hk` / `away_hk` |
| `source` | `oddspapi`（/historical-odds）、`odds_api`（The Odds API spreads）、`api_football`（/odds）；missing 行为空 |
| `verification` | 行级：同一公司同一时点在 ≥2 个来源的主盘线一致 = `verified`；主盘线不同但同一条线两边赔率差都 ≤0.06（同一市场、只是主盘选线不同）也 = `verified`，`status_detail` 标 `ladder_price_match`；否则 = `conflict`，只有一个来源 = `single`，无数据 = `missing` |
| `match_verification` | 场级：每个来源取各公司主盘线的众数作为该源共识；≥2 个来源共识一致 = `verified`；共识不同但所有共同公司行级都 `verified` 也 = `verified`；否则 = `conflict`；1 个来源 = `single`；无数据 = `missing` |
| `is_main` | `1` = 该公司主盘（两边赔率最接近的线）；`0` = 主盘 ±0.5 以内的相邻线（方便在推荐盘口上直接算 CLV）。验证只看主盘 |
| `home_hk` / `away_hk` | 港盘水位 |
| `as_of` | 该记录代表的时刻（UTC）：open = 开盘时刻；report = 运行时刻或 `--as-of`；close = min(运行时刻, 开赛前 1 秒) |
| `price_ts` | 价格本身的时间戳（OddsPapi createdAt / The Odds API last_update / API-Football update）；无则空 |
| `report_date`, `fixture_id`, `kickoff_utc`, `league_id`, `league`, `home`, `away` | 比赛信息（fixture_id = API-Football id） |
| `status_detail` | missing 行写各来源原因，如 `oddspapi:not_covered; live_sources:no_history` |
| `source_ref` | 来源侧 id（OddsPapi fixtureId、The Odds API event id、API-Football fixture id） |

CLV 用法提示：取 `stage=close, source=oddspapi, bookmaker=pinnacle`，在推荐的同一 `ah_home_line` 上读 `odds_home/odds_away`（相邻线已存），去水后与下注赔率比较。

## 来源与额度

| 来源 | 端点 | 时点 | 花费 |
|---|---|---|---|
| OddsPapi | `/historical-odds`（每次最多 3 家，默认两组：pinnacle/sbobet/singbet、dafabet/sabasports/3et） | open、report、close（开赛后补跑也准，只取开赛前的价格） | 免费；映射用 `/fixtures` 每联赛 1 次（月 250，留 30）。429 指数退避 2/4/8 秒，尊重 Retry-After，最多 3 次 |
| The Odds API | `/sports/{sport}/odds?markets=spreads&regions=eu` | report、close（只能取当前盘，开赛后不取） | 每联赛每次 1 credit（月 500，留 20），缓存 10 分钟；不覆盖亚冠/卡塔尔 |
| API-Football | `/odds?fixture=` | report、close | 每场 1 次（日 100）；默认只给 The Odds API 不覆盖的联赛补第二来源（`--apif auto`），剩余 ≤ 30 时不再用 |

open 只有 OddsPapi 有历史，所以 open 最多是 `single`。

## 运行

```bash
# 日报生成后（北京时间约 08:30–10:00）：日报时 + 开盘
PYTHONPATH=src .venv/bin/python -m ah.odds_snapshots report open --date YYYY-MM-DD
# 临场：每 5 分钟跑一次，只处理 10 分钟内开赛、还没抓过临场的场次；没有到点的场次不发请求
PYTHONPATH=src .venv/bin/python -m ah.odds_snapshots close
# 开赛后补跑（免费，只用 OddsPapi 历史，把收盘更新为开赛前最后一次报价）
PYTHONPATH=src .venv/bin/python -m ah.odds_snapshots close --backfill
```

选项：`--match apif-xxx`（只跑指定场次）、`--as-of report`（日报时取报告 generated_at）、`--sources`、`--apif off|auto|all`、`--apif-floor`、`--lead`。
