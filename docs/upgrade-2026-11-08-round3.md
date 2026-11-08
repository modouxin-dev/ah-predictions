# 2026-11-08 第三轮：用户授权后的全量升级（三类单独提交）

授权来由：用户在深度检查后明确「没做的（要你单独授权）：CLV 正式写回台账、第三比分源、
线上规则回测+概率校准、观察块统一渲染、provider 静默降级改造、时区统一、注册源降级……
授权帮我一次性搞定」。

执行方式：**链式 3 个 commit（`DATA SOURCE:` / `MODEL CHANGE:` / `DISPLAY/OPS:`），
只更新一次 main ref**。原因：每次 push 都会触发 `tests.yml` / `pages.yml`，
分批 push 会出现「代码已改、模块还没上传」的中间态导致 CI 失败。三个 commit 各自
带前缀与依据，满足 AGENTS.md「单独提出、单独评审、单独提交」，最终 ref 指向第三个。

**令牌：保留，未注销、未轮换、未外传。**

---

## A. `DATA SOURCE:` —— 数据源层（5 项）

### A1 CLV 正式写回台账（口径对齐，不另立）
- 新增 `src/ah/clv.py`。**发现并纠正一处口径冲突**：`src/ah/audit/experiment_track.py`
  早已定义 `clv()` = `o_bet(十进制) × p_fair(收盘去水) − 1`（价格型，仅收盘盘口=下注盘口时算），
  `line_move()` = `line − close_line`。我最初把线差写进 `clv` 是另立口径，已全部改回。
- 统一 schema：`settle.py` 新增 `CLV_FIELDS`（12 列），其中 `close_line / close_odds /
  close_odds_opp / close_fair_p / close_captured_at` **复用实验盘既有列名**；
  `experiment_track.py` 的 `EXTRA_FIELDS` 去重为 3 列，两套台账不再重复列名。
- `line_move` / `clv` 直接调用 `experiment_track.line_move()` / `clv()`，不改其实现。
- 真实回填结果（【已验证】本地文件解析）：
  | 字段 | 有值 |
  |---|---|
  | `line_open` | 22 / 24 |
  | `price_ts` | 19 / 24 |
  | `open_to_report_move` | 22 / 24 |
  | `close_line`（来自 `final_line`） | 2 / 24 |
  | `line_move` | 2 / 24（-0.5 / 0.0） |
  | `clv`（价格型） | **0 / 24，全部「未取得」** |
- **为什么 clv 全是未取得**：仓库里根本没有 `stage=close` 快照，也没有收盘两侧水位，
  无法去水算 `p_fair`。绝不拿线差冒充 CLV、绝不补造。
- 为此新增 `.github/workflows/close-snapshots.yml`：`config/sources.yaml` 与
  `odds_snapshots.py` 早有 `close` 阶段能力，但**此前没有 workflow 跑它**；现在
  UTC 19:00 / 22:00 用 `close --backfill` 补采，并把真实 `stage=close` 行数写
  `data/close_capture.json`。跑起来之后后续日期才可能产出非空 `clv`。
- `daily.yml` 采集后执行 `python -m ah clv --apply`，产物 `data/clv_backfill.json` 与
  `data/predictions.csv` 一并提交。

### A2 第三比分真值源
- 新增 `src/ah/score_truth.py` + `config/sources.yaml score_truth` 声明块（3 个免注册源：
  titan007 bfdata、the-afc.com 落地页、jleague.jp），每条带
  `source_id / source_url / as_of / coverage_status`。
- 复用仓库 2026-11-04 已实测的 `scrapers/afc.py::parse_scores`（`parser: afc_scores`），
  不另写猜测型解析器；其余用保守正则，只认「队名 + 明确 m-n 比分」，认不出返回空。
- 判定：`AGREE / DISAGREE / THIRD_UNAVAILABLE / PRIMARY_MISSING / NO_MATCH`；
  AGREE 不等于真值已证明，DISAGREE **不自动改写结算**。
- 真值回填默认关闭：`AH_THIRD_SCORE_FALLBACK=1` 且 ≥2 个独立 source_id 给出完全一致比分
  且主源为空，才允许回填。绝不推断、不补造比分。
- **本会话真实 dry-run 事实（不粉饰）**：3 个源全部 `NO_EXPLICIT_SCORE_FOUND`，
  AFC 页返回 `SiteUnavailable`（单页外壳），claims=0。即「已接线、会安全降级」，
  **不等于已获得可用的第三比分真值**。
- 接入 `settle.yml` 的 `::always::` 事实步骤，产物 `data/score_truth_check.json`。

### A3 注册源降级为可选增强
- `config/sources.yaml` 新增 `credential_policy` 块：5 个需注册接口声明
  `mode: optional_enhancement` / `on_missing: skip_with_fact`，带 `client_file` 指向。
- 新增 `src/ah/source_policy.py`：按「当前进程是否注入该 env var」给
  `active / skipped_no_credential`，并做**静态契约检查**——逐个确认 env_var 真的出现在
  对应 provider 客户端里（5/5 通过），避免「声明可选、代码却硬依赖」。
