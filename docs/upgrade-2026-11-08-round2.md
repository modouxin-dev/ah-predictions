# 2026-11-08 第二轮升级：深度检查后的第一梯队全部落地

提交基线：main `a996aac4ecfe`（深度检查报告所依据的版本）
**性质**：编排层 / 事实层 / 测试层修复。**未触碰模型评分、阈值、盘口核验口径、展示生成、推送渲染**；未发送任何 Telegram；令牌按要求保留。

---

## 1. A1｜真 Bug：发送幂等被绕过（已修）

`src/ah/delivery.py::delivery_mode` 进入「本 slot 已送达」分支后用 `rows[-1]`（当日**全部**送达记录的末条）判断 0 场升级，没有按 slot 过滤。

**修复前可复现**：slot=1 已发 7 场（消息 547/548），台账末条是 slot=4 的 0 场兜底记录 → 重跑 slot=1 返回 `('primary','upgrade_zero_pick_report')`，会再发一遍完整日报。
**修复后**：只取本 slot 的最后一条 → `('skip','already_delivered_today')`。同时把「未知 slot」分支的跨时段 `rows[-1]` 判断一并移除，保守跳过。

回归：`tests/test_idempotency_fix.py` 8 例，含跨时段、同 slot 0 场升级、早间版不被吞、refresh、backfill 幂等。

## 2. A2｜日期口径分叉（事实化，不绕过保护）

编排层按 cron **目标时刻**解析 `AH_DATE`，`daily.py` 入口按**运行时刻**校验 `allowed`。极端延迟下两者可能分叉，此时 `daily.py` 的 `parser.error` 是「防误采历史数据」的**保护**，不应绕过。

新增 `src/ah/date_consistency.py`：只比对并落盘 `data/date_consistency.json`，分叉时输出 `DATE_DIVERGENCE` 告警并明确「保护性拒绝，不绕过、不自动改日期、不推断原因」。**不改 `daily.py` 一行**（采集层不在允许改动范围）。

## 3. B/C 级缺口 → 数据质量事实（只读旁路）

新增 `src/ah/data_quality.py`，`python -m ah data-quality` 输出 `data/data_quality.json`，一次覆盖：

| 段 | 覆盖原缺口 | 真实 11-08 实测 |
|---|---|---|
| `clv_field_coverage` | B1 CLV 字段没回填 | `line` 24/24、`line_open` 0/24、`price_ts` 0/24、`final_line` **2/24**、`final_odds` 2/24；快照可 join **22/24** |
| `candidate_coverage` | C1 初筛截断 | 上限 32、窗口 51、**19 场未进候选池** |
| `verification_facts` | B6 盘口单源/核验未过 | 报告原文抄录：`no_quotes 1场`、`no_shared_book 3场`、`sources:1<2 2场`、`stale>7200s 20场`、`verified:0<1 6场` |
| `source_concentration` | B6 单源 | 7 场 picks 全部 `oddspapi` / `pinnacle`，`distinct_sources=1` |
| `factor_gaps` | C2 伤停首发 | 56 个因子中 49 个 value 为空；7 场处于 observation 模式（不参与评分） |
| `zero_pick_reasons` | C4 0 场展示 | 只列 `collection_notes` / `collection_errors`，不推断 |
| `sample_precision` | B3 样本不足 | `decided=14`、点估计 0.25、**Wilson 95% 区间宽度 0.416**；明确「不构成命中率或优势结论」 |

接入 `daily.yml`（采集后）与 `review.yml`（复盘时），产物落盘。**不写回 `predictions.csv`、不改报告、不改复盘门槛 30 场**。

> 这组数字本身就是「数据采了没回填」的硬证据：快照能 join 22 场，但收盘/开盘字段几乎全空。正式把 `line_open/price_ts/close_*` 写回台账并计算 CLV，仍需单独 `DATA SOURCE:` 授权（见第 7 节）。

## 4. D 级工程项

