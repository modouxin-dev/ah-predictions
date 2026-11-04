# data/paper_trades.csv 字段说明

生成：`cd src && python -m ah.audit.paper_trades [--fetch-scores]`（代码 `src/ah/audit/paper_trades.py`）。
来源：`reports/YYYY/MM/YYYY-MM-DD.json` 的 picks + watchlist + watched（自 2026-11-04 起）。所有筛选场次都记模拟单（路线图「回测机制」），每注 1 单位。
主键：`date + match_id`，可重复运行；已回填的比分、收盘、结算等字段在新值为空时保留。

## 通用约定

- **编码与格式**：UTF-8 CSV，首行为表头；数字用最短十进制（如 `-0.75`、`1`）。
- **空值**：一律写空字符串。原因写在 `close_note` / `score_note` / `note` 里。
  - 未结算：`result`、`ah_outcome`、`units`/`pnl`、`settled_at` 为空，`score_note=赛果未出/待回填`。
  - 无收盘：`close_*`、`odds_close`、`clv`、`line_move` 为空，`close_note` 说明原因（如 `data/odds_snapshots/ 不存在（待数据官提供）`、`odds_snapshots 中无该场开球前快照`、`line_moved_needs_model`）。
  - 无模型：`model_p`、`brier` 为空，**网站/推送展示为「暂无」**。watch 场次没有模型方向时，`pick_side`/`pick`/`line`/`odds`/`stake` 为空，`note` 以 `no_model_direction` 开头。
- **时间**：`*_bj`、`updated_at`、`settled_at`、`close_captured_at` 为北京时间 ISO 8601，带 `+08:00`，精确到秒（例 `2026-11-05T04:00:00+08:00`）。`kickoff_utc` 保留原始 UTC（`Z`），用于和数据源匹配。`date` 为报告日期 `YYYY-MM-DD`（北京时间）。
- **视角**：`line`、`odds`、`close_line`、`close_odds`、`odds_taken`、`odds_close` 均为**所选一方**视角，负数=所选一方让球。`home_line`、`line_open_home`、`close_home_line` 为**主队**视角，作对照。
- **赔率**：按 `odds_format` 解释。`hk` 为港盘水位（净赔，十进制赔率 = 1 + 水位），`decimal` 为欧赔。目前报告全部是 `hk`。
- **status**：`active` 推荐有效；`cancelled` 赛前撤销推荐（记 void，0 单位）；`void` 比赛取消、延期等作废（0 单位）；`watch` 观察或未推荐（只做模拟单，不推送）。
- **比分**：主-客，90 分钟（含补时，不含加时和点球）。只有两个独立来源一致才填（目前是 API-Football fulltime + football-data.org），否则留空并在 `score_note` 写原因。

## CLV

```
p_fair = (1/o_pick) / (1/o_pick + 1/o_opp)   # 收盘两边十进制赔率，比例法去水
CLV    = o_bet × p_fair − 1                   # = 下注赔率 / 收盘无水公平赔率 − 1
```
`o_bet` 为下注时的十进制赔率。只在收盘盘口等于下注盘口（同一视角）时计算。盘口不同时，按路线图应由模型把收盘价换算到下注盘口；模型接入前 `clv` 留空，`close_note=line_moved_needs_model`，并记录 `line_move`。收盘取 `data/odds_snapshots/` 中 `captured_at` 早于开球的最后一条快照。

## Brier

`brier = (model_p − y)²`，y=1 为赢盘，y=0 为输盘。走盘、赢半、输半暂不计（待与模型研究员约定）。无 model_p 时为空，展示为「暂无」。

## 列

