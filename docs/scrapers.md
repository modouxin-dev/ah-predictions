# 首发 / 比分核对入口（数据官，phase 1）

两个稳定入口，供先知按时间点调用。环境：仓库根目录，`PYTHONPATH=src`，venv 同 ah-predictions。
API-Football key 只从环境变量或 `/home/box/.config/ah-secrets.env` 读取（`ah.secrets.get_secret`），不打印、不落盘。
Client 永远保留最后 10 次额度（`RESERVE=10`），低于即拒绝请求（退出码 3）。

## 1. 首发：`python -m ah.scrapers.lineups`

```
python -m ah.scrapers.lineups --match-id <fixture id> [--match-id ...]
                              [--jleague-id comp/year/id] [--no-api] [--official-ttl 秒]
```

| 参数 | 说明 |
|---|---|
| `--match-id` | **必填**，API-Football fixture id，可重复（一次跑多场） |
| `--jleague-id` | 可选，J.League 比赛路径（如 `acle/2026/110402`），覆盖配置映射；只能配单个 `--match-id` |
| `--no-api` | 只读本地缓存，不消耗 API-Football 额度（缓存没有首发 → 暂无） |
| `--official-ttl` | 官网页缓存秒数，默认 0（每次重新请求官网） |

调用时机：**开赛前 60 分钟、40 分钟各一次**。

- 官网来源：只有 jleague.jp（日本球队的 J 联赛 / ACLE / ACL2）。亚冠场次的 J.League 路径查
  `config/sources.yaml` → `scrapers.jleague.acl_match_ids`。
- 输出：`data/snapshots/lineups/<开赛北京日期>/lineups-<fixture id>.json`，格式见 `schema/lineup_crosscheck.schema.json`。
  有官网记录时另写 `jleague-<comp>-<year>-<id>.json`（官网原始抽取）。
- stdout：每场一行摘要，最后一行写本次 API-Football 消耗与官网真实请求数。

### 状态（verdict）

| 状态 | 含义 |
|---|---|
| 已验证 | 官网与 API-Football 首发 11 人号码一致，同号姓名可对应 |
| 冲突 | 号码不同或同号姓名明显不同；写明两边差异，**不进入推荐** |
| 暂无 | 任一来源缺失或未公布（API-Football 也没有 11 人） |
| unverified | **只有 API-Football 单一来源**：A-Leagues（官网 JS/Opta 渲染）、沙特联（条款禁止爬虫，已停用）、没有 J.League 映射的亚冠比赛，或官网还没公布而 API 已有 11 人。可用，但没有第二来源验证 |

整场判定优先级：冲突 > 暂无 > unverified > 已验证。

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 完成（已验证 / unverified / 暂无） |
| 1 | 至少一场「冲突」 |
| 2 | 参数错误，或本地缓存和 API 都找不到该 fixture |
| 3 | 来源故障：官网结构变化 / 拦截 / 不可用，API-Football 报错或额度不足（能写的结果仍会写出） |

多场时取最大值。

### 额度

每场最多 **2 次**：

- fixture 详情：本地赛程缓存命中时 0 次。
- `/fixtures/lineups`：1 次。非空结果永久缓存，之后再跑 0 次；未公布时返回空、不缓存，下次再调 1 次。

所以 T-60、T-40 两次调用通常是 1～2 次。官网请求：jleague 每场 1 页（+ robots.txt，24 小时缓存），同站间隔 ≥10 秒。

## 2. 比分：`python -m ah.scrapers.scores`

```
python -m ah.scrapers.scores (--match-id <fixture id> [--match-id ...] | --date YYYY-MM-DD [--date ...])
                             [--no-api] [--no-afc]
```

| 参数 | 说明 |
|---|---|
| `--match-id` | API-Football fixture id，可重复；与 `--date` 二选一 |
| `--date` | UTC 日期，可重复；核对当天全部亚冠（ACLE 17 / ACL Two 18）比赛 |
| `--no-api` | 只读本地缓存，不消耗额度 |
| `--no-afc` | 不请求 the-afc.com（只用 jleague） |

调用时机：**完场后约 2.5 小时一次，次日早上再一次**。

- 第二来源：the-afc.com 赛事落地页（ACLE / ACL2 各 1 页，只抽取对阵 + 比分）。有 J.League 映射的日本球队比赛另用 jleague.jp 比赛页。
- 未完场的比赛记「暂无」，不比较；官网没出现的比赛也记「暂无」，绝不推断。非亚冠比赛不在本核对范围。
- 输出：
  - `--match-id`：`data/snapshots/scores/<开赛北京日期>/score-<fixture id>.json`
  - `--date`：`data/snapshots/scores/<日期>/acl-score-crosscheck.json`
- 状态：已验证 / 冲突 / 暂无。

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 完成 |
| 1 | 有「冲突」 |
| 2 | 参数错误 / 找不到 fixture / `--no-api` 时没有该日缓存 |
| 3 | API-Football 报错或额度不足；AFC 页全部失败；jleague 抓取失败 |

### 额度

- `--match-id`：每场最多 1 次 `/fixtures?id=`。缓存已是完场时 0 次；完场结果永久缓存。
- `--date`：每个日期 1 次 `/fixtures?date=`（强制刷新）。
- `--no-api`：0 次。

## 3. the-afc.com 合规（tos_note: internal-crosscheck-only）

- **只做内部比分核对**：只提取对阵、国家代码、阶段标记、比分和来源链接。不保存页面正文、战报文字或标题原文，也不展示、不转载。
- **HTML 只在内存里解析，不落盘**（`scrapers.afc.cache_raw: false`）。落盘的只有抽取结果 `data/cache/scrapers/afc/claims-<acle|acl2>.json`；robots.txt 照常缓存。
- **请求上限**：每次运行最多 2 页（`afc.MAX_PAGES`），间隔 ≥15 秒。失败不重试（`max_retries: 0`）。另有 robots.txt，24 小时 1 次。
  - 注意：`python -m ah.scrapers probe afc` 会再取 1 页，用来确认首发结论，平时不要跑。
- **停用开关**（立即生效，不再发任何请求，比分核对里 AFC 相关场次记「暂无：AFC 来源已停用」）：
  - 环境变量 `AH_AFC_SCORES_DISABLED=1`；或
  - `config/sources.yaml` → `scrapers.afc.scores.enabled: false`（或 `scrapers.afc.enabled: false`）。

## 4. 维护 ACL → J.League 映射

`config/sources.yaml` → `scrapers.jleague.acl_match_ids` 的格式是 `API-Football fixture id: comp/year/id`。

新一轮亚冠有日本球队时，按下面的步骤补：

1. 打开 jleague.jp/match/ 日程页，找到 `/match/acle/…` 或 `/match/acl2/…` 链接。
2. 核对开赛时间和对阵后写入。
3. 不确定的不要填（没有映射 = 只有 API-Football → unverified）。
   - 如果官网自动匹配结果与手工映射不同，按手工映射处理，并在结果里加 `note`。
   - 队名对不上时补 `config/team_aliases.yaml`。
