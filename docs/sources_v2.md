# 数据源 v2：现状、扩源清单、交叉核验规则草案（data/sources-v2，2026-11-05）

时间如无特别说明均为北京时间（UTC+8）；CSV 内时间为 UTC。

## 1. 现状

### 1.1 来源与公司（来自代码与 2026-11-04/05 快照实际出现的 bookmaker）

| source | 端点 | 公司（实测出现） | 盘口形态 | 计费 |
|---|---|---|---|---|
| oddspapi | `/v4/historical-odds`（免费）+ `/fixtures`（计费，map.json 缓存） | pinnacle, sbobet, singbet, dafabet, sabasports, 3et | 全梯度 + 时间序列，可取 open/report/close 任一时点 | 250/月，historical 免费 |
| api_football | `/odds?fixture` | pinnacle, sbobet, bet365, 1xbet, betano, betvictor, marathonbet | 全梯度，只有当前盘 | 100/天，每场 1 次 |
| odds_api | `/v4/sports/{key}/odds?markets=spreads&regions=eu` | pinnacle, 1xbet, marathonbet, matchbook, coolbet, betonline, betanything, gtbets | 每家 1 条主盘（SINGLE_LINE_SOURCES），只有当前盘；不覆盖 ACLE/ACL2/卡塔尔 | 500/月，1/联赛 |
| infersports（本分支新增，opt-in） | `/v1/events/{id}/odds`、`/v1/events/{id}/opening` | crown, sbobet, macau, m8bet, nova88, hkjc（免 key 档无 Pinnacle） | 全梯度当前盘 + 每家一条初盘（opened_at） | 免 key 200/天/IP |

未接进快照、但项目里有客户端：fivedollar（五大联赛 Bet365 初/即，免费档）、football_data（赛果，无赔率）。titan007 在 `working` 里，按要求本分支不动。

### 1.2 刷新方式与间隔

| 阶段 | 触发 | oddspapi | api_football | odds_api | infersports |
|---|---|---|---|---|---|
| open | 日报后手动 `--stage open` | historical 序列里最早的报价 | 无（只有当前盘） | 无 | `/opening`（opt-in） |
| report | 日报后手动 `--stage report`（as_of≈now 才调用当前盘来源） | as_of 时点 | live_ok 时 1 次/场（auto：Odds API 无覆盖才用） | live_ok 时 1 次/联赛（缓存 600s） | live_ok 时 1 次/场（缓存 90s） |
| close | `/workspace/ah-data-close-loop.sh` 每 5 分钟（开球前 10 分钟内 + 赛后 `--backfill`），看门狗兜底 | 开球前最后报价（backfill 只用免费 historical） | **循环不调用** | **循环不调用** | opt-in 时 live_ok 调用，强制不读缓存 |
| live（`*.live.csv`） | 临时手动（2026-11-05 亚冠二） | ✓ | ✓ | — | — |

客户端节流：oddspapi historical 5.5s/次；api_football ~10 次/分钟；the odds api 无硬间隔（有缓存）；infersports ≥10s/次（跨进程，usage 文件记录 last_request_at）。

### 1.3 同一主盘线上的独立来源/公司数（以 Pinnacle 主线为锚，2026-11-05.csv / live.csv / 2026-11-04.csv）

| 赛事 | 阶段 | 场数 | 独立来源 | 同线公司数 | 两源都看到的公司 |
|---|---|---|---|---|---|
| ACL2 | open | 11 | 1（oddspapi） | 1–5 | 0 |
| ACL2 | report | 11 | 2（oddspapi + api_football） | 5–9，中位 7 | 中位 1（sbobet 或 pinnacle） |
| ACL2 | close | 6 | 5/6 场只有 oddspapi | — | 0 |
| ACL2 | live.csv | 3 | 2 | 7–9 | 2 |
| UCL | open / close | 8 | 1（oddspapi） | 5–6 | 0 |
| UCL | report | 8 | 2（oddspapi + odds_api） | 6–12 | pinnacle |
| ACLE（11-04） | close | 2 | 1 | 4 | 0 |
| J1 / 五大联赛 | — | 0 | 这两天快照里没有比赛（暂无） | — | — |

### 1.4 缺第二来源的赛事/阶段

- **所有赛事的 open 阶段**：只有 oddspapi；api_football 和 odds_api 拿不到历史盘。InferSports `/opening` 能补上 ACL2/J1/K1/A 联赛/沙特/卡塔尔/UCL/UEL/五大联赛。
- **所有赛事的 close 阶段**：收盘循环只调 oddspapi（为了省配额）。
- **ACLE**：odds_api 不覆盖，InferSports 也没有；只剩 api_football（100/天）。
- **ACL2、卡塔尔**：odds_api 不覆盖，第二来源只有 api_football。
- **J1/K1/中超/A 联赛/沙特**：odds_api 覆盖，但每家只有 1 条主盘。

