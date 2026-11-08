# ROUND 4：复盘/回测同步网页 + 命中率结论口径统一

日期：2026-11-08 · 类别：站点展示层（DISPLAY / OPS）
范围：只改 `src/ah/build_site.py`、`.github/workflows/pages.yml` 与两个测试文件。
**未改** `stats.py` / `publish_telegram.py` / 评分 / 阈值 / 策略 / 采集 / 推送；**未触发日报、未发送 Telegram**。

---

## 一、为什么改

只读核查发现：复盘产物其实**每天都有存档**（`docs/review/*.md`、`data/accuracy_review.json`、
`data/rule_lab.json`、`data/backtest_report.*`、`data/sample_gaps.json` 等都在 main），
但 GitHub Pages 站点里一处都没引用——导航只有「今日 / 推荐记录 / 战绩 / 数据源 / 规则 / 实验场」，
没有复盘入口，复盘内容没上网页。

同时查到一处**口径冲突**：线上首页 / 战绩页写「25.0% 总体命中率（14 场）」，
而同一天的日复盘文档写「INSUFFICIENT_SAMPLE（14 < 30 场）→ 命中率 未取得，不构成统计预测准确率」。
原因是前端只要有 `decided` 就显示百分比，没接复盘层的 30 场门槛。

---

## 二、四项改动

### 1. 新增「复盘」页（`review.html`）

- 导航新增「复盘」入口；`docs/review/*.md` 逐篇渲染成 `review/<stem>.html` 子页
- 轻量 Markdown 渲染器（标题 / 表格 / 列表 / 引用 / 段落 / 粗体 / 代码），**只做结构转换，不改一个字**
- 主页展示：最新复盘快照（日期 / 生成时间 / run_id / 对账状态 / 未结算 / 赛果未取得 / 告警）、
  样本状态、命中率结论（不足门槛写「未取得」）
- 研究/回测事实卡：6 个文件逐一列出状态与生成时间，`<details>` 里附**逐字原始 JSON**
- 文件缺失一律写「暂无」，不推断、不补造

### 2. 命中率结论门槛与复盘层统一

- 从 `review.py` 直接复用 `MIN_SAMPLE=30` / `MIN_BUCKET_SAMPLE=10` / `MISSING="未取得"`，不另立一套常量
- `site_can_quote(s)`：**总体不足 30 场时，窗口 / 分桶 / ROI 结论也一律不输出**（与 review.py 的 `can_quote` 一致）
- 影响面：`summary_bar`、`stats_brief`、`stats_html`（KPI + 各分组表）、`chart_data`（滚动命中率点置 null，
  前端 `app.js` 原生支持断点）、`site_summary_line`
- 站点新增自己的 `site_summary_line`；`stats.py::summary_line` **保持原样**，Telegram 推送文本不受影响（有单测锁住）
- 累计盈亏曲线保留，但明确标注「过程记录，不是准确率结论」

### 3. 事实数据导出到 `site/data/`

`accuracy_review.json` / `rule_lab.json` / `backtest_report.json` / `sample_gaps.json` /
`feature_readiness.json` / `clv_backfill.json` 逐字导出，随 Pages 发布，外部可直接取用。
文件不存在则不导出、页面写「暂无」。

### 4. Pages 触发补齐

`pages.yml`：
- `push.paths` 增加 `docs/review/**`
- `workflow_run.workflows` 由 `[daily-report, settle-and-stats]` 扩成
  `[daily-report, settle-and-stats, accuracy-review, research-backtest]`
  —— 复盘/回测产物写回 main 后立即重建站点，不再只靠 `data/**` 的 push 兜底

---

## 三、验证（本地，【已验证】）

- `pytest -q`：**994 passed, 1 skipped**（原 988 + 6 个新测试）
- 新增测试覆盖：
  - 小样本首页/战绩页不出现 `25.0%` / `50.0%`，出现「未取得」
  - 30 场样本充足时才显示 `100.0%`
  - Telegram 的 `summary_line` 文本保持不变（防改动外溢）
  - 复盘页生成、子页渲染、JSON 逐字导出、不编造结论数字
  - 复盘产物缺失时优雅降级
  - `pages.yml` 契约：4 个 workflow_run + `docs/review/**` 路径
- 本地 `python -m ah site` 实跑：`review.html` + 2 个复盘子页生成；
  首页/战绩页的命中率结论百分比已全部清空，仅剩模拟单里的「模型概率」原始字段（非结论）

## 四、【未验证】边界

- 线上 Pages 部署是否成功、**线上复盘页是否读到了 main 上的 6 个事实文件**（本地沙盒副本缺
  `accuracy_review.json` / `rule_lab.json`，故本地预览显示「暂无」；这两个文件已在 main 上确认存在）
- `accuracy-review` / `research-backtest` 的 `workflow_run` 实际触发链（GitHub 要求 workflow 同名且在默认分支）
- Pages 站点 VIP 前端行为、外部访问（沙盒直连 Pages 返回 403，是出网限制，非站点故障）
- Telegram 接收端、5 个 Secret 有效性、收盘线采集结果、CLV/校准结论 —— 均沿用前几轮的未验证边界

## 五、令牌

按用户要求**保留、未注销、未轮换、未外传**。本轮没有改动任何凭据相关代码。