- 只判注入，**不读值、不判断 key 有效/订阅/额度/覆盖**；编排声明 secret ≠ Secret 已配置。
- 接入 `daily.yml` / `settle.yml` / `source-probe.yml`，产物 `data/source_policy.json`。
- 本地 dry-run：declared=5 / active=0 / skipped=5 / gates=5（沙盒没注入 Secrets，
  **不代表线上缺 key**）。

### A4/A5 数据与编排接线
- `config/sources.yaml` 只**追加**两个顶层声明块，未改任何既有 provider 的
  `enabled` / 核验 / 采集算法。
- `predictions.csv` 25→37 列、`experiment_picks.csv` 33→40 列，均为向后兼容扩列，
  旧行数值一个未动。
- `daily.yml` / `settle.yml` / `source-probe.yml` 增加事实步骤，全部 `continue-on-error`
  或 `if: always()`，不阻断采集、结算、推送。

---

## B. `MODEL CHANGE:` —— 线上规则回测与概率校准（研究层）

**关键边界：`analyze.py` / `policy.py` / `daily.py`（build_report/assess）/
`config/settings.yaml` 一行未改，`live_model_modified=false`。**
用户授权的是「回测 + 校准」，不是「悄悄改线上发推荐的规则」。真正把校准结果部署到
评分需要另一次独立决策与独立回测证据。

- 新增 `src/ah/rule_lab.py` + `.github/workflows/rule-lab.yml`，接入 `review.yml`。
- 输入：`data/samples/*.json`（开球前候选池）+ `data/predictions.csv`（已结算真值），
  按 `match_id` 内连接、按 `kickoff_utc` 排序，保证时序不泄漏。
- walk-forward：按日期滚动，**每个块只用更早块作历史规模说明，当前块为样本外**；
  明确写「线上规则无可拟合参数，不是重新训练模型」。
- 门槛沿用 `review.py`：`MIN_SAMPLE=30` / `MIN_BUCKET_SAMPLE=10`，不足一律
  `INSUFFICIENT_SAMPLE`，命中率 / ROI / 分桶数字**留空**。
- 概率校准：官方台账无 `model_p`（只在实验盘），故 Brier / 可靠性曲线
  `NOT_COMPUTED`；`tier_calibration` 的「档位→实际胜率」明确标注**描述性统计，
  不是校准概率，不得展示为高胜率**。
- 预测质量层（log loss / 校准）与下注规则层（ROI / 回撤 / CLV）分层，不混成一个数。
- **防幻觉闸门**：生成后自检，出现「准确率提升 / 模型更准 / ROI 改善 / 已校准 /
  显著提升 / 稳定盈利」等断言直接 `RuntimeError` 拒绝输出（有专门测试锁住）。
- 真实 dry-run（【已验证】）：候选池能 join 上已结算的 **9 场**（不是 14 场，因为
  只有 2 天候选池），`walk_forward.overall = INSUFFICIENT_SAMPLE`、
  `calibration = NOT_COMPUTED`、`live_model_modified = False`。
  **不产出任何命中率或 ROI 结论。**

---

## C. `DISPLAY/OPS:` —— 展示与工程（3 项）

### C1 观察块统一渲染
- 新增 `src/ah/render_observation.py`，把渲染 + 校验收成单一真源。
- `publish_telegram.observation_blocks()` → 薄包装委托；
  `grounding.check_observation_markup()` → 委托同一 `validate()`；
  `recheck.py` → 直接调 `render_observation_section()`。三处不再各写一套字符串。
- 每条观察**逐条**带「非正式推荐，不计战绩」，不再只靠 section 标题；
  禁止措辞（`🎯 推荐` / `稳赢` / `高胜率` / `已校准概率` 等）统一校验。
- 不改 `daily.py` 的 watchlist/observation 生成、不改评分/核验/阈值、不凑数
  （沿用 `DAILY_ITEMS_MAX=7` 与「有真实评分才展示」）。
- 兼容验证：`tests/test_daily.py`（⚽ 计数）、`test_upgrades.py`（免责标记）、
  `test_telegram_format.py`（群消息不含观察块）全部保持通过。

### C2 provider 静默降级改造
- 新增 `src/ah/degrade.py`：只记「模块 / 阶段 / UTC 时刻 / 异常类型」，
  原子写 `data/degradation_facts.json`，每进程按 `(source, stage, error_type)` 去重，
  上限 200 条，**自身永不抛异常**，原 `except` 的 `pass / return None / continue` 完全不变。
- 深度检查列出的 **17 处**静默异常逐处接入（api_football / paper_trades / consensus /
  form_history / odds_snapshots ×2 / oddspapi / publish_telegram / lineups / settle ×2 /
  source_audit ×2 / source_policy / source_status ×2 / tg_bot）。
- `tests/test_degrade.py` 静态契约：逐个断言 17 个 `(file, stage)` 的降级调用就在
  `except` 紧邻下一行；另有测试验证不回显 `apiKey=` / 异常消息、写盘失败不抛、
  事件上限。
- 不回显异常消息、URL、响应体或密钥；不推断失败原因；不改任何 provider 算法。