## 2. 扩源清单（按可行性排序）

| # | 来源 | 覆盖（亚洲重点） | 额度/价格 | 亚盘/初盘 | 接入成本 | 条款风险 | 注册/付费 | 证据 |
|---|---|---|---|---|---|---|---|---|
| 1 | **InferSports 免 key 档**（本分支已接） | ACL2、J1、J2、K1、A 联赛、沙特、卡塔尔、UCL、UEL、五大联赛、英冠；**ACLE、中超暂无** | 200 次/天/IP，60/分钟，约 90s 新鲜度 | 全梯度亚盘（全场/半场）+ `/opening` 初盘；crown/sbobet/macau/m8bet/nova88/hkjc；免 key 无 Pinnacle | 已完成 | medium：公测期；AUP 禁止用多 key 规避限流、批量抽取、整体转发原始数据 | 不需要 | https://infersports.dev/pricing ，https://api.infersports.dev/llms-full.txt （"Keyless … 200 requests/day per IP, with the sharp book (Pinnacle) excluded"），https://infersports.dev/aup ，https://infersports.dev/terms |
| 2 | InferSports 免费 key | 同上，并加 Pinnacle（免费档是否开放 Pinnacle：暂无，llms-full 只说免 key 档排除 Pinnacle） | 5000 次/天/账号，无需绑卡 | 同上 | 设 `INFERSPORTS_KEY` 即可，代码已支持 | medium | **需要注册**（用户决定） | https://infersports.dev/signup ，llms-full.txt 第 205–211 行 |
| 3 | oddspapi 免费 historical 多开几家 | ACLE/ACL2/卡塔尔/沙特/J1/K1 等（tournaments 已配） | historical 免费，限速约 5s；fixtures 计费 | 有初盘和时间序列；可加 ps3838/198bet | 改 bookmakers 列表，每次 3 家 | low | 已有 key | config/sources.yaml `oddspapi`；https://oddspapi.io/en/docs |
| 4 | API-Football `--apif all` / 收盘循环带上 api_football | 全部赛事，包括 ACLE | 100/天，每场 1 次 | 全梯度，只有当前盘 | 改参数即可；每天多花 10–20 次 | low | 已有 key | config/sources.yaml `api_football` |
| 5 | 5DollarFootballAPI 接进快照 | 免费档只有五大联赛（Bet365）；ACLE 要 Pro（$5），ACL2 要 Ultra | 60 次/小时 | Bet365 opening/closing/inplay | 客户端已有，约半天 | low（展示需署名） | 已有免费 key；Pro/Ultra 要付费 | https://5dollarfootballapi.com/docs ，https://5dollarfootballapi.com/afc-champions-league-api |
| 6 | football-data.co.uk CSV | 欧洲联赛（含 J1？暂无核实），**没有亚冠** | 免费 | 只有历史收盘亚盘（Pinnacle/均值），赛后才有 | 低 | low | 不需要 | https://www.football-data.co.uk/notes.txt |
| 7 | iSports API 试用 | 号称 2000+ 联赛；18 家含 Crown/Sbobet/Macau/HKJC/Bet365，部分有 close 字段 | 15 天试用，200 次/天/项目，延迟约 2 分钟；Odds Pro $599/月 | 有初盘和收盘（部分公司），HK 水位 | 中 | medium | **需要注册**；之后要付费 | https://www.isportsapi.com/en/faq/article/is-there-a-free-trial-available-72.html ，https://www.isportsapi.com/en/products/detail/football-api-product-54.html |
| 8 | Sportmonks | 免费档只有丹超/苏超；亚洲联赛要付费套餐 | 赔率附加包 €14–69/月，Premium(TXOdds) €129+/月 | Asian Handicap market 6 | 中 | low | 注册 + 付费 | https://www.sportmonks.com/ ，https://docs.sportmonks.com/v3/endpoints-and-entities/endpoints/standard-odds-feed |
| 9 | odds-api.io | 暂无核实 | 新免费 key 已暂停；$65+/月 | 暂无 | 中 | low | 付费 | https://odds-api.io/pricing/free ，https://odds-api.io/pricing |
| 10 | The Odds API 加 region | 不覆盖 ACL | 加 uk/au 花费翻倍 | 每家 1 条线 | 低 | low | 已有 | config/sources.yaml `the_odds_api` |
| — | Betfair Exchange API、Goalserve | 暂无（未核实） | 暂无 | 暂无 | — | — | 需要注册/付费 | 暂无 |
| ✗ | OddsPortal、FotMob /api、Sofascore、Transfermarkt、K 联赛官网 | — | — | — | — | prohibited | — | /workspace/data-phase2-research/sources_feasibility.md |
| ? | 球探 titan007、澳客 okooo | — | — | — | — | high | — | 降级与否由用户决定，本分支没动 |

