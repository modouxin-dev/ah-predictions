# ROUND 5：选场漏斗 ——「为什么只推这几场 / 为什么 0 场」可诊断

日期：2026-11-08 · 类别：只读事实层 + 站点展示（OPS / DISPLAY）
范围：新增 `src/ah/funnel.py`、`tests/test_funnel.py`；改 `src/ah/__main__.py`、
`src/ah/build_site.py`、`.github/workflows/review.yml`、`tests/test_workflow_contracts.py`、`README.md`。
**未改**：评分 / 阈值 / 策略 / 盘口核验 / 采集源 / 推送渲染 / Telegram。
**未触发日报、未发送任何消息、未编造赛程、盘口、比分或倾向。**

---

## 一、用户提出的问题

「提高门槛就会导致一场比赛推不出，到底哪里出了问题？」

核查发现：淘汰原因其实**一直写在报告里**，只是散在三处、跨天看不出瓶颈：

- `watchlist[].withheld_category`（逐场淘汰类别）
- `not_covered`（上游筛查上限 + 数据源状态原文）
- `collection_notes`（近期状态不足、盘口核验明细）

而且 `screened` 字段在新报告里是空的，等于「窗口里有多少场、多少场没进候选」这一层没被结构化记账。

---

## 二、真实诊断（5 份报告，逐字可回溯，【已验证】）

| 层级 | 实测 |
|---|---|
| 上游截断 | 窗口 64 / 79 / 51 场，筛查上限 32 → **32 / 47 / 19 场没进候选**（11-06/07/08） |
| 近期状态数据不足 | 连续 3 天 **9 / 10 / 10 场**；按联赛累计 日职乙 11、日职联 9、韩K联 4、澳超 4、卡塔尔联 1 等 |
| 盘口核验未通过 | 2 / 1 / 7 场；11-08 另有 `stale>7200s 20场`、`verified:0<1 6场`、`no_shared_book 3场` |
| 模型判断（评分不足 / 深盘） | 16 / 13 / 7 场 |
| 伤停 / 首发 | 报告原文自述「尚未自动采集伤停或首发」 |

报告自述的根因文本（逐字）：`API-Football 未启用`、`API-Football 与 InferSports 赛果库均不足 3 场`、
`OddsPapi 赛程 acl_elite: 未取得赛程（HTTP 404）`。

**结论**：当前最大瓶颈是**上游筛查上限 + 近期状态数据覆盖**，不是发布门槛。
把门槛调低救不了「数据没采到」的场次，反而会把缺数据的比赛硬推出去。
因此本轮选择「先补可观测性，不动阈值」——阈值本身标注「规则档位，未经独立回测」，
14 场样本调阈值纯属拟合噪音。

---

## 三、做了什么

### 1. `src/ah/funnel.py`（纯只读，全标准库）

- 遍历 `reports/*/*/*.json`（排除 rechecks），按日输出事实行
- **类别白名单**：把真实出现过的类别分成
  `data_gap / verification / model_judgment / timing / unknown`；
  复合类别（如「深盘+机构分歧」）和陌生类别**单列 unknown，不猜测归属**；
  旧版报告（11-05 及更早）类别名逐字收录，不改原文
- **上游数字只从 `not_covered` 文本严格正则取**「筛查上限 N 场」「窗口内共发现 M 场」；
  缺一处整组写「未取得」，**不拿 `settings.yaml` 的 32 倒推**
- `collection_notes` / `not_covered` 原文逐字保留，可折叠下钻
- **0 场日照常输出一行**，并生成「0 场推荐日」事实句
- 跨天只做计数与事实句（每条带【已验证】依据）；生成后 fail-closed 自检，
  出现「准确率提升 / 模型更准 / ROI 改善」等断言直接抛错、不落盘
- 原子写 `data/selection_funnel.json`（临时文件 + `os.replace`）

### 2. 网页 `funnel.html`

- 复盘页新增显眼入口卡片；主导航保持 7 项不挤
- 页面内容：4 个 KPI、按日漏斗表（发布 / 观察 / 四类淘汰 / 窗口总数 / 筛查上限 / 未进候选 / 采集错误）、
  跨天事实、数据缺口按联赛、逐日下钻（类别 + 核验明细 + 采集原文）、【未验证】清单
- `site/data/selection_funnel.json` 逐字导出
- **定时班次还没生成文件时，站点即时从本次 reports 自算**，绝不白屏、绝不补造

### 3. 自动化

- `review.yml` 新增 `python -m ah funnel --out data/selection_funnel.json`（facts only），
  并把该文件纳入 `scripts/safe_commit.sh` 提交路径
- `pages.yml` 的 `workflow_run` 已含 `accuracy-review`，事实落盘后自动重建网页；
  加上站点自算兜底，双保险

---

## 四、验证（本地，【已验证】）

- `pytest -q`：**1004 passed, 1 skipped**（净增 10 个测试）
- 新增测试覆盖：白名单分类 / 复合类别不猜 / 0 场日出表 / 上游缺字段不倒推 /
  核验明细逐字 / 连续 3 天事实句 / 禁词 fail-closed / 原子写 / 站点自算兜底 / 落盘优先 / 工作流契约
- `python -m ah funnel` 实跑 5 份真实报告：
  `days=5, total_picks=22, data_gap=32, verification=10, model_judgment=51, timing=2, unknown=1, max_outside_screen=47`
- `python -m ah site` 实跑生成 `funnel.html` + 复盘入口 + 导出 JSON，页面不含 `25.0%` 或夸大断言
- 所有 workflow YAML 解析通过

## 五、【未验证】/ 明确没做的

- 未进候选池的**具体场次**报告未逐场记录，本模块不推断
- 数据源为何缺近期状态，需各自运行日志（日志下载仍受 403 限制），不猜
- **没有**提高 `screening_max_fixtures`：会影响采集额度与候选结果，需单独授权 + 实测验证
- **没有**替换或新增数据采集源、没有编造近期状态 / 伤停 / 首发
- **没有**动 `confidence_thresholds`、`policy`、评分权重、`analyze.py`
- 线上 Pages 部署、`review.yml` 新步骤的实跑、Telegram 接收端、5 个 Secret 有效性、CLV / 校准结论

## 六、下一步建议（需你单独授权才动）

1. 补近期状态数据覆盖：优先日职乙 / 日职联 / 韩K联 / 澳超；先验证免注册源可用性，再谈注册源
2. 评估把 `screening_max_fixtures` 从 32 提高（须同时核对额度、限速与候选质量，不能只改数字）
3. 让报告结构化写出 `screened` / `window_fixtures`，替代从 `not_covered` 文本正则提取
4. 样本到 30 场后才谈命中率；CLV 需收盘快照 join 回台账

## 七、令牌

按用户要求**保留、未注销、未轮换、未外传**。本轮不涉及任何凭据相关改动。
