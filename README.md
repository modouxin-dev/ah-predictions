# 先知 · 亚盘每日预测

先知从可用数据源采集足球赛程、亚盘和球队近期状态，按现有规则筛选比赛，通过 Telegram 发布日报，并记录已发布推荐的赛果与统计。数据不足或没有比赛通过筛选时，日报可以没有推荐。

**真实数据原则：不得编造赛程、盘口、水位、球队状态、伤停、比分、来源或推荐理由；未取得或未核验的信息须如实标注，不能将猜测写成事实。** 不得声称已经成功推送、回测或验证，除非有对应运行结果支持。

## 当前自动任务

所有时间为北京时间；GitHub 定时任务实际启动时间可能延迟。日报日期使用 UTC 日期，运行时以工作流中的 `date -u` 为准。

| 工作流 | 配置时间 | 当前行为 |
|---|---|---|
| `daily-report` | 每天 10:00 | `collect` job 采集并保存日报；独立的 `publish` job 依 `data/telegram_delivery.json` 送达台账决定是否推送。采集失败不会静默阻断发布判断。 |
| `settle-and-stats` | 每天 17:00 | 尝试获取已完赛比分，结算已记录的预测并更新统计。无法确认的结果保留待处理。 |
| `pages` | 报告或相关数据更新等触发 | 生成并部署 GitHub Pages 看板。 |
| `Check live collection` | 手动触发；其工作流文件推送到 `main` 时也触发 | 用当前数据源做采集预览，不提交日报，也不发送 Telegram；仍会消耗接口额度。 |
| `Publish live recheck` | 每天 14:00 定时 + 手动触发 | 实时采集并保存独立复核报告并推送；0 场推荐时只发「赛程事实清单」，不生成预测。 |