### C3 时区统一
- 新增 `src/ah/timeutil.py`：`BJT` 优先 `ZoneInfo("Asia/Shanghai")`（含历史规则，
  比固定 +8 更正确），无 tzdata 时安全回退固定 +8；`BJ = BJT` 兼容存量命名；
  统一 `utc_now() / utc_now_iso() / bj_date(now) / to_bj()`。
- 10 个文件改为引用共享常量：`match.py` / `model_lab.py` / `tg_bot.py` /
  `scrapers/common.py` / `audit/paper_trades.py` / `date_consistency.py` /
  `build_site.py` / `publish_telegram.py` / `render_observation.py` / `delivery_gaps.py`。
- `delivery._bj_date()` 回退改走 `bj_date(datetime.now(UTC))`，**保留测试时钟注入能力**
  （`test_bj_date_is_utc_plus_8` 仍通过）。
- `tests/test_timeutil.py` 断言所有模块的 `BJ/BJT` 是同一个对象、UTC 跨午夜日期正确。

---

## D. 验收证据【已验证】

- 本地 `pytest 986 passed, 1 skipped`（Python 3.10）
- 精简副本自包含（删 `data/` + `reports/` 由 conftest 补齐 57 文件）此前已验证，
  本轮新增测试均为 `tmp_path` 自包含，不依赖历史数据
- `GROUNDING_OK`（11-08 报告，7 场推荐）
- 13 个 workflow YAML 全部解析通过，均有 `concurrency.group` 与 job `timeout-minutes`
- 台账扩列后 `settle` / `stats` / `render_report` / `experiment_track` 全套回归通过
- 真实 dry-run 数字：CLV / 第三比分源 / 凭据策略 / rule-lab 见上文各节
- **未触发任何日报 dispatch、未发送 Telegram、未重复推送**

## E. 【未验证】边界（如实）

| 项目 | 何时能确认 |
|---|---|
| `stage=close` 快照能否真采到 | `close-snapshots.yml` 在真实 CI 跑一次后看 `data/close_capture.json` |
| 第三比分源能否拿到可用声明 | 本会话 3 源全部 `NO_EXPLICIT_SCORE_FOUND`；需运行时 `fetch_facts` |
| 5 个 Secret 实际存在/有效 | PAT 读 `/actions/secrets` 403；下次 `daily.yml` 看 `registered_configured` |
| 价格型 CLV / 概率校准 / 线上规则回测结论 | 均 `NOT_COMPUTED` / `INSUFFICIENT_SAMPLE`（9 场 < 30 场） |
| Telegram 接收端 | 台账只有 `message_id` |
| 外部定时准时率、结算积压成因 | 需账号侧 / 日志侧（artifact 下载仍被 403） |

## F. 边界声明

本轮**未改** `analyze.py` / `policy.py` / `daily.py` 的 assess/build_report /
`config/settings.yaml` 的阈值与 policy / `config/sources.yaml` 既有 provider 块 /
任何 provider 的采集与核验算法；未编造任何赛程、盘口、水位、比分、推荐、命中率、
ROI、CLV 数值或校准概率；未发送 Telegram；令牌保留。


## G. 首跑事故与修复（真实 CI，【已验证】）

三类提交上 main 后，我触发了 4 个**只产事实、不发消息**的工作流。真实结论：

| 运行 | 结论 | 说明 |
|---|---|---|
| `42481957841` tests | success | 3.12 真实环境全套通过 |
| `42481957682` pages | success | 站点构建通过 |
| `42482023855` Source runtime selfcheck | success | 注册源自检 + source-policy 事实 |
| `42482027896` accuracy-review | success | `data/rule_lab.json` 已落 main |
| `42482021772` Online-rule research | **failure** | 见下 |
| `42482025915` Capture closing lines | in_progress（观察时） | `close --backfill` 仍在采集 |

**失败根因（可复核）**：run `42482021772` 的
`Walk-forward + calibration facts` 步骤 **success**（命令在真实 3.12 跑通），
失败的是第 6 步 `Commit research facts`——我新写的两个 workflow 用了裸
`git commit && git push origin HEAD:main`，而多个定时任务同时改 main，
造成 non-fast-forward。这是**编排缺陷，不是代码或模型缺陷**，也不影响事实生成。

**修复**：`rule-lab.yml` / `close-snapshots.yml` 的提交步骤统一改用仓库已有的
`scripts/safe_commit.sh --skip-tests`（文件锁 + `pull --rebase` + 竞争退避重试），
并补 `GITHUB_AH_TOKEN`；提交失败降级为 `::warning::`，不再把整个 run 标红。
`tests/test_workflow_contracts.py` 新增契约测试，静态断言两个 workflow 不含裸 push。
修复后本地 `pytest 988 passed, 1 skipped`。

**仍【未验证】**：close 快照本次是否真采到 `stage=close` 行（该 run 观察时仍在跑）、
第三比分源可用性、5 个 Secret 有效性、价格型 CLV、概率校准、线上规则回测结论、
Telegram 接收端。事实文件 `data/close_capture.json` / `data/source_policy.json` /
`data/score_truth_check.json` 在观察时尚未落 main，属「已接线、待定时班次产出」。
