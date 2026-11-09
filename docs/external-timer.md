# 准点触发：零注册主路径 + 可选外部增强

## 结论先说

**不需要任何外部账号、不需要注册、不需要 API key，准点能力已经在仓库内。**
`punctual-dispatch.yml` 每 5 分钟跑一次，按北京目标时刻用 REST API 触发 `daily.yml`。
GitHub 自带那 4 条 `schedule` 保留作兜底，两边都受 `(date, chat_id, slot)` 台账幂等保护，
**不会重复推送**。

以前这页写「外部定时要你本人账号点 4 下」，现在那句话作废。

---

## 一、主路径（零注册）

`.github/workflows/punctual-dispatch.yml`：

- `schedule: "*/5 * * * *"`，`concurrency` 唯一组 + `cancel-in-progress: true`
- `permissions: contents: read, actions: write`，只用 runner 自带的 `GITHUB_TOKEN`
- 每 5 分钟由 `python -m ah punctual --apply` 做一次纯决策：

| 北京目标 | slot | 说明 |
|---|---|---|
| 06:00 | 1 | 提前采集；`daily.yml` 的 publish 自己等到 07:00 再推 |
| 11:00 | 2 | 更新 |
| 16:00 | 3 | 更新 |
| 22:00 | 4 | 更新 |

**三重防重复**

1. 台账已有 `(date, slot, status=sent, message_ids 非空)` → `already_delivered`，跳过
2. 最近 30 分钟已有 `daily.yml` 的 run（谁触发的都算）→ `recent_daily_run_exists`，跳过
3. workflow 自身并发组挡住同一次决策重入

**只在目标时刻起 60 分钟内动作。** 四个目标 +1h 都不跨北京午夜，所以
`workflow_dispatch` 按「当前北京日期」解析出的 `AH_DATE` 与目标日期天然一致，
不会重演 2026-11-08「延迟班次把日期滚到次日、吞掉早间版」的事故。

窗口外的漏发**不自动补**：仍由 `python -m ah delivery-gaps` 写 `data/delivery_gaps.json`
并输出 `::warning::`，人工按 slot 补发（幂等）。不假装全自愈，也不绕过 `daily.yml`
的日期一致性保护。

手动自检链路：

```
Actions → Punctual dispatch → Run workflow → verify=true
```

会真发一次 `slot=1` 的 dispatch 并核对是否新建 run。当天 slot 1 已送达时，
`publish` 会因幂等跳过，**不会重复发 Telegram**；但 `collect` 仍会跑一次并消耗一次
数据源额度，这点如实记录。

---

## 二、验收口径（务必照这个看）

1. `POST .../dispatches` 成功是 **204 且响应体为空**，不返回 run_id。
2. 去 GitHub Actions 看是否新建了 `daily-report` run，记下 run_id。
3. 再核对 `data/telegram_delivery.json` 出现对应 `date` / `slot` / `status=sent` /
   `message_ids` 非空 / `picks_count`。
4. **204 不等于送达**，送达证据只认台账里的 `message_id`。
5. `punctual` 的决策事实会作为 artifact `punctual-facts-<run_id>` 留存（日志下载若被
   网络策略拦截，以 GitHub 页面上的 workflow 运行为准）。

---

## 三、可选增强：cron-job.org（要账号，但已压成一条命令）

只有当你想要「站点侧执行历史 / 失败与恢复通知 / 状态页」时才需要。
官网确认支持自定义 HTTP 方法、请求头与 body、Test runs、失败通知、REST API，免费 。

### 3.1 一键建 4 个任务

两个密钥都只从环境变量读，**绝不进仓库、绝不回显**：

```bash
CRONJOB_API_KEY=你的cron-job.org_API_key \
GH_DISPATCH_PAT=仅授该仓库Actions:write的新PAT \
python3 scripts/setup_cronjob_org.py

# 先看脱敏后的 4 份 payload，不发任何请求：
python3 scripts/setup_cronjob_org.py --dry-run
```

脚本按官方 REST API 做（`https://api.cron-job.org`，`Authorization: Bearer <API key>`，
`GET /jobs` 列现有、`PUT /jobs` 创建、requestMethod `1=POST`） ：

- schedule `timezone=Asia/Shanghai`，hours `[6|11|16|22]`，minutes `[0]`
- headers：`Accept` / `Authorization: Bearer <PAT>` / `X-GitHub-Api-Version` /
  `Content-Type`
- body：`{"ref":"main","inputs":{"slot":"N"}}`（**必须显式带 slot**）
- `saveResponses=true` 让执行历史能看到 204 与时序；
  `notification.onFailure/onSuccess/onDisable=true`
- 按标题去重，重复运行不会建出 8 个任务
- 免费档每天 100 次 API 请求，本脚本一次约 9 次 

### 3.2 仍需你在 Console 点一下

官方 REST API 文档**没有**「Test run / 立即执行」的端点 ，所以脚本只能建任务、读
`GET /jobs/<id>/history`，**不能假装已经测试通过**。请在 Console 对每个任务点一次
Test run，看到 **204**，再去 GitHub Actions 核对 run_id 与台账。

### 3.3 别这么做

- 不要传 `slot=manual`（会强制完整重发）；不要省略 slot（缺省落 `manual`）
- 不要用 `slot=1` 去补 11/16/22 点的更新班次
- **不要再用那条明文贴进对话的旧 PAT**：换一条只授 `Actions: write` 的新令牌
- 不要把 PAT 写进 URL 或公开日志
- 不要删掉 GitHub 自带 4 条 schedule，它们是兜底

---

## 四、两个容易混的事

- **准点** ≠ **有没有比赛**。2026-11-09 的 0 场是窗口内没有配置联赛比赛（InferSports
  120 场全被联赛映射刷掉），跟定时准不准无关。
- **准点** ≠ **账号可用**。API-Football 账号 suspended 是另一件事，见
  `docs/zero-pick-2026-11-09.md` 与 `data/credential_verify.json`。

---

## 五、仍【未验证】

- `punctual-dispatch.yml` 的 5 分钟 cron 在 GitHub 高峰时段的实际抖动
- 真实准点班次（北京 11:00 / 16:00 / 22:00）跑完后的台账落地
- cron-job.org 的实际准时率、Test run 结果、失败通知是否到达（需要账号）
- Telegram 接收端