| 列 | 类型 | 含义 | 空值 |
|---|---|---|---|
| date | date | 报告日期（北京时间） | 不空 |
| match_id | str | 报告里的 match_id（titan007-/apif-）；报告未给时为 `watch-<主>-<客>` | 不空 |
| league | str | 赛事 | — |
| home / away | str | 主队 / 客队 | — |
| kickoff_utc | ISO UTC `Z` | 开球（原始 UTC） | 报告未给时为空 |
| kickoff_bj | ISO +08:00 | 开球（北京时间） | 同上 |
| status | enum | active / cancelled / void / watch | 不空 |
| confidence | enum | 报告里的原信心 high/medium/low（撤销后也保留） | 报告未给时为空 |
| pick_side | enum | home / away，模拟下注方向 | watch 无模型方向时为空 |
| pick_team | str | 所选球队简称 | 同上 |
| line | float | 下注盘口（所选一方视角） | 同上 |
| home_line | float | 下注或报告时的盘口（主队视角） | 报告未给盘口时为空 |
| line_open_home | float | 初盘（主队视角） | 同上 |
| odds | float | 下注水位（按 odds_format） | watch 为空 |
| odds_format | enum | hk / decimal | 不空 |
| close_line | float | 收盘盘口（所选一方视角） | 无收盘或无方向时为空 |
| close_home_line | float | 收盘盘口（主队视角） | 无收盘时为空 |
| close_odds / close_odds_opp | float | 收盘所选一方 / 对面水位（按 odds_format） | 无收盘时为空 |
| close_captured_at | ISO +08:00 | 收盘快照时间 | 无收盘时为空 |
| close_note | str | 收盘、CLV 为空的原因 | 正常时为空 |
| line_move | float | close_line − line（负数=收盘更深，对所选方有利） | 无收盘时为空 |
| implied_p_bet | float | 1/下注十进制赔率（含水，仅作参考） | 无赔率时为空 |
| clv | float | 见上文 CLV 公式 | 无收盘或盘口变动时为空 |
| result | str `主-客` | 90 分钟比分（两源一致） | 未出或不一致时为空 |
| score_sources | str | 比分来源 | 同上 |
| score_note | str | 比分为空的原因 | — |
| ah_outcome | enum | win / half_win / push / half_loss / loss / void | 未结算时为空 |
| units | float | 单位盈亏（1 单位，src/ah/settle.py profit） | 未结算时为空 |
| model_p | float 0–1 | 模型给出的所选方赢盘概率 | 模型接入前为空（展示「暂无」） |
| brier | float | 见上文 Brier | 无 model_p 或非整赢/整输时为空（展示「暂无」） |
| note | str | 撤销原因、观察原因等 | — |
| updated_at | ISO +08:00 | 本行最后生成时间 | 不空 |
| pick | str | **别名**：= pick_team | 同 pick_team |
| odds_taken | float | **别名**：= odds | 同 odds |
| odds_close | float | **别名**：= close_odds | 同 close_odds |
| stake | int | 1=模拟下注；0=撤销/作废；空=无方向（watch） | — |
| pnl | float | **别名**：= units | 同 units |
| settled_at | ISO +08:00 | 结算时间（撤销取报告的撤销时间；之后保留不变） | 未结算时为空 |

## 样例（来自当前 csv）

```csv
date,match_id,league,home,away,kickoff_utc,kickoff_bj,status,confidence,pick_side,pick_team,line,home_line,line_open_home,odds,odds_format,close_line,close_home_line,close_odds,close_odds_opp,close_captured_at,close_note,line_move,implied_p_bet,clv,result,score_sources,score_note,ah_outcome,units,model_p,brier,note,updated_at,pick,odds_taken,odds_close,stake,pnl,settled_at
2026-11-04,titan007-3068888,AFC Champions League Elite（亚冠精英·东区 第4轮）,武里南联 Buriram United,鹿岛鹿角 Kashima Antlers,2026-11-04T12:15:00Z,2026-11-04T20:15:00+08:00,cancelled,medium,away,鹿岛鹿角,-0.75,0.75,0.5,0.78,hk,,,,,,data/odds_snapshots/ 不存在（待数据官提供）,,0.5618,,,,赛果未出/待回填,void,0,,,撤销：临场盘口由报告时的鹿岛 -0.75(0.78) 骤降两档至 -0.25(0.92)，低于初盘 -0.5，大小球由3降至2.75；原推荐的核心依据（资金流向鹿岛）已反转，且无法确认首发，按准确率优先原则取消。,2026-11-04T20:57:43+08:00,鹿岛鹿角,0.78,,0,0,2026-11-04T19:33:00+08:00
2026-11-04,titan007-3073152,UEFA Champions League（欧冠 联赛阶段第4轮）,波尔图 Porto,那不勒斯 Napoli,2026-11-04T20:00:00Z,2026-11-05T04:00:00+08:00,active,medium,home,波尔图,-0.75,-0.75,-0.25,1.06,hk,,,,,,data/odds_snapshots/ 不存在（待数据官提供）,,0.4854,,,,赛果未出/待回填,,,,,,2026-11-04T20:57:43+08:00,波尔图,1.06,,1,,
2026-11-05,apif-1635606,UEFA Champions League（欧冠 联赛阶段第4轮）,AEK雅典 AEK Athens,皇家马德里 Real Madrid,2026-11-04T17:45:00Z,2026-11-05T01:45:00+08:00,watch,low,,,,2,1.5,,hk,,,,,,data/odds_snapshots/ 不存在（待数据官提供）,,,,,,赛果未出/待回填,,,,,no_model_direction；深盘：皇马由-1.5升至-2（共识中位-2，6/8 家一致）；皇马欧冠3战全胜，但-2 深盘需净胜3球才全赢，盘口已透支,2026-11-04T20:57:43+08:00,,,,,,
```
