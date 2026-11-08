# ROUND 6：A 档一次性收敛（CLV join / 结构化上游 / 评估边界 / 联赛事实 / 告警）

日期：2026-11-08 · 类别：只读事实层 + 评估展示（DATA SOURCE / OPS / DISPLAY）
范围：`src/ah/clv.py`、`daily.py`、`funnel.py`、`build_site.py`、`__main__.py`、
新增 `src/ah/eval_facts.py`；`.github/workflows/close-snapshots.yml`、`review.yml`；
5 个测试文件；数据产物 `predictions.csv` / `clv_backfill.json` / `clv_join_facts.json` / `eval_facts.json`。
**未改**：评分、阈值、策略、盘口核验、采集源、推送渲染、Telegram。
**未触发日报、未发送任何消息、未编造赛程、盘口、水位、比分或倾向。**

用户授权：「按你的来」＝ 执行 A 档；B 档（放开 32 场上限 / 补数据源 / 引入概率输出）**先不动**；C 档等时间。

---

## 一、A1：817 行收盘快照为什么「采了没用」——真因与修复

### 真因（逐项实测，不是推测）

1. **时序**：`clv_backfill.json` 的 `checked_at` 是 `2026-11-08T12:29:23Z`，
   而 `close-snapshots.yml` 那次实跑落盘在约 `13:02 UTC`。回填时仓库里确实还没有 close 行，
   所以 `snapshot_close_matches=0`、`close_from_snapshot=0`。**不是 join 逻辑坏，是跑早了。**
2. **计数口径**：`close_capture.json` 写 `close_rows_in_repo=817`，是用
   `line.split(",")[2] == "close"` 数的。用 csv 模块解析后真实为
   **815 行有效 close**（11-07 510 + 11-08 307，其中 2 行无 `ah_home_line`），
   覆盖 **26 个唯一 match_id**，与台账 22 个唯一 mid **只交上 8 场**。
   「817 行」不等于「817 场可 join」，之前的表述容易误读，本轮已如实拆开。
3. **两个潜在计算 bug（本轮一并修掉）**：
   - 台账 `odds_format=hk`（十进制赔率 = 1 + 水位），收盘快照 `odds_format=decimal`，
     旧代码把快照水位直接喂给按 hk 解释的 `experiment_track.clv()`，一旦出现「收盘盘口=下注盘口」
     的场次就会算错去水概率。现按快照自身格式**机械归一**（decimal→hk 减 1），未知组合不混算。
   - `_align_odds_format` 返回 `0.0`（decimal 1.0 → hk 0.0）是合法值，
     旧写法 `or MISSING` 会把它误判成缺值；已改为显式 `None` 判断。
   - 台账自带的 `final_odds` 本来就是台账格式，不能被快照格式误减 1；已加来源判断。

### 修复后的真实结果（`data/clv_join_facts.json`）

| 项目 | 数值 |
|---|---|
| 仓库内可解析 close 行 | **815** |
| 覆盖唯一 match_id | **26** |
| 与台账 join 上的唯一场次 | **8** |
| `close_line` / `line_move` 有值 | **8 行** |
| `close_from_snapshot` | **6** |
| **价格型 CLV 有值** | **0** |

**价格型 CLV 仍是 0，是口径正确，不是失败。** 逐行原因（合计 24 行）：

- 7 行：收盘盘口 ≠ 下注盘口，按仓库既有 `clv()` 只在盘口相同时计算；线差单列 `line_move`，不当作 CLV
- 13 行：无收盘快照
- 3 行：有快照但 `learn_view` 未能唯一确定视角，不猜测
- 1 行：缺收盘两侧水位去水

`clv_basis` 现在逐行写明「为什么未取得」，而不是笼统写「缺数据」。
台账严格校验：相对 main 仅 6 个 CLV 回填列变化
（`close_line`/`close_odds`/`close_odds_opp`/`close_captured_at`/`line_move`/`clv_basis`），
**未触碰**赛程、盘口、赔率、比分、结算单位、状态、track 等任何权威字段。

### 配套

- `close-snapshots.yml` 的计数改用 `csv.DictReader` 解析 stage 列，
  输出 `close_rows_in_repo` / `close_rows_with_line` / `close_unique_match_ids`，
  note 明确「行数不等于可 join 场次」。
- `review.yml` 新增 `python -m ah clv --apply` 步骤：**先 join 再算复盘**，
  消除「回填跑在采集前面」的时序复发。

---

## 二、A2：上游数字结构化，不再靠正则抠文本

- `src/ah/daily.py` 生成报告时新增 3 个字段：
  `window_fixtures` / `screening_cap` / `outside_screen`。
  **只加字段**，不改 `not_covered` 文本、不改 picks、不改评分/核验/阈值。
