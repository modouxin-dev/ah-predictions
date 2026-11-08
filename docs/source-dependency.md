# 数据源依赖：少依赖「需要注册才能拿 key 的 API」

时间：2026-11-08

## 一、为什么要做这个

项目里 5 个赔率/赛程源都要先注册账号拿 key（API-Football、The Odds API、football-data、5dollar、OddsPapi）。
一旦 key 失效、额度用尽或 Secret 没注入，过去的编排是**静默**的：日志不会告诉你「今天其实只靠免注册源在跑」，
采集失败也只留一个笼统的 job failure。本轮把这件事变成**每次运行都看得见的事实**。

**先说清楚边界**：本轮**没有改** `config/sources.yaml` 的开关、没有改任何 provider 客户端、
没有改核验口径/阈值/评分、没有新增任何需要注册的数据源。真正把某个注册源替换成免注册主源，
属于 AGENTS.md 冻结清单，需要单独 `DATA SOURCE:` 授权 + 单独提交。本轮做的是编排/事实层的地基。

## 二、依赖分级（来自 `config/sources.yaml`，只读）

| 类别 | 源 | 需要注册吗 |
|---|---|---|
| 需注册（5） | `api_football`、`the_odds_api`、`football_data`、`fivedollar`、`oddspapi` | 是，要 key |
| 免注册（11） | `infersports`（keyless 档）、`titan007_live_fixtures` / `titan007_odds` / `titan007_1x2_history`、`uefa_fixtures`、`jleague_acle`、`jleague`、`afc`、`oddsportal`、`okooo`、`websearch_previews` | 否 |

> 「免注册」**不等于**已经接进采集主链路。这里只陈述配置事实，不宣称数据充足、不宣称已启用。

## 三、本轮落地的三件事

1. **`python -m ah source-audit`**（`src/ah/source_audit.py`）
   - 每次运行写 `data/source_dependency.json`：哪些源需注册、当前凭据是否**存在**（只记布尔，绝不回显值）、哪些免注册。
   - 缺 key → `::warning::REGISTERED_CREDENTIALS_NOT_INJECTED`，**只说「当前进程未注入」**，
     并标出其中几个是编排层已声明的；**不推断为 Secret 未配置、不推断「本次只靠免注册源」、不阻断采集**。
   - 同时静态解析 `.github/workflows/*.yml` 里 `${{ secrets.NAME }}` 的**名字**（不读值），
     写进 `workflow_declared_secrets`，一眼看出「编排接了、本进程没拿到」还是「压根没接线」。
   - `--probe-keyless` 对合规公开 URL 做一次低频轻量探测，只记 HTTP 状态 / 首块字节数 / 耗时 / 异常类型，
     **不解析成赛程、盘口、水位或比分**，不写报告。失败写「未取得」，不推断站点状态。
2. **`python -m ah delivery-gaps`**（`src/ah/delivery_gaps.py`）
   - 零注册依赖：每次运行自己核对当天四个目标时刻与 `data/telegram_delivery.json`。
   - 只报「已过目标时刻却无有效送达记录」的 slot；未到点不报；`picks_count=0` 或空 `message_ids` 不算送达。
   - 未到点却已有记录（历史凌晨误发场景）标 `DELIVERED_BEFORE_TARGET` 并给 info 告警，**不推断为误发或正常**。
   - 给建议补发 body 但**不自动触发**，避免误发与递归。
3. **接入点**：`daily.yml` 的 collect / publish 各跑一次并落盘；`source-probe.yml`（北京 09:00）也探一次免注册源。
   所有新增步骤都 `continue-on-error: true`，**不会因为没 key 或探测失败让日报发不出去**。

## 四、公开探测白名单为什么这么小

| 站点 | 处理 | 原因（来自 `sources.yaml` / `scrapers/probes.py`） |
|---|---|---|
| UEFA / J.League / AFC | 探 | robots 与条款已评估，低频读取允许 |
| titan007 | **不探** | 必须带 `?r=` 随机参数与 Referer，直接 GET 会被断开，探测只会误报 |
| oddsportal / okooo | **不探** | 配置标注 `partial`，结论未定 |
| spl.com.sa | **永不探** | 条款明确禁止自动化访问，配置 `enabled: false` |

