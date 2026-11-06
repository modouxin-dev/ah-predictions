# 防推测 / 防幻觉 / 防断言 操作规范

配套文件：`AGENTS.md`（强制指令）、`src/ah/grounding.py`（可执行闸门）、
`src/ah/delivery.py`（送达证据）、`scripts/check_grounding.py`（CI 入口）。

## 1. 为什么需要这套规范

历史问题（【已验证】，见审计报告）：

- `config/sources.yaml` 声明 `advisory_only: true`，但 `src/ah/daily.py` 把核验结果当硬拦截；
- `picks=[]` 且 `watchlist` 无 `observation` 时，推送正文只剩聚合计数，**一场对阵都不显示**；
- `python -m ah telegram` 在缺密钥时打印 `[skip]` 后 `exit 0`，CI 可能显示成功但群里没有消息；
- 幂等键用「报告文件是否存在」，报告在推送前已入库 → 后续定时运行静默跳过推送；
- `getMe`/`getChat` 预检通过被当成「能推送」，而真实 `sendMessage` 从未执行。

以上都不是模型算法问题，因此本次修复**不触碰模型框架**，只在编排层、展示层、对账层加约束。

## 2. 三条铁律

### 铁律一：只展示已存在的字段
`fixture_facts_lines()` 读取 `picks / watched / watchlist` 的
`league / home / away / kickoff_utc / withheld_reason`，缺一律写 `未取得`。
不生成盘口、水位、比分、倾向、评分、推荐理由。

### 铁律二：0 场不等于没有比赛
`picks=0` 时消息仍然列出**本次实际筛过的赛程事实清单**，并明确标注：
「仅列本次已采集字段；非推荐、不含预测结论」。
这解决「推送里没有比赛场次」，同时不制造任何预测。

### 铁律三：送达必须有证据
| 事件 | 能否写「已送达」 |
|---|---|
| Actions 步骤绿色 | 否 |
| `getMe` / `getChat` 通过 | 否 |
| `sendMessage` 返回 200 / `ok=true` / 非空 `message_id` | **是**，且必须写入台账 |
| 台账里没有记录 | 标 `【未验证】`，不得反推「没发」也不得反推「发了」 |

## 3. 闸门检查项（fail-closed）

`python -m ah grounding --report <path>` 校验：

1. 事实清单每行格式可解析；
2. 行内队名 / 联赛 / 开球时间必须能在报告 JSON 中逐字找到（防编造队名）；
3. 行内不得出现 `None` / `nan` / `null` 等未处理空值；
4. 不推原因不得含 `预计 / 应该是 / 大概率是 / 推测为 / 据经验 / 通常为`；
5. 推送文本声称的「推荐 N 场」必须等于 `policy.published(report)` 的实际条数；
6. 文本不得含 `已成功推送 / 推送成功 / 已送达 / 保证命中 / 稳赢` 等无证据断言；
7. 台账中 `status=sent` 必须同时具备 `message_ids` / `sent_at` / `chat_id`。

任一失败 → 退出码 1 → 工作流 `Publish Telegram` 步骤不执行。

## 4. 幂等键

唯一幂等键是 `data/telegram_delivery.json` 中的
`(date, report_sha, chat_id, status=sent, message_ids 非空)`。

- 报告 JSON 已存在 **但无送达记录** → 仍发送一次；
- 已有送达记录 → 跳过（防重复）；
- 报告内容变化（`report_sha` 变化，如 rescreen 覆盖）→ 视为新版本，允许再发一次，并在台账中留痕。

> 报告文件是否存在只决定**是否重新采集**，不再决定是否推送。

## 5. 目标会话一致性

`resolve_chat()` 同时读取 `TELEGRAM_CHAT_ID`（Secret）与 `config/settings.yaml` 的
`telegram.chat_id`：

- 两者都非空且不一致 → **立即报错**，拒绝猜测；
- 只有其一 → 使用该值，并在日志/台账中记录 `chat_source`；
- 都为空 → 报错退出。

`preflight` 只做 `getMe / getChat / getChatMember` 事实输出，并显式打印
`PREFLIGHT_DELIVERY_VERIFIED false`。

## 6. 本次修复的未验证项（不得写成已完成）

| 项 | 状态 | 原因 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` 的明文值 | 【未验证】 | 所持令牌读 `/actions/secrets` 返回 403 |
| 两者是否与 `settings.yaml` 一致 | 【未验证】 | 同上；由 `resolve_chat()` 在运行时自检 |
| 目标群是否真的收到消息 | 【未验证】 | 需 Telegram 端收件记录或 `message_id` 入账 |
| 定时任务是否被 GitHub 自动禁用、`02:00Z` 的实际启动延迟 | 【未验证】 | 需仓库管理员在 Actions 页面确认 |
| 所持令牌能否修改 `.github/workflows/**` | 实测后记录 | fine-grained PAT 的 Workflows 权限接口不可读 |

## 7. 本地复现

```bash
PYTHONPATH=src python -m ah grounding --report reports/2026/11/2026-11-06.json
PYTHONPATH=src python -m ah telegram 2026-11-06 --dry-run   # 查看纯文本，不发送
PYTHONPATH=src python -m ah delivery show                    # 查看送达台账
```
