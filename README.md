# 先知 · 亚盘每日预测

先知从可用数据源采集足球赛程、亚盘和球队近期状态，按现有规则筛选比赛，通过 Telegram 发布日报，并记录已发布推荐的赛果与统计。数据不足或没有比赛通过筛选时，日报可以没有推荐。

**真实数据原则：不得编造赛程、盘口、水位、球队状态、伤停、比分、来源或推荐理由；未取得或未核验的信息须如实标注，不能将猜测写成事实。** 不得声称已经成功推送、回测或验证，除非有对应运行结果支持。

## 当前自动任务

所有时间为北京时间；GitHub 定时任务实际启动时间可能延迟。日报日期使用 UTC 日期，运行时以工作流中的 `date -u` 为准。

| 工作流 | 配置时间 | 当前行为 |
|---|---|---|
| `daily-report` | 每天 10:00 | 采集数据、生成并保存日报；新生成日报后发送 Telegram。 |
| `settle-and-stats` | 每天 17:00 | 尝试获取已完赛比分，结算已记录的预测并更新统计。无法确认的结果保留待处理。 |
| `pages` | 报告或相关数据更新等触发 | 生成并部署 GitHub Pages 看板。 |
| `Check live collection` | 手动触发；其工作流文件推送到 `main` 时也触发 | 用当前数据源做采集预览，不提交日报，也不发送 Telegram；仍会消耗接口额度。 |

GitHub Actions 提供运行环境，以上任务不要求用户另行购买服务器。任务是否真正完成，以 [Actions 运行记录](https://github.com/modouxin-dev/ah-predictions/actions)和实际 Telegram 消息为准。

**当前未实现的自动化：** 日报流程尚未自动采集伤停或首发；赛前 50 分钟检查、赛后逐场复盘及其 Telegram 推送没有对应的自动工作流。已有记录和发布命令可供手动使用，见下文。历史赛果结算与统计也不等同于对预测模型完成独立回测。

## 自动日报与发布规则

`python -m ah.daily` 采集未来 36 小时的赛事，默认初筛最多 8 场；`--max-fixtures` 可设置 1 至 12 场。在 football-data 已启用时，有限的初筛名额优先分配给其支持球队状态数据的联赛，再按开赛时间展示选中的比赛。

- 赛程来自已启用且可用的数据源；盘口快照支持 OddsPapi、The Odds API、InferSports 和 API-Football。当前核验配置排除 API-Football；球探盘口只作为补充观察，不自动算作已核验盘口；5Dollar 数据用于补充已有推荐。
- 正式候选需要真实赔率、有效报价、同机构多源盘口核验、近期状态和同来源同机构的初盘。核验配置位于 `config/sources.yaml`；当前日报设置包括至少两个来源、三家机构和一家具备跨来源核验的机构。
- 报价超过 30 分钟但不超过两小时，且其他核验条件均满足时，信心降低一级；超过两小时则拦截。推荐仍须通过 `config/settings.yaml` 的发布策略：当前为**中信心及以上**，最多 10 场，深盘及共识偏离另有约束。
- 数据缺失、核验未通过或评分不足的场次进入观察名单；可以如实发布 0 场推荐的日报。现有规则评分没有经过独立验证，不能保证准确率或盈利。

日报保存在 `reports/YYYY/MM/YYYY-MM-DD.json` 和对应 Markdown 中，推荐记录写入 `data/predictions.csv`。日报同一 UTC 日期的 JSON 已存在时不会覆盖：定时运行不会重复推送，手动运行 `daily-report` 会重新发送已有日报，**不会重新采集并覆盖它**。

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

**截至 2026-11-06 的项目状态：** `config/sources.yaml` 中 API-Football 仍设为关闭；仅保存密钥不会自动启用它。当天的实时采集预览中，OddsPapi 的亚冠精英和中超赛程请求返回 HTTP 404，原因尚未确认，不应将其当作密钥或账户状态的定论。

更换 Telegram 机器人时，需要在 GitHub 更新 `TELEGRAM_BOT_TOKEN`，将新机器人加入目标群并确认 `TELEGRAM_CHAT_ID`。创建机器人不会自动修改项目配置；更新令牌后是否能发送，须通过实际运行确认。

## 最近一次实测

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