被封来源的替代：oddsportal/球探的“多家亚洲公司初盘+即时盘”，现在可以用 InferSports（crown/macau/hkjc/sbobet，含初盘）加 oddspapi historical 拼出来。

## 3. 交叉核验硬规则草案

> 2026-11-05 口径已更新为只打标不拦截，详见 docs/cross_verification.md。门槛未放宽。
（config/sources.yaml `cross_verification_draft`，`enabled: false`，未改出单逻辑）

1. **口径**
   - 比较对象：同一场、同一阶段、`is_main=1` 的行。主线仍由 `main_line()` / `pick_main` 决定（完整梯度去水后主队概率最接近 0.5，Pinnacle 优先）；没有 Pinnacle 时按现有逻辑选。
   - **独立来源**按 `source` 计。**公司**按 `bookmaker` 计：同一家公司在两个来源出现（如 sbobet 来自 oddspapi 和 infersports、pinnacle 来自 oddspapi 和 api_football）只算 1 家公司，但该公司 `verification=verified` 记 1 次"来源一致"。
   - 疑似同一公司的不同名字（crown≈singbet、nova88/m8bet≈sabasports，11-05 实测价格几乎一样）**先不合并**。若要更严，可在规则里把它们视为同一家：`maybe_same_book_as`。
   - 注意：两个来源的 sbobet 在 11-05 report 阶段 6/6 场同线同价，时间戳相差几秒，说明很可能来自同一上游抓取。verified 证明“抄得对”，不证明“市场共识”，所以还需要第 3 条的公司数条件。
2. **最低门槛**（同一主盘线 `ah_home_line` 完全相等）

   | 阶段 | 独立来源 ≥ | 同线公司 ≥ | verified 公司 ≥ | 另外 |
   |---|---|---|---|---|
   | open | 1 | 2 | — | 只记录，不作出单依据 |
   | report | 2 | 3 | 1 | 主线上有 Pinnacle，或 sbobet/singbet(crown)/sabasports 中至少 2 家同线 |
   | close | 2 | 3 | 1 | 同上 |

   不满足：report 置信度降一级（中 → 低，即不推送）；close 标 `unverified`，不改主线。
3. **时效**（`captured_at − price_ts`）

   | 阶段 | 降级 | 撤回 |
   |---|---|---|
   | open | 不按时效 | — |
   | report | > 30 分钟 | > 2 小时 |
   | close | > 10 分钟 | > 30 分钟 |

   单家公司超时，就不算进第 2 条的计数。
4. **深盘拦截**
   - 主盘行 `abs(ah_home_line) >= 1.75` 时，`status_detail` 追加 `deep_line`，summary 的 `matches[].deep_line=true`。
   - report 或 close 任一阶段触发就拦截出单。
   - 例：11-05 BG巴吞联 vs 大埔 report 主线 −2.0（oddspapi 与 infersports 一致）→ 拦截；町田 −1.5 不拦截，但 hkjc 已开 −1.75，需要留意。

## 4. 本分支实现

- `src/ah/infersports.py`
  - 客户端：免 key 或 `INFERSPORTS_KEY` 均可；按 UTC 日计数，保留 20 次；同站间隔 ≥10s（跨进程）；带缓存；分钟级 429 按 Retry-After 退避，日级 429 当天停止调用；写入 source_status。
  - 纯函数：`ah_ladders`、`ah_opening`、`event_index`。
- `src/ah/odds_snapshots.py`
  - 新增 `source=infersports`（opt-in：`--sources oddspapi,odds_api,api_football,infersports`），默认 `SOURCES` 不变，收盘循环行为不变。
  - open 用 `/opening`（只收开球前的 opened_at），report/close 用 `/odds` 全梯度（close 不读缓存）。
  - event id 写入 `map.json[mid].infersports`；`quota_snapshot` 增加 infersports。
- `config/team_aliases.yaml`：BG Pathum United / Tai Po / Phnom Penh Crown / Machida Zelvia 的 InferSports 写法。
- 测试：`tests/test_infersports.py`（9 个，全 mock）。

### 4.1 实测（2026-11-05 16:50 前后，亚冠二剩余 6 场，写到 /workspace/sources-v2-trial/，没动正式快照）

- 6/6 场匹配到 InferSports 事件（Pathum/Tai Po 加别名后）。每场新增 5–6 家公司（crown/macau/m8bet/nova88/hkjc/sbobet）。
- report：oddspapi 与 infersports 的 sbobet **6/6 同线同价 → verified**，match_verification 从 single 变成 verified。
- open：第一次有第二来源。sbobet 3/6 同线同价 verified，3/6 conflict：InferSports 的 opened_at 是该公司最早报价，oddspapi 取的是自己最早抓到的那条，两边“初盘”的取值时点不同。
- 配额：InferSports 13 次（events 1 + 每场 odds/opening 2）；OddsPapi 计费 0（historical 免费，fixture map 复用）；API-Football 0；The Odds API 0。