- `src/ah/funnel.py`：`_upstream(report)` 优先读结构化字段（`source="report 结构化字段"`）；
  旧报告没有才降级到 `not_covered` 文本正则（`source="…文本正则（旧报告兼容）"`）。
  已写明但缺另一字段时**照实展示已写明的那个**，绝不拿 `settings.yaml` 的 32 倒推，
  也不在缺字段时推算「未进候选」。
- 网页按日漏斗表新增 `source` 可追溯来源。

---

## 三、A3：研究层 ≠ 线上层（显式标注，不调和）

新增 `data/eval_facts.json` 的 `model_scope` 段，把两边**已有字段逐字摆出来**：

```
rule_lab.model_under_test：线上规则档位（analyze.py / policy.py / config/settings.yaml），未改动任何一行
rule_lab.live_model_modified：False
rule_lab.joined_decided_rows：9
backtest.evaluation.status：INSUFFICIENT_SAMPLE
backtest 门槛：{"min_per_tier": 80, "min_total": 300}
rule_lab 样本状态：INSUFFICIENT_SAMPLE
```

并写明边界：**两边样本、输入与评估对象不同，任何一边的数字都不能迁移成另一边的准确率或 ROI 声明**；
线上发布仍是「规则档位，未经独立回测；不构成统计预测准确率」。
本轮**没有**统一两边输入（那属于 B 档模型层改动），只做了不可迁移标注。

---

## 四、A4：按联赛事实分桶

`eval_facts.league_facts` 把 `stats.json.by_league` 的已结算计数与
`selection_funnel.json` 的数据缺口计数并排：11 个联赛、**8 个有数据缺口**、
连续数据缺口 **3 天**（只计数，不称「漂移」、不推断数据源原因）。
样本 < 30 场一律 `hit_rate_conclusion: "未取得"` / `INSUFFICIENT_SAMPLE`，
**不输出联赛优劣、可预测性结论**。

---

## 五、A5：结构化告警（notify_only）

`eval_facts.alerts` 共 5 条：`PERSISTENT_DATA_GAP` / `UPSTREAM_SCREEN_CAP` /
`PRICE_CLV_NOT_QUOTED` / `MODEL_SCOPE_BOUNDARY` / `COLLECTION_SELF_REPORT`。
每条带 `code` / `severity` / `message` / `evidence` / `action`，**action 一律 `notify_only`**：
只落盘与展示，不自动补发、不递归触发、不发送 Telegram。
0 场日、连续缺口、上游截断、CLV 口径边界都在覆盖内。

---

## 六、网页

`review.html` 新增三块：评估边界卡（研究层≠线上层）、按联赛事实分桶表、结构化告警表，
以及「收盘快照 join 事实」块（815 / 26 / 8 三个 KPI + 逐行 CLV 状态分布）。
`funnel.html` 加指向该块的链接。`site/data/` 逐字导出
`clv_join_facts.json` 与 `eval_facts.json`。事实文件缺失时显示「暂无」，不白屏、不补造。

---

## 七、验证（本地，【已验证】）

- `pytest -q`：**1022 passed, 1 skipped**（净增 18 个测试）
- 新增测试覆盖：格式归一机械性 / 0.0 不被误判 / final_odds 不被误转 /
  close 快照 join 只写 line_move 不冒充 CLV / 同盘口才允许 clv / join 原因覆盖全部台账行 /
  结构化字段优先 / 旧文本正则兼容 / 部分字段不倒推 / 研究层不可迁移 /
  联赛样本不足不引用 / 告警 notify_only 且带 evidence / 缺文件优雅降级 /
  禁词 fail-closed / 原子写 / 网页两块渲染与降级 / 工作流契约
- `python -m ah clv --apply`、`python -m ah funnel`、`python -m ah eval-facts`、
  `python -m ah site` 全部实跑；所有 workflow YAML 解析通过

## 八、【未验证】边界 / 明确没做

- 收盘价是否即该机构最终收盘价、收盘快照是否覆盖全部已结算场次
- 未进候选池的具体场次（报告未逐场记录）、数据源为何缺近期状态（日志 403 读不到）
- `review.yml` 新增步骤的定时实跑链、Telegram 接收端、5 个 Secret 有效性
- **B 档没动**：`screening_max_fixtures=32` 未放开、未换/加数据源、未引入概率输出或校准、
  未改评分/阈值/策略/盘口核验；没有编造任何近期状态、伤停、首发
- **C 档等时间**：样本到 30 场、CLV 滚动稳定、今晚定时班、外部准点定时（要你账号）

## 九、令牌

按用户要求**保留、未注销、未轮换、未外传**。本轮不涉及任何凭据相关改动。
