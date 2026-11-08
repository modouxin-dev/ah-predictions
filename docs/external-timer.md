# 外部准点触发（可选增强）+ 零注册自愈

## 零注册优先：不建外部任务也能看见漏发

GitHub Actions 自带 `schedule` 会延迟数小时甚至丢失，但不一定要靠「需要注册账号的外部定时网站」.
本轮已在仓库内落地 `python -m ah delivery-gaps`：`daily.yml` 每次运行都会自己核对当天
四个目标时刻与 `data/telegram_delivery.json`，把「已过时刻却漏发的班次」写成
`data/delivery_gaps.json` 并输出 `::warning::`。**纯仓库内、零账号、零注册、不自动补发。**

再加上 `daily.yml` 自带的 4 条 schedule 互相兜底（早间失败时后续时段会补一次完整采集与推送），
即使完全不建下面的外部任务，系统也能跑，只是「准点率」不如外部定时。

> 所以：**外部定时是可选增强，不是必需依赖。** 不想注册账号，完全可以跳过下面这一节。

---

## 可选增强：cron-job.org（需要账号）

以下配置保留给希望「严格准点」的场景。**保留 GitHub 自带四条 schedule 作兜底，不要删。**

## 四个 job（时区 Asia/Shanghai）

| 北京时间 | body 的 slot | 含义 |
|---|---|---|
| 06:00 | 1 | 提前采集；publish 会等到北京 07:00 再发 |
| 11:00 | 2 | 更新 |
| 16:00 | 3 | 更新 |
| 22:00 | 4 | 更新 |

## 每个 job 的配置

- URL：`https://api.github.com/repos/modouxin-dev/ah-predictions/actions/workflows/daily.yml/dispatches`
- Method：`POST`
- Headers：
  - `Accept: application/vnd.github+json`
  - `Authorization: Bearer <PAT>`
  - `X-GitHub-Api-Version: 2022-11-28`
  - `Content-Type: application/json`
- Body：`{"ref":"main","inputs":{"slot":"N"}}`（N 取 1/2/3/4，**必须显式带 slot**，不要省略）
- 令牌：fine-grained PAT，仅授权该仓库 `Actions: write`（不需要 `Contents: write`）；不要用已经明文贴进对话的旧令牌

## 验收（很重要）

1. **成功返回 204 且响应体为空**，不返回 run_id。必须用 Test runs + Execution history 看 HTTP 状态与时序。
2. 再去 GitHub Actions 看是否新建了 `daily-report` run，并核对 `data/telegram_delivery.json` 是否出现对应 `date` / `slot` / `status=sent` / `message_ids` / `picks_count>0`。
3. 两边都命中同一 slot 时，台账 `(date, chat_id, slot)` 幂等会 skip 第二次，**不会重复推送**。
4. 开启失败通知（cron-job.org 支持 email / webhook / Slack / Telegram），204 之外的状态要能收到告警。

## 不要做的事

- 不要传 `slot=manual`：它会强制完整重发（兼容旧行为，补发请用 `AH_DELIVERY_MODE=backfill`）。
- 不要省略 slot：缺省会落 `manual`，不是 1。
- 不要用 `slot=1` 去补 11/16/22 点的更新班次。
- 不要把 PAT 写进 URL 或公开日志；响应体为空不代表「消息已送达」，送达证据只认台账里的 `message_id`。

## 不建外部任务时的核对方式

- 看 `data/delivery_gaps.json` 的 `missing_slots`；有缺口就按需用
  `POST .../daily.yml/dispatches` + `{"ref":"main","inputs":{"slot":"N"}}` 补发（幂等，不会重复推）。
- 看 `::warning::DELIVERY_GAP` 注解；它只报告、不自动触发，避免误发。
- 不需要任何第三方账号、API key 或注册。

## 仍【未验证】

外部 job 是否已创建、cron-job.org 实际准时率、失败通知是否生效、令牌轮换是否完成。
这些需要账号侧操作，仓库内无法代劳。