| 项 | 改动 |
|---|---|
| D1 依赖不可复现 | `requirements.txt` 给 5 个 `>=` 加宽松上界；新增 `requirements-dev.txt`；新增 `.github/workflows/lock.yml`，**在真实 Python 3.12 runner 上**用 `pip-compile --generate-hashes` 生成 `requirements.lock`，并把生成结果（成功/失败、字节、sha256）写 `data/lock_generation.json`。**不在本地 3.10 凭空造版本或 hash** |
| D2 测试不自包含 | 新增 `tests/fixtures/baseline/`（57 个静态历史快照，main 原样副本）+ `tests/conftest.py::pytest_sessionstart` **只补齐缺失路径、不覆盖已有文件**。实测：删掉 `data/` 与 `reports/` 的精简副本，全套 **944 passed / 1 skipped** |
| D3/D5 静默降级 / 日志 403 | 新增 `src/ah/ops_failures.py`，`daily.yml` 的 collect / publish 加 `if: failure()` 步骤，把阶段、UTC 时刻、run_id、slot、异常类型（读不到写「未取得」）落 `data/ops_failures.json`。不回显密钥或 URL，不推断原因，不阻断 |
| D4 延迟排队堆积 | 8 个幂等/只读 workflow 改 `cancel-in-progress: true`（settle / source-probe / collection-check / telegram-preflight / telegram-group-check / research-backtest / review / tests；pages 原本已是 true）；**`daily.yml` 与 `publish-recheck.yml` 保持 false 并加注释**——涉及发送与 07:00 等待，取消在途 run 会漏发，靠 `(date,chat,slot)` 幂等防重复 |
| D6 时区分散 | 未擅自抽改 12+ 存量文件；由 `tests/test_workflow_contracts.py` 与文档记录，列为待单独评审 |
| D7 令牌 | **保留**，未注销、未轮换、未外传 |

回归：`tests/test_date_consistency.py`、`tests/test_data_quality.py`、`tests/test_ops_failures.py`、`tests/test_workflow_contracts.py`（含 YAML 全量解析、并发/超时契约、取消策略、新增步骤存在、lock 不伪造 hash）。

---

## 5. 验收证据【已验证】

- 本地 `pytest 944 passed, 1 skipped`（Python 3.10）
- **精简副本自包含**：删掉 `data/` + `reports/` 后由 conftest 补齐 57 个文件，子集 65 passed；全量 944 passed
- A1 复现场景修复后返回 `skip / already_delivered_today`
- `GROUNDING_OK date=2026-11-08 picks=7 watchlist=25 fixture_lines=20`
- 12 个 workflow YAML 全部解析通过，均有 `concurrency.group` 与 job `timeout-minutes`
- 真实数据 `data-quality` dry-run 数字见第 3 节
- **未触发任何 workflow dispatch、未发送 Telegram、未重复触发日报**

## 6. 【未验证】边界

- `requirements.lock` 是否生成成功（需在 3.12 runner 实跑 `lock.yml`；失败也只落事实，不伪造）
- 新增事实步骤在真实 `daily.yml` 定时班次里的表现
- Actions Secrets 5 个 key 的实际存在性与有效性（PAT 读 `/actions/secrets` 返回 403；日志/artifact 下载被网络策略 403 拦截）
- Telegram 接收端是否真正读到（台账只记 `sendMessage` 返回的 `message_id`）
- 结算积压那 1 场的成因；外部定时准时率；概率校准；CLV；线上规则独立回测

## 7. 本轮**没做**（需单独授权，不混在编排修复里）

按 AGENTS.md 冻结清单，`MODEL CHANGE` / `DATA SOURCE:` 必须单独提出、单独评审、单独提交：

1. 把 `line_open / price_ts / close_line / close_odds / clv` 正式写回 `predictions.csv` 并出 CLV 分桶 → `DATA SOURCE:`
2. 接第三比分真值源（免注册公开赛果）解决结算单点 → `DATA SOURCE:`
3. 线上规则档位的 walk-forward 回测、概率校准与可靠性曲线 → **`MODEL CHANGE`**
4. `render_observation()` 统一渲染、观察块与正式推荐的版式隔离 → 展示层单独评审
5. 源码内 22 处 `except Exception` 静默降级的逐处改造（涉及 provider 客户端）
6. 时区常量抽公共模块、改 12+ 存量文件
7. 真正把注册源降为可选增强、按 `credential_present` 动态跳过 provider

## 8. 边界声明

本轮未改 `config/sources.yaml` 开关、任何 provider 客户端、`analyze.py` / `policy.py` / `daily.py` / `settle.py` / `stats.py` / `review.py` / `config/settings.yaml` / `publish_telegram.py` 的评分与展示生成逻辑；未新增需注册数据源；未编造任何赛程、盘口、水位、比分、推荐、命中率或依赖 hash；未发送 Telegram。令牌保留。