## 五、缺 key 时到底会怎样

- 采集照常跑，**不会**因为当前进程没注入 key 就整轮失败（阻断会让下午补发也发不出去）。
- 日志有告警、仓库有 `source_dependency.json` 事实文件，谁都能事后核对。
- 采集若仍失败，publish 侧的 provenance 已会标 `report_source=main-checkout` + `collect_result=failure`（上一轮落地），
  两者结合能看出「这次是不是在数据不足的情况下发的」，**但不推断因果**。

## 六、真正「少依赖」的下一步（需单独授权，本轮未做）

按 AGENTS.md，以下都属冻结清单，必须单独 `DATA SOURCE:` 提交并写明依据与未回测风险：

1. 把 `infersports` keyless 档 / titan007 公开源**提为采集首选**，5 个注册源降为「有 key 才用的增强」。
2. 给每个源标注 `auth_class`，采集编排按 `credential_present` 动态跳过不可用的注册源（现在是 provider 内部静默处理）。
3. 定义「单场最少独立来源数」不足时的降级展示口径（涉及核验口径，需评审）。
4. 结算真值增加免注册的公开赛果源，避免 `predictions.csv` 单点。

**没拿到授权前，不要把这些写成「已经少依赖了」。**

## 七、凭据部署现状（2026-11-08 用户补充后更正）

**【用户陈述】**：5 个需注册接口的凭据都已保存在 GitHub 仓库的部署环境里。

**【已验证·静态】**（读 main 上的 workflow 文件，不读 Secret 值）：
`daily.yml`、`source-probe.yml`、`collection-check.yml`、`publish-recheck.yml` 四个任务都声明了
`API_FOOTBALL_KEY` / `ODDS_API_KEY` / `ODDSPAPI_KEY` / `FOOTBALL_DATA_KEY` / `FIVEDOLLAR_KEY`；
`settle.yml` 只声明结算需要的 2 个、`telegram-preflight.yml` 只声明 1 个，符合各自用途。
本地 dry-run 也复核出 `registered_wired=5`（5 个都在编排层已声明）。

**【未验证】**（不得写成「已验证」）：
- 所持 PAT 读 `/actions/secrets` 返回 **403（Resource not accessible by personal access token）**，
  没有列 Secret 名单的权限；`github-pages` 是唯一 environment，无 environment secrets。
  因此**无法代为核对 Secret 是否真实存在、名字是否完全一致**。
- 即便能列出名字，也**不代表** key 有效、订阅激活、额度充足或数据覆盖足够（AGENTS.md 防断言规则）。
- Actions 步骤日志与 artifact 下载在本会话仍被网络策略 403 拦截，读不到 CI stdout 里的
  `configured` 数字。

**怎么自证（不泄露任何值）**：等下一次 `daily.yml` 真实运行结束，看仓库里的
`data/source_dependency.json` → `summary.registered_configured`。
若等于 **5**，就是「5 个凭据在 CI 运行时确实注入成功」的运行时代理自检证据；
若小于 5，再按 `workflow_declared_secrets` 对照哪个没拿到，**不推断原因**。

> 结论口径：线上有 key → 采集照常优先使用这 5 个注册源；11 个免注册源是**兜底 +
> 可见性 + 零注册自愈**，不是替换。真正把注册源降为可选增强，仍需单独 `DATA SOURCE:` 授权。

## 八、仍未验证

- GitHub Actions Secrets 里 5 个 key 的实际存在性与有效性（见第七节 403 边界）。
- 免注册源是否返回足够的赛程/盘口覆盖（连通 ≠ 数据充足）。
- 采集实际用了哪个源（本审计不读运行日志、不推断）。
- Telegram 接收端；外部定时准时率；结算积压那 1 场的成因。
