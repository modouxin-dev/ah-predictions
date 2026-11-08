# 本项目 AI / 维护者工作指令（强制）

> 本文件是**约束**，不是说明文档。任何智能体、脚本或维护者在改动本仓库前必须遵守。
> 目的：杜绝「推测当事实、幻觉当数据、断言当证据」。

## 1. 证据分级（所有结论必须带标签）

| 标签 | 含义 | 允许写入产物 |
|---|---|---|
| `【已验证】` | 有可复核的本地文件行号、API 响应、命令退出码或运行记录 ID | 是 |
| `【运行时代理自检】` | 由工作流在运行时代理实测，写入台账/日志 | 是，但必须附 `run_id` 与时间戳 |
| `【未验证】` | 无法读取或无权核验（如 Secrets 明文、Actions 步骤日志、Telegram 端收件记录） | **只能列为待办，不得写成已完成** |
| `【推断】` | 基于现象的判断，未直接观测 | 必须写明依据与替代解释，不得单独作为结论 |

禁止把 `【推断】` 或 `【未验证】` 包装成「已修复 / 已推送 / 已验证 / 应该能收到」。

## 2. 硬性禁止（防推测 / 防幻觉 / 防断言）

1. **禁止编造数据**：不得生成报告中不存在的赛程、对阵、盘口、水位、比分、伤停、首发、来源、推荐理由、命中率或战绩。所有展示字段必须能在 `reports/**.json` 的对应键中逐字回溯。
2. **禁止补全空值**：`null` / 空字符串 / 缺键一律渲染为 `未取得`。不得用「待定」「预计」「应该是」「通常为」等措辞填充，也不得用默认值冒充真实值。
3. **禁止无证据断言**：
   - Actions 步骤绿色 **不等于** 消息已送达；
   - `getMe` / `getChat` 通过 **不等于** `sendMessage` 成功；
   - 只有 Telegram 返回 HTTP 200 + `ok=true` + 非空 `message_id` 并写入 `data/telegram_delivery.json`，才允许写 `status: sent` 或「已送达」。
   - 禁止出现「推送成功」「已成功推送」「保证命中」「稳赢」「必定」等词，除非同一处附带 `message_id` 证据。