GitHub Actions 提供运行环境，以上任务不要求用户另行购买服务器。任务是否真正完成，以 [Actions 运行记录](https://github.com/modouxin-dev/ah-predictions/actions)和实际 Telegram 消息为准。

**当前未实现的自动化：** 日报流程尚未自动采集伤停或首发；赛前 50 分钟检查、赛后逐场复盘及其 Telegram 推送没有对应的自动工作流。已有记录和发布命令可供手动使用，见下文。历史赛果结算与统计也不等同于对预测模型完成独立回测。

## 自动日报与发布规则

`python -m ah.daily` 采集未来 36 小时的赛事，默认初筛最多 24 场；`--max-fixtures` 可设置 1 至 60 场。初筛为亚洲重点联赛预留部分名额，其余名额优先分配给已启用的数据源能提供球队状态的联赛，再按开赛时间展示选中的比赛。更大的筛查范围会增加请求次数和运行时间，客户端仍遵守各数据源的限额和保留额度。

- 赛程来自已启用且可用的数据源；盘口快照支持 OddsPapi、The Odds API、InferSports 和 API-Football。当前核验配置排除 API-Football；球探盘口只作为补充观察，不自动算作已核验盘口；5Dollar 数据用于补充已有推荐。
- 正式候选需要真实赔率、有效报价、同机构多源盘口核验、近期状态和同来源同机构的初盘。核验配置位于 `config/sources.yaml`；当前日报设置包括至少两个来源、三家机构和一家具备跨来源核验的机构。
- 报价超过 30 分钟但不超过两小时，且其他核验条件均满足时，信心降低一级；超过两小时则拦截。推荐仍须通过 `config/settings.yaml` 的发布策略：当前为**中信心及以上**，最多 10 场，深盘及共识偏离另有约束。
- 数据缺失、核验未通过或评分不足的场次进入观察名单；可以如实发布 0 场推荐的日报。现有规则评分没有经过独立验证，不能保证准确率或盈利。
- 当 football-data 近期状态不足时，已配置的 5DollarFootballAPI 可补充其支持联赛的近期赛果；只使用确认完赛且发生在采集之前的比赛，并标注真实来源。程序可根据该账号返回的联赛列表识别已配置的亚洲联赛 ID；实际能否使用取决于账号套餐和数据覆盖，亚洲球队状态仍可能缺失。
- 已取得符合核验条件的数据、且模型至少给出低信心评分的观察场次，可以在 Telegram 的「比赛观察分析」中展示模型倾向、盘口、球队近期赛果和报价时间。观察明确标为非正式推荐，不进入推荐账本或战绩；报价超过 30 分钟时会注明。

日报保存在 `reports/YYYY/MM/YYYY-MM-DD.json` 和对应 Markdown 中，推荐记录写入 `data/predictions.csv`。日报同一 UTC 日期的 JSON 已存在时不会覆盖。**是否推送不再以「报告文件是否存在」为准**，而以 `data/telegram_delivery.json` 中 `(date, report_sha, chat_id, message_ids)` 的送达记录为准：报告已存在但无送达记录仍会发送一次；报告内容变化（`report_sha` 变化）视为新版本。送达判定必须是 Telegram `sendMessage` 返回的非空 `message_id` 入账，Actions 步骤绿色不等于消息已送达。

实时复核另存于 `reports/rechecks/YYYY/MM/`，盘口快照按运行次数单独保存在 `data/snapshots/odds/rechecks/`，不会覆盖当天已有日报或其他复核的快照；符合条件且已发送的正式推荐记入账本，观察不记入账本。如果没有正式推荐且没有符合条件的观察，复核任务**不再中断**：仍发送一份只含本次已采集赛程事实与不推原因的报告，明确标注「非推荐、不含预测结论」，不生成任何盘口、比分或倾向。

## 数据源与密钥

在仓库 **Settings → Secrets and variables → Actions → Repository secrets** 中设置：

| Secret | 用途 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram 机器人令牌。 |
| `TELEGRAM_CHAT_ID` | 目标群、频道或个人会话 ID。机器人须具备相应发送权限。 |
| `ODDS_API_KEY` | The Odds API 数据源。 |
| `ODDSPAPI_KEY` | OddsPapi 数据源。 |
| `FOOTBALL_DATA_KEY` | football-data 赛程、球队状态及支持赛事的比分。 |
| `API_FOOTBALL_KEY` | API-Football 数据源；是否启用还受项目配置和停用开关控制。 |
| `FIVEDOLLAR_KEY` | 5Dollar 的补充盘口数据。 |

`SITE_URL` 可作为 Actions 变量设置。未配置数据源密钥时，相关来源可能跳过；日报任务在需要发送时如果缺少 Telegram 密钥会失败。GitHub 不会展示已保存的密钥明文，保存成功不代表密钥有效或数据覆盖充足。

**截至 2026-11-06 的项目状态：** API-Football 的 `/status` 返回账号 suspended，已关闭其自动请求，盘口跨来源核验也排除该来源。当天的一次实时采集预览中，OddsPapi 的亚冠精英和中超赛程请求返回 HTTP 404，原因尚未确认，不应将其当作密钥或账户状态的定论。

更换 Telegram 机器人时，需要在 GitHub 更新 `TELEGRAM_BOT_TOKEN`，将新机器人加入目标群并确认 `TELEGRAM_CHAT_ID`。创建机器人不会自动修改项目配置；更新令牌后是否能发送，须通过实际运行确认。

## 一次实测记录

2026-11-06，使用部署后的工作流和仓库配置运行了 [实时采集预览](https://github.com/modouxin-dev/ah-predictions/actions/runs/42200708695)：筛查 8 场，推荐 0 场；其中 6 场评分不足，2 场盘口核验未通过。该结果只反映当时的数据与规则，不能代表以后每天的结果。预览没有覆盖已有日报，也没有测试更新后的 Telegram 机器人发送。

## 手动操作与本地测试

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=src .venv/bin/python -m pytest -q

# 只预览采集结果；不会写入日报或发送 Telegram，但会请求数据源
PYTHONPATH=src .venv/bin/python -m ah.daily --preview --max-fixtures 8

# 预览已有日报的 Telegram 内容，不发送
PYTHONPATH=src .venv/bin/python -m ah telegram 2026-11-06 --dry-run

# 结算与统计；--no-autofill 仅结算已填写比分的记录
PYTHONPATH=src .venv/bin/python -m ah settle
PYTHONPATH=src .venv/bin/python -m ah stats
```

`daily-report` 可在 Actions 页面手动运行，重发当天已有日报；`Check live collection` 用于不发送消息的采集预览。赛前记录和赛后复盘可使用 `python -m ah prematch-record` 与 `python -m ah backtest-record`；具体参数通过各命令的 `--help` 查看。这些命令的存在不表示已经定时运行。

## 项目文件

```text
config/                 联赛、数据源和发布规则
src/ah/                 采集、评分、日报、发布与结算代码
tests/                  自动测试
reports/YYYY/MM/        每日 JSON 和 Markdown 报告
data/predictions.csv    已记录的预测及结算
data/stats.json         统计结果
data/snapshots/odds/    盘口快照
.github/workflows/      GitHub Actions 工作流
```

## 多人协作提交

**统一命令入口**（在仓库根目录）：

```bash
.venv/bin/python -m pytest -q                         # 测试（pytest.ini 已配置 pythonpath=src）
PYTHONPATH=src .venv/bin/python -m ah <cmd>           # 运行模块，如 site / stats / sources / tg-bot
```

**提交与推送一律用 `scripts/safe_commit.sh`**，不要手动 `git add -A` / `git commit -a` / `reset --hard` / `push --force`：

```bash
scripts/safe_commit.sh -m "说明" path1 path2 ...      # 只提交这些路径
scripts/safe_commit.sh --dry-run -m "说明" path1      # 只看会提交什么
scripts/safe_commit.sh --skip-tests -m "data" data/x  # 纯数据提交可跳过测试（代码改动请勿跳过）
scripts/safe_commit.sh --help                          # 全部选项与退出码
```

- **锁**：`flock` 锁住 `<git-common-dir>/ah-safe-commit.lock`（即主仓库 `.git/ah-safe-commit.lock`，所有 worktree 共用），默认最多等 300 秒（`--lock-timeout`），超时退出码 3。整个「提交 → rebase → 测试 → 推送」过程都在锁内。
- **只提交指定路径**：`git add -A -- 路径` + `git commit -- 路径`，别人已暂存/未提交的改动不会被带进来；作者为 `ah-predictions-bot <modouxin-dev@users.noreply.github.com>`。
- **rebase**：远端有新提交时 `git pull --rebase --autostash`（冲突则 abort、撤销本次提交，退出码 4）。
- **测试**：在检出待推送 HEAD 的**干净临时 worktree** 里跑（别人未提交的半成品不会影响结果）；失败则撤销本次提交（改动留在工作区）、不推送，退出码 5。
- **推送**：`GITHUB_AH_TOKEN`（环境变量或 `~/.config/ah-secrets.env`）通过 `GIT_CONFIG_*` 环境变量作为 `http.extraheader` 传给 git，不出现在命令行和日志里。被拒（并发推送）时自动重新 rebase、必要时重跑测试，指数退避重试最多 `--retries` 次（默认 4），仍失败退出码 6（本地提交保留）。
- 退出码：0 成功/无改动，2 用法或仓库状态错误（detached HEAD、rebase 进行中等），3 等锁超时，4 rebase 冲突，5 测试失败，6 推送失败，7 add/commit 失败。
- 建议每个智能体在自己的 worktree 里工作（`git worktree add ../ah-<名字> main`），避免在共享目录里切分支影响别人；锁和对象库是共用的。

**数据源状态文件**：各数据客户端每次请求都会更新状态，运行时写到 `var/source_status.json`（已 gitignore），不再弄脏 `data/`。
网站与 CI 读取入库快照 `data/source_status.json`；本机构建和 Telegram 机器人会自动选用更新的运行时文件。
需要更新线上「数据源」页时（建议日报任务每天一次）：

```bash
scripts/publish_source_status.sh            # var/ -> data/ 快照，内容有变化才经 safe_commit 提交推送
```

## MODEL CHANGE（2026-11-06）：让日报真正产出比赛推荐

**依据（【已验证】）**
- 远端 13:38 强制重采的日报：`picks=0`、24 场中 14 场「盘口跨来源核验未通过」硬拦、8 场「基本面与盘口评分不足」、2 场「近期状态数据不足」；仅 1 场走到评分（Leeds vs Tottenham，score 3.55）。
- `analyze.WEIGHTS` 有 8 个因子，但 `build_report` 实际只填 `form` / 可选 `h2h` / 自动 `line_movement`，已填权重上限 3.0、score 理论上限约 6.0，原 `high=7.0` 数学上不可达。
- `config/sources.yaml` 的 `cross_verification_draft.advisory_only: true` 与 `on_fail.report: 置信度降一级` 早已写明降级语义，但 `daily.py` 一直当硬拦截用。

**改动**
- `config/settings.yaml`：`medium 4.5→3.5`、`low 3.0→2.0`；顶层与 `policy` 两处发布门槛 `中/ medium → 低/ low`。
- `src/ah/daily.py`：新增 `_verification_gate()` 让 `advisory_only` 真正生效；核验未完全通过但**有真实 verified 主线报价**的场次改为「降级放行」，强制 `confidence=low`、写入 `verification_advisory`、在风险里显式披露，且推荐理由不再写「盘口已通过多源核验」。`build_report` 同步为这些场次采集基本面。
- `src/ah/grounding.py`：新增闸门——advisory 推荐若未标低信心、假称「已通过多源核验」、或未披露核验风险，一律判定失败并拒绝发送。
- `tests/`：更新 5 个因契约变更失效的断言（不再硬编码每日重采的场次数），新增 3 个 advisory 防幻觉测试；本地 105 项通过。

**硬底线未放宽**：推荐仍必须有真实采集的 `line` / `odds` / `price_ts` / 同机构初盘 / ≥3 场近期状态；没有 verified 主线报价时 `advisory_only` 也不放行；`advisory_only=false` 时维持原硬拦截。

**风险（【未验证】，不得写成「准确率提升」）**
- 阈值重标定**未经独立回测**；低信心推荐只代表规则放行，不代表更准。
- advisory 场次的盘口跨来源核验未完全通过，结论仅作分析参考，已在消息里标低信心并列为风险。
- 每次 `--refresh` 的真实近期状态不同，实际推荐场数以当日 `reports/**.json` 为准；规则评分未核实伤停与首发。

## ORCHESTRATION（2026-11-06）：10:00 准时推送 + 不再复发「0 场」

**根因（【已验证】）**
1. GitHub `schedule` 不保证准点：配置 `02:00 UTC`（10:00 北京）的定时运行，历史实际 `08:54 UTC` 才启动，延迟约 6 小时 54 分。单靠一条 cron 无法承诺 10:00。
2. `publish` job 原本重新 `checkout main` 读报告；若 `collect` 的 `safe_commit.sh` 推送失败或冲突，publish 拿到的是**旧报告**（可能是 0 场版本），这就是「链路通了但没有比赛」的隐藏路径。
3. 每次 `--refresh` 都生成新 `report_sha`，原幂等键按 sha 去重 → 一天内重复推送（11-06 已有 3 条送达记录）。
4. `screening_max_fixtures: 24` 把窗口内 60+ 场裁到 24，可能把有真实盘口的比赛挡在门外。

**改动**
- `.github/workflows/daily.yml`：cron 提前到 `01:00 UTC`（09:00 北京）先采集；`publish` job 等到 `02:00 UTC`（10:00 整点）再发送；`timeout-minutes` 调到 90 / 100。
- `collect` 产出的日报经 `actions/upload-artifact` 传给 `publish`，publish 优先用**本次采集产物**覆盖 checkout 报告；即使 git 提交失败也不会推旧报告。
- `src/ah/delivery.py`：幂等键改为 `(date, chat_id)`，一天只正式推送一次；手动 `workflow_dispatch` 允许强制重发；**已推送 0 场而本次重采出正式推荐时允许补发一条升级版**。台账新增 `picks_count` 便于审计。
- `config/settings.yaml`：默认初筛上限 24 → 32；collect 显式 `--max-fixtures 32`，若采集无错误但 0 场，再扩到 48 场重采一次。**只扩候选池，不改评分/核验/阈值**；仍 0 场则如实推赛程事实清单，绝不编造预测。
- schedule 补跑时若当天已送达带推荐日报，collect 直接跳过，避免重复采集与刷屏。

**未验证 / 风险（不得写成「保证准点」）**
- GitHub 仍可能把定时任务排到 10:00 之后；此时工作流会「已过点立即推送」，时间会晚。已另设 **每天凌晨 5:00 仓库体检任务** 与 **当天 09:55 一次性兜底核验任务**，由调度侧触发 `workflow_dispatch` 保障 10:00 推送。
- 兜底任务到点能否访问仓库取决于会话上下文与凭据有效期（当前 GitHub PAT 为 7 天有效期）；令牌到期后需续期，或改用自托管 runner / 外部 cron。
- 规则筛完仍可能 0 场；此时消息只含真实采集的赛程事实，不生成任何预测。

## 发布对账与真实数据原则（2026-11-06 编排层修复）

**本次修复不改动模型算法框架**：`analyze.py` / `policy.py` / `daily.py` / `odds_snapshots.py` / `config/settings.yaml` / `config/sources.yaml` 的评分、核验、阈值与策略逻辑保持原样。改动仅在编排层、展示层与对账层，冻结清单见 `AGENTS.md`。

- **防推测 / 防幻觉 / 防断言**：强制指令见 `AGENTS.md`，操作规范见 `docs/grounding-policy.md`，可执行闸门见 `src/ah/grounding.py`。所有展示字段必须能在报告 JSON 中逐字回溯；缺字段写「未取得」；禁止编造对阵、盘口、比分、倾向、送达结论。
- **0 场也有比赛**：`picks=[]` 时推送会增加「赛程事实清单」，列出本次筛过的真实对阵、开球时间与不推原因，明确标注「非推荐、不含预测结论」。不为了凑数生成任何预测。
- **送达证据**：`python -m ah delivery send` 只有在 Telegram 返回 HTTP 200 / `ok=true` / 非空 `message_id` 后才写 `status: sent` 到 `data/telegram_delivery.json`。缺密钥、目标会话与配置不一致、闸门未通过一律非零退出，不再 `exit 0` 静默跳过。
- **目标会话一致性**：`TELEGRAM_CHAT_ID`（Secret）与 `config/settings.yaml` 的 `telegram.chat_id` 同时存在且不一致时直接报错，不再静默任选其一。

```bash
# 事实闸门（失败则退出码 1，工作流拒绝发送）
PYTHONPATH=src python -m ah grounding --report reports/2026/11/2026-11-06.json

# 查看当天日报的纯文本，不发送
PYTHONPATH=src python -m ah telegram 2026-11-06 --dry-run

# 查看送达台账
PYTHONPATH=src python -m ah delivery show

# 运行时代理自检（只输出事实，不代表已送达）
PYTHONPATH=src python -m ah delivery preflight
```

### C 阶段：数据源修复（2026-11-06，单独提交 `DATA SOURCE:`）

用户已更换 `API_FOOTBALL_KEY` 并加入运行环境。本阶段**未改模型算法、核验口径或阈值**：

- `config/sources.yaml`：`api_football.enabled` 由 `false` 改回 `true`；保留 `AH_API_FOOTBALL_DISABLED` 与本地熔断 flag 作为紧急停用开关。旧 suspended 记录保留为历史，不代表当前账号状态。
- `python -m ah source-probe`：运行时代理自检，只输出可观测事实（key 是否存在、开关状态、`/status` 与 `/account` 返回字段、错误类型），**不泄露密钥、不推断「已恢复」、不把「能连通」写成「数据充足」**。另有 `Source runtime selfcheck` 工作流（每日 09:00 + 手动），只产出事实 artifact，不发送 Telegram、不写报告。
- `daily-report` 的 `collect` 改为每次运行显式 `--refresh` 重新采集：此前「报告已存在就跳过采集」会让任何数据源修复永远不生效。`--refresh` 只改 CLI 编排层，不碰 `build_report` / `assess` 算法。
- OddsPapi 赛程 HTTP 404 已按「未取得赛程（HTTP 404）」记录，不再当作致命采集错误；具体覆盖仍需运行时代理自检确认。

**C 阶段的【未验证】项（不得写成已完成）**：新 key 的实际有效性、订阅档位与数据覆盖；OddsPapi 赛程端点的真实返回；这些只能在 CI 运行时由 `source-probe` 与日报采集日志给出事实。仓库静态文件与本次对话都无法读取 Secrets 明文。

### 本次修复仍未验证的项（不得写成已完成）

| 项 | 状态 |
|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` 的明文值与有效性 | 【未验证】所持令牌读 `/actions/secrets` 返回 403 |
| 目标群是否真的收到消息 | 【未验证】需 Telegram 端收件记录或 `message_id` 入账 |
| 定时任务是否被 GitHub 自动禁用、`02:00Z` 配置的实际启动延迟 | 【未验证】需管理员在 Actions 页面确认 |
| `.github/workflows/**` 是否已同步到 `main` | 以远端文件校验结果为准；fine-grained PAT 的 Workflows 权限接口不可读，可能被拒绝 |

管理员需在 **Settings → Secrets and variables → Actions** 确认两个 Secret 与 `config/settings.yaml` 一致，并把机器人加入目标群、赋予发送权限。仓库代码无法读取 Secret 明文，也无法代替这一步。

## CI 与定时触发（追加说明）
- `.github/workflows/tests.yml`：push/PR/手动触发时运行 `pytest -q` 与 `ah grounding`，对应 AGENTS.md 验收口径。
- `daily.yml` 含主 cron（01:00 UTC）与备用 cron（02:47 UTC）；重复触发由台账 `data/telegram_delivery.json` 的 (date, chat_id) 幂等键保证不会重复推送。


## ORCHESTRATION（2026-11-07）：每天 4 次推送（北京 07/11/16/22 点）

- `daily.yml` 的 cron：`0 22`（采集，北京 06:00，publish 等到 07:00 再发）、`0 3`（11:00）、`0 8`（16:00）、`0 14`（22:00），均为 UTC。
- 只有第 1 次完整采集。第 2/3/4 次**复用当日报告**，只列尚未开赛的推荐与赛程，并在消息开头注明「第 N 次更新 / 沿用几点采集的数据」；不额外消耗赔率接口额度，也不生成任何新预测。
- 若早间采集失败（当日报告不存在），后续时段会自动补一次完整采集并按完整日报发送。
- 台账幂等键由 (date, chat_id) 扩展为 (date, chat_id, slot)；旧记录按 slot=1 处理。报告日期改按**北京日期**归档（`AH_DATE`），`ah.daily --date` 接受当前 UTC 或北京日期。
- 更新时段若已无尚未开赛的正式推荐，则不发送并在日志说明（不是故障）。
- `publish-recheck.yml` 取消定时（它每次完整重采，会重复消耗额度），仅保留手动触发。
- `workflow_dispatch` 支持 `slot` 输入（默认 `1`）：用于补跑已错过的 07:00 班次，publish 会等到北京 07:00 再发送。
- 【未验证】定时触发的实际时刻（GitHub cron 常有延迟）、Actions 实跑、群内实际到达；本地仅用假 Telegram 接口模拟。


## 2026-11-07：只显示推荐 + 中文 + 规则档位重标定（含深度研究）

用户需求（已澄清）：TG 群消息与 GitHub Pages **两端**同时「只显示正式推荐 + 中文显示」；授权改动模型算法框架以补全评分依据；不接受「0 场比赛」。

**先做了深度研究**（报告见交付物）：结论是「不能靠降阈值凑高信心标签」。实证：
- `analyze.WEIGHTS` 名义 8 因子合计 7.8，但 `daily.assess()` 实际只写 `form` / 可选 `h2h` / 自动 `line_movement` 三项（合计 3.0）；
- 3 份历史日报 14 个真实 score：`min 2.16 / P50 2.94 / P90 3.55 / max 5.00`，原 `high=7.0` **高于实测最大值**；
- 2026-11-07 的 7 场正式推荐里 6 场 `verification_advisory=True`，原代码把 advisory 全部强制压成 `low`，于是高/中档被吸空。

**本轮改动**
- `config/settings.yaml`：`high 7.0→4.0`、`medium 3.5→3.0`（按实测分位重标定，让最高档真实可达），`low 2.0` 不变。**明确标注：样本仅 14 场、未按概率校准、未经独立回测；这是「规则档位」，不是统计预测准确率，阈值变化不等于准确率提升。**
- `src/ah/daily.py`：advisory 不再强制压成「低」，改为保留真实 score 对应的规则档位 + 独立风险文本披露（研究报告第六节）；新增 `FEATURE_COVERAGE` 运行时代理自检输出。
- `src/ah/feature_contract.py`（新）：因子证据合同。8 因子逐个记录来源与状态；`home_away / injuries / xg / motivation` 如实标 `未取得`（不填 0、不造数据），`line_value` 为 observation 模式**不参与评分**。缺 source/as_of 的因子不得进评分。
- `src/ah/zh.py`（新）+ `config/team_aliases.yaml` 的 `display_zh` 段：中文显示层。译名来源为**中国体彩网竞猜队名通知、网易体育译名对照、俱乐部官方中文页、公开通用译名对照**（文件内逐条标注 URL）。**精确键匹配；未收录保留英文原名，绝不音译/意译造词；日韩澳球队不自行翻译**。报告 JSON 始终保留英文原文，grounding 仍逐字回溯。
- `src/ah/publish_telegram.py` + `src/ah/build_site.py`：两端都只渲染正式推荐卡片；有推荐时移除观察分析、模型实验盘、历史战绩、已撤销、赛程事实清单；**0 场时保留如实兜底**（TG 赛程事实清单 / Pages 观察名单），不编造赛事补位。队名中文、`vs→对阵`、盘口加受控中文术语（数字保留可回溯）、「信心」改称「规则档位」、头部统一写「规则档位，未经独立回测；不构成统计预测准确率」。数据来源品牌名（Bet365 / The Odds API / OddsPapi / infersports / 5DollarFootballAPI）保留原样。
- `src/ah/grounding.py`：advisory 契约更新——不再要求强制低信心，但仍拦截「已通过多源核验」假断言、要求显式披露核验风险；其余 fail-closed 校验不变。
- 测试按新契约对齐；本地相关测试通过，CI（Python 3.12）跑全量。

**未验证 / 风险（不得写成已完成）**
- 阈值仅按 14 场历史 score 分位重标定，**样本远低于统计校准所需**（研究引用第三方评测建议每概率桶 80–100 场），因此只称「规则档位」。
- 伤停 / xG / 主客场分离战绩 / 战意 / 赛程密度**仍未接入评分**，卡片里仍如实写「尚未自动核实伤停及首发」；接入需先逐联赛×赛季验收覆盖（研究建议连续 14 天达标后才从 observation 转 scoring）。
- 新增因子与权重**未经独立回测**，不宣称命中率或 ROI 改善；未来需按时间外切分、可靠性曲线、Brier score 与市场去水基准评估。
- 译名表为展示层映射，来源已标注但未逐条官方页复核；如发现错译以报告英文原文为准。
- GitHub cron 实际触发时刻与 Telegram 端真实到达仍以运行记录 / `message_id` 台账为准。

**AGENTS.md 冻结清单执行记录**：本轮 `MODEL CHANGE / ORCHESTRATION` 已获用户 2026-11-07 明确授权触碰 `analyze.py` 阈值相关配置与 `daily.py` 的 advisory 档位逻辑；未改动评分公式、核验算法、盘口采集口径与数据源开关。


## 样本积累（2026-11-07）：把「开球前照片」和「踢完的账」都攒起来

之前只有 4 天日报、`stats.json` 里 3 条推荐 / 2 场算完结果，样本远不够校准。本轮不改算法、不额外消耗接口额度，做两件事：

**A. 结算修稳 + 失败可见**
- `.github/workflows/settle.yml` 增加备用 cron `30 12 * * *`（主 `0 9 * * *`；GitHub cron 实测曾漂到 16:28 UTC 且失败）。settle 以 `settled` 标记幂等，重复触发不会重复计数。
- 无论成功失败都写 `data/settle_health.json`：列出 `predictions.csv` 里仍未结算的条目（date / match_id / 对阵 / 开球 / 状态 / 档位），**赛果字段为空就写「未取得」，不补造比分、不推断原因、不假称已补齐**；失败时日志打 `::error::`。
- 赛果仍由 `python -m ah settle` 按已配置的 API-Football / football-data 数据源回填。密钥没返回数据就保持「未取得」，本轮不承诺补齐几条。

**B. 候选全留档，推送只挑推荐**
- 新增 `src/ah/samples.py`：`python -m ah samples pool <date>` 把当日报告里 `picks + watched + watchlist` **全部候选**逐条写入 `data/samples/<date>.json`（按 match_id 去重）。11-07 实测 32 条 = 7 推荐 + 25 未推荐，样本量从每天 7 条变 32 条。
- 每条只抄报告里真实存在的字段；缺字段写「未取得」；`result` 固定「未取得」、`settled=false`；**不生成任何预测结论、盘口或赛果**。`sample_role` 只标记该场在报告里的归属，不代表模型判断。
- `daily.yml` 在采集末尾调用 pool，并把 `data/samples` 加入提交路径；群消息与网页**仍只显示正式推荐**，两端展示完全不变。
- 一天只在完整采集时段（slot=1 / manual）写一张开球前快照；2/3/4 更新时段复用当日报告，不重复写。

**验收口径 / 未验证**
- 已验证（本地）：`tests/test_samples.py` 全通过；全量 pytest；11-07 样本池 32 条、无 `result` 字段被填入；`data/settle_health.json` 列出 12 条未结算。
- 未验证：备用 cron 的真实触发时刻与延迟；本轮 settle 手动运行实际回填了几场（取决于密钥与数据源返回）；Telegram 端收件（本轮**未额外触发 TG 推送**，避免刷屏）。
- **样本量仍不足以做概率校准**：研究口径是每个档位 80–100 场；A+B 之后每天约 32 条候选入池，需连续积累数周才能做第一轮时间外回看。阈值仍是「规则档位，未经独立回测」。
- 历史盘口快照只有 3 天，缺的部分**不能补造**，只能从今天开始攒。