4. **禁止静默跳过**：缺 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`、报告文件不存在、目标会话不一致时，必须**非零退出并打印原因**，不得 `exit 0` 假装成功。
5. **禁止猜目标会话**：`TELEGRAM_CHAT_ID` 与 `config/settings.yaml` 的 `telegram.chat_id` 同时存在且不一致时，立即报错；不得静默任选其一。
6. **禁止把 0 场伪装成有场次**：`picks=[]` 时只能输出「0 场正式推荐」+ 报告中**已存在**的赛程事实清单（明确标注「非推荐、不含预测结论」）。不得为了凑数生成任何预测倾向、模型评分或盘口。
7. **禁止把模型观察写成推荐**：`observation` 块、实验盘、`watchlist` 一律标注「非正式推荐 / 不计战绩」。

## 3. 冻结边界（模型算法框架，禁止改动）

以下文件属于模型/算法框架，**本次及后续修复不得修改其评分、核验、阈值、权重、策略逻辑**：

```
src/ah/analyze.py            # 权重、score/confidence/select
src/ah/policy.py             # 发布门槛、深盘、共识偏离
src/ah/daily.py              # assess / build_report 的采集、核验、评分、观察生成
src/ah/odds_snapshots.py     # 跨来源核验判定
src/ah/enrich.py             # 特征工程
src/ah/consensus.py
src/ah/settle.py / stats.py
src/ah/model/**
src/ah/api_football.py / football_data.py / odds_api.py / oddspapi.py
src/ah/infersports.py / fivedollar.py / form_history.py
src/ah/fetch_fixtures.py / fetch_odds.py
config/settings.yaml         # 阈值、置信度、policy、push_rules
config/sources.yaml          # 核验口径与数据源开关
config/model.yaml / leagues.yaml / team_aliases.yaml / oddspapi_ah_markets.json
research/** / schema/** / templates/report.md.j2
```

**允许改动范围（仅编排层、发布层、展示层、对账层）**：
```
.github/workflows/**          # 调度与 job 编排
src/ah/delivery.py            # 发送台账、目标会话一致性、重试、送达证据
src/ah/grounding.py           # 事实一致性闸门
src/ah/publish_telegram.py    # 仅 send() 与新增「赛程事实清单」展示；不动 daily() 的 picks/observation 生成逻辑
src/ah/recheck.py             # 仅中断策略与发送编排
src/ah/__main__.py            # 命令注册
scripts/check_grounding.py
tests/**
AGENTS.md / docs/grounding-policy.md / README.md（追加章节）
data/telegram_delivery.json
```

> 若确需调整模型阈值或核验口径，必须**单独提出、单独评审、单独提交**，并在提交信息中写明「MODEL CHANGE」及依据；不得混在编排修复里。

### 2026-11-06 用户已授权的例外（按「先 C 后 A」分两次单独提交）

- **C（数据源）**：用户已更换 `API_FOOTBALL_KEY` 并加入运行环境，授权重新启用数据源、排查 OddsPapi 404、并让每日运行显式重新采集。
  - 允许：`config/sources.yaml` 中 `api_football.enabled` 开关；`src/ah/daily.py` 的 `main()` 增加 `--refresh`（**仅 CLI 编排层，禁止改 `build_report` / `assess` 的算法**）；新增 `source-probe` 自检与工作流；`.github/workflows/**` 编排。
  - 禁止：改 `cross_verification_draft`、改 `confidence_thresholds`、改 WEIGHTS、改 policy。这些留给 A。
  - 提交信息前缀：`DATA SOURCE:`。
- **A（模型框架）**：用户授权在 C 之后单独执行，提交信息前缀必须为 `MODEL CHANGE:`，且必须写明依据与未回测风险。
  - 允许范围：`cross_verification_draft.advisory_only` 真正生效（核验失败改为降级标注而非硬拦截）、按现有可用特征重标定 `confidence_thresholds`、移除已恢复数据源的 `exclude_sources` 排除。
  - 硬底线（防幻觉，不得放宽）：推荐仍必须有**真实采集**的 `line` / `odds` / `price_ts` / 同机构初盘 / ≥3 场近期状态；不得用默认值、推测值或聚合值补全；核验未通过必须如实标注，不得隐藏。
  - 仍然禁止：编造赛程/盘口/水位/比分/倾向/命中率；把「能连通」写成「数据充足」；把「已推送」写成「已送达」；把未回测的规则写成「准确率提升」。
- **执行记录**：2026-11-06 的 `MODEL CHANGE:` 提交已执行 A；依据与未回测风险见 README「MODEL CHANGE」章节。

## 4. 每次改动必须附的证据

提交信息或 PR 描述中必须包含：
- 改动文件 + 行号；
- 对应的已验证事实（本地命令输出 / API 响应 / run id）；
- 明确列出本次**未验证**项；
- 是否触碰冻结清单：是 / 否。

## 5. 落地验收标准（不得降低）

1. `pytest -q` 全绿；
2. `python -m ah grounding --report <当日报告>` 返回 `GROUNDING_OK`；
3. 0 场报告的推送**纯文本**中必须出现报告里真实存在的对阵，且不含任何编造队名；
4. 以「发送台账」为唯一幂等键：报告已存在但无 `message_id` 记录时仍会发送一次；已有记录则跳过；
5. 真实送达以 `message_id` 入账为准；未取得时如实标注 `【未验证】`。


## 2026-11-07 授权与执行记录

- 用户明确授权「补基本面因子 / 重标定阈值」，故本轮 `MODEL CHANGE` 触碰 `config/settings.yaml` 阈值与 `src/ah/daily.py` 的 advisory 档位逻辑；**未改** `analyze.py` 的 WEIGHTS/score 公式、`odds_snapshots.py` 核验算法、盘口采集与数据源开关。
- 深度研究报告结论已纳入 `docs/grounding-policy.md` 口径：缺 source/as_of 的因子不得进评分；盘口超时不得当价值证据；覆盖声明 True 不代表单场数据存在；阈值变化不得写成准确率变化；0 场不得用任何赛事补位；缺译名不得造词；「未取得」不得改写为「无伤停/无影响」。
- 新增 `src/ah/feature_contract.py`：新因子默认 `observation`，不参与 score；连续覆盖验收达标前不得转 `scoring`。
- 展示层中文只做精确键映射，未收录保留英文原文；报告 JSON 原文是唯一可回溯锚点。
- 界面必须写「规则档位，未经独立回测；不构成统计预测准确率」，禁止把 high/medium/low 写成「高胜率 / 已校准概率」。


## 2026-11-07 样本积累执行记录（不触碰模型算法）

- 仅新增 `src/ah/samples.py` 与 `tests/test_samples.py`，改 `__main__.py` 路由、`daily.yml`/`settle.yml` 编排、README；**未改** `analyze.py` / `daily.py build_report/assess` / `policy.py` / `odds_snapshots.py` / 阈值 / 核验算法 / 推送渲染。
- 硬约束：样本池与结算健康盘点只读报告 JSON 与 `predictions.csv` 现有字段；缺字段写「未取得」；不填比分、不推断原因、不生成预测结论、不假称「已补齐」「已结算」；`result` 固定「未取得」。
- 「候选全留档」不等于「多推比赛」：两端展示仍只发 `policy.published` 的正式推荐。
- 样本池一天一张开球前快照；赛果由 settle 按已配置数据源回填，回填不了就保持「未取得」。


## 2026-11-07 二次升级执行记录

- 用户授权「全都要·能做的全做」；未触碰 `analyze.py` WEIGHTS / score 公式、`odds_snapshots.py` 核验算法、盘口采集口径、两端展示与推送渲染。
- 新增因子必须带 source_id / source_url / as_of / coverage_status；`value=None` 不等于 0 分；缺证据不得进 score。
- `injuries / xg` 保持 missing；`line_value` 因共识在评分后 attach，时序上不能进本次 score，不得假称已接入。
- 样本不足时回测一律 `INSUFFICIENT_SAMPLE`，禁止输出命中率/校准/「模型更准」类断言；Brier 与可靠性曲线无校准概率时 `NOT_COMPUTED`。
- 赛果只来自 `predictions.csv`；`未取得` 不得改写为「无伤停 / 无影响 / 已结算」；回填归因只按开球时间做事实分类，不推断数据源失败原因。
- 因子转 scoring 必须通过连续覆盖验收 + 离线重放零场保护；未达标不得自动改 `settings.yaml`。


## 2026-11-08 自动复盘执行记录（不触碰模型算法）

- 用户授权「按策划实现预测准确率自动统计和复盘」。仅新增 `src/ah/review.py`、`.github/workflows/review.yml`、`tests/test_review.py`，改 `src/ah/__main__.py` 命令注册、README/AGENTS.md 追加章节；**未改** `stats.py` / `settle.py` / `analyze.py` / `policy.py` / `daily.py` / `odds_snapshots.py` / `config/**` / `research/**` / `schema/**` / 展示与推送渲染。
- 命中率唯一权威仍是 `data/stats.json`；复盘层从 `predictions.csv` **独立复算仅用于对账**，不另立口径、不对外当结论。对账不一致时写「未取得」。
- 赛果只来自 `predictions.csv`；`result` 空写「未取得」，不补造比分、不推断未结算原因；`track=official` 才计入；`void/cancelled` 不进分母。
- 样本不足（总体 < 30 场 / 分桶 < 10 场）一律 `INSUFFICIENT_SAMPLE`；窗口、分桶命中率与盈亏同时留空，禁止自相矛盾；禁止「准确率提升 / 模型更准 / ROI 改善」类断言，生成后自检命中即拒绝输出。
- 复盘**不发送 Telegram**；`::warning::` / `::error::` 注解与 `alerts[]` 是唯一告警通道，避免重复推送与「已推送 = 已送达」混淆。
- 界面必须写「规则档位，未经独立回测；不构成统计预测准确率」；分桶名逐字引用 stats.json，不做译名推断。
- 证据：`pytest 883 passed, 1 skipped`（本地 3.10）；CI 3.12 与 Actions 首跑结果见交付说明。未验证：settle 积压成因、Telegram 接收端、概率校准、外部定时准时率。
- 是否触碰冻结清单：否。


## 2026-11-08 收尾升级执行记录（不触碰模型算法）

- 用户要求「令牌保留，全部升级过关再注销」；本轮只做冻结清单外、无需外部账号的收尾升级，令牌**未动**。
- 允许改动范围内新增 `src/ah/runfacts.py`、`docs/external-timer.md`、`tests/test_upgrades.py`，改 `.github/workflows/daily.yml`、`src/ah/delivery.py`（新增 `AH_DELIVERY_MODE` 白名单与 backfill）、`src/ah/grounding.py`（观察块防回退检查）、`src/ah/__main__.py` 命令注册、README/AGENTS 追加章节。
- 硬约束不变：打点与新鲜度检查**只标注不阻断**，缺字段写「未取得」，不推断延迟原因、不补造盘口水位或比分；观察块检查不改 `publish_telegram.daily()` 的生成逻辑；`backfill` 按 `(date, chat_id, slot)` 幂等，不强制重发；非法 mode 立即失败。
- 不改 `requirements.txt` 版本 pin、不造 lock 文件（需 3.12 CI 生成）；不清理历史台账里 2026-11-06 的 4 条重复记录（事实数据只读，需单独评审）。
- 冻结清单内仍待单独授权：`render_observation()` 统一渲染、recheck/daily 统一幂等键、报告新鲜度 SLO 转阻断策略、阈值/评分/盘口核验调整、概率校准。
- 证据：`pytest 894 passed, 1 skipped`（本地 3.10）；10 个 workflow YAML 解析与并发/超时契约通过；`GROUNDING_OK`；新鲜度 dry-run 两条告警未阻断。CI 3.12 与 daily 实跑见交付说明。
- 是否触碰冻结清单：否。


## 2026-11-08 数据源依赖与零注册自愈执行记录（不触碰模型算法）

- 用户要求「信息源采集升级，不要太依赖需要注册 api」；令牌按要求**保留**，未注销、未轮换。
- 仅新增 `src/ah/source_audit.py`、`src/ah/delivery_gaps.py`、`docs/source-dependency.md`、`tests/test_source_upgrades.py`；改 `.github/workflows/daily.yml` / `source-probe.yml`、`src/ah/__main__.py`、`docs/external-timer.md`、README/AGENTS 追加章节。
- **未改** `config/sources.yaml` 开关、任何 provider 客户端、`analyze.py` / `policy.py` / `daily.py` / `odds_snapshots.py` / `settle.py` / `stats.py` / `config/settings.yaml` / `research/**` / `schema/**` / 展示与推送渲染；未新增任何需注册数据源。
- 硬约束：密钥只记布尔、绝不回显值；缺 key 只告警不阻断采集；公开源探测只记 HTTP 状态/字节数/异常类型，**不解析成赛程、盘口、水位、比分**，不写报告；`titan007` 需特殊参数不探、`oddsportal/okooo` partial 不探、`spl.com.sa` 条款禁止自动化永不探；连通 ≠ 数据充足，不得写成「已恢复」。
- 漏发盘点：只读台账，未到点不报；`picks_count=0` / 空 `message_ids` 不算送达；未到点却已有记录标 `DELIVERED_BEFORE_TARGET` 并给 info 告警，**不推断为误发或正常**；给建议补发 body 但**不自动触发**、不写台账、不发消息。外部定时降级为可选增强。
- 真正把注册源替换为免注册主源、按 `credential_present` 动态跳过 provider、改核验口径或结算真值源，均属冻结清单，须单独 `DATA SOURCE:` 授权与提交，本轮**未做**。
- 证据：`pytest 910 passed, 1 skipped`（本地 3.10）；真实配置 dry-run `registered=5/configured=0/keyless=11`（本地无 Secrets，不等于线上缺 key）；真实台账盘点 `slot=1 已送达 / slot=2,3 漏发 / slot=4 离档`，只告警不触发；10 个 workflow YAML 解析与并发/超时契约通过。CI 3.12 与 Actions 实跑见交付说明。
- 是否触碰冻结清单：否。


## 2026-11-08 凭据口径更正执行记录（不触碰模型算法）

- 用户补充「需要注册的 5 个接口凭据都已保存在 git 仓库部署环境」。据此更正：`configured=0` 仅为本地沙盒未注入，**不得写成线上缺 key**；也不得反过来把「用户陈述 + 编排声明」包装成【已验证】。
- 【已验证·静态】读 main 上 workflow 名字：`daily.yml` / `source-probe.yml` / `collection-check.yml` / `publish-recheck.yml` 均声明 5 个 `${{ secrets.* }}`；`settle.yml` 声明 2 个、`telegram-preflight.yml` 1 个。`registered_wired=5`。**只核名字，不读值**。
- 【未验证】`GET /actions/secrets` 返回 403（PAT 无列 Secret 名单权限）；唯一 environment 为 `github-pages` 且无 environment secrets；Actions 日志与 artifact 下载被网络策略 403 拦截，读不到 CI stdout 的 `configured`。Secret 实际存在性、有效性、订阅与覆盖均未核验。
- `source_audit` 收紧为纯事实口径：新增 `declared_secrets()` / `workflow_declared_secrets` / `wired` / `registered_wired`；告警改 `REGISTERED_CREDENTIALS_NOT_INJECTED`，明确「当前进程未注入」，**不推断为 Secret 未配置、不推断「本次仅依赖免注册源」、不阻断采集**；`NO_REGISTERED_CREDENTIAL_IN_PROCESS` 亦不推断线上状态。NOTE 写明「编排声明 ≠ 密钥有效 / 数据充足」。
- 自证口径：下次 `daily.yml` 实跑后读 `data/source_dependency.json` 的 `summary.registered_configured == 5` 即运行时注入成功，全程不回显值；不足时按 `workflow_declared_secrets` 对照，不推断原因。
- 未改 `config/sources.yaml` 开关、任何 provider 客户端、核验口径/阈值/评分/展示/推送渲染；未新增需注册数据源；未触发任何 Telegram 推送。令牌按用户要求**保留**。
- 证据：本地 `pytest 913 passed, 1 skipped`（3.10）；`GROUNDING_OK`；10 个 workflow YAML 解析与并发/超时契约通过；真实 dry-run `registered_wired=5`。CI 3.12 见交付说明。
- 是否触碰冻结清单：否。


## 2026-11-08 第二轮升级执行记录（深度检查后的第一梯队，不触碰模型算法）

- 用户要求「一次性修复升级」；按上一轮深度检查报告的第一梯队全量落地，**第二梯队（需单独授权）一项未做**，令牌按要求**保留**。
- 改动文件：`src/ah/delivery.py`（A1 幂等）、`src/ah/__main__.py`（命令注册）、新增 `src/ah/date_consistency.py` / `data_quality.py` / `ops_failures.py`、`.github/workflows/daily.yml` / `review.yml`、`requirements.txt`、新增 `requirements-dev.txt` 与 `.github/workflows/lock.yml`、8 个 workflow 的 `cancel-in-progress`、`tests/conftest.py` + 5 个新测试 + `tests/fixtures/baseline/**`、README/AGENTS/docs 追加章节。
- **未改** `config/sources.yaml`、任何 provider 客户端、`analyze.py` / `policy.py` / `daily.py` / `settle.py` / `stats.py` / `review.py` / `config/settings.yaml` / `publish_telegram.py` 的评分与展示生成逻辑；未新增需注册数据源。
- 硬约束：A1 只看本 slot 记录；A2 分叉只告警、不绕过 `daily.py` 日期保护、不自动改日期；`data_quality` 纯只读、不写回台账/报告、缺字段写「未取得」、样本精度明确「不构成命中率或优势结论」、`verification_facts` 只抄录报告原文数字不解释成因；`ops_failures` 只记异常类型、不回显密钥或 URL、不推断、不阻断；`lock.yml` 失败只落事实，**绝不伪造版本或 hash**。
- 并发策略：`daily.yml` / `publish-recheck.yml` 保持 `cancel-in-progress: false`（涉及发送与 07:00 等待，取消会漏发），其余幂等/只读任务改 true，由 `tests/test_workflow_contracts.py` 锁定。
- 测试基线：`tests/fixtures/baseline/` 为 2026-11-08 main 上 `reports/` + `data/` 静态快照原样副本，`conftest.py::pytest_sessionstart` **只补齐缺失路径、不覆盖已有文件**，不生成任何赛程盘口水位比分。
- 证据：本地 `pytest 944 passed, 1 skipped`（3.10）；删掉 `data/`+`reports/` 的精简副本由 conftest 补齐 57 个文件后子集 65 passed、全量 944 passed；A1 复现返回 `skip/already_delivered_today`；`GROUNDING_OK`；12 个 workflow YAML 解析与并发/超时契约通过；真实 `data-quality` dry-run 数字见 `docs/upgrade-2026-11-08-round2.md`。CI 3.12 与 lock 实跑见交付说明。
- 真实数据事实（不推断）：上限 32 / 窗口 51 / 19 场未进候选；`final_line 2/24`、`line_open 0/24`、`price_ts 0/24`、快照可 join 22/24；picks 全 oddspapi+pinnacle 单源；报告写明 `verified:0<1 6场`、`stale>7200s 20场`；`decided=14`、Wilson 95% 宽度 0.416。
- 【未验证】lock 在 3.12 实跑结果、新增事实步骤的真实定时表现、Actions Secrets 有效性（`/actions/secrets` 403）、Telegram 接收端、结算积压成因、CLV/校准/线上规则回测。
- 是否触碰冻结清单：否。


<!-- ROUND3 DATA SOURCE -->
## 2026-11-08 第三轮 · DATA SOURCE 执行记录（用户授权）

- 用户明确授权「CLV 正式写回台账、第三比分源、注册源降级」等全部七项；本类单独提交，前缀 `DATA SOURCE:`。
- 改动：新增 `src/ah/clv.py` / `score_truth.py` / `source_policy.py`；`config/sources.yaml` **追加**
  `credential_policy` 与 `score_truth` 两个顶层声明块（未改任何既有 provider 块）；
  `settle.py` 新增 `CLV_FIELDS` 扩列、`audit/experiment_track.py` `EXTRA_FIELDS` 去重；
  `data/predictions.csv` 25→37 列、`data/experiment_picks.csv` 33→40 列；
  新增 `.github/workflows/close-snapshots.yml`；`daily.yml` / `settle.yml` / `source-probe.yml` 接线；
  新增 3 个测试模块 19 例。
- **口径对齐（重要）**：发现 `experiment_track.py` 早已定义价格型 `clv()` 与 `line_move()`；
  初版把线差写进 `clv` 属另立口径，已全部改回——收盘列名复用 `close_*`，
  `line_move` / `clv` 直接调用既有函数，不改其实现。
- 硬底线：缺收盘两侧水位就写「未取得」，**不拿线差冒充 CLV、不补造盘口**；
  第三比分源认不出格式返回空，`DISAGREE` 不自动改写结算，真值回填默认关闭；
  凭据策略只判 env var 是否注入，不读值、不判断有效性、不推断线上状态。
- 真实事实（不粉饰）：`line_open 22/24`、`price_ts 19/24`、`close_line 2/24`、
  `line_move 2/24`、**价格型 `clv` 0/24 全「未取得」**；3 个免注册比分源
  dry-run 全部 `NO_EXPLICIT_SCORE_FOUND`（AFC 页 `SiteUnavailable`）、claims=0；
  凭据策略本地 `active=0 / skipped=5 / gates=5`（沙盒未注入 Secrets，不代表线上缺 key）。
- 是否触碰模型算法框架：否（仅 schema 扩列与编排接线）。

