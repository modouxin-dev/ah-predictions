# Telegram @LYZKYPbot 接管记录

接管时间：2026-11-04 20:30 左右（北京时间 / UTC+8）。由「先知」长轮询程序 `python -m ah tg-bot` 接管按钮与命令回复。
本文件不含任何 Token；所有 API 调用均使用环境变量 `TELEGRAM_BOT_TOKEN`。

## 接管前状态（快照）

```json
{
  "getMe": {
    "ok": true,
    "result": {
      "id": 8858788137,
      "is_bot": true,
      "first_name": "绿茵足球官方",
      "username": "LYZKYPbot",
      "can_join_groups": true,
      "can_read_all_group_messages": false,
      "supports_inline_queries": false,
      "supports_guest_queries": false,
      "can_connect_to_business": false,
      "has_main_web_app": false,
      "has_topics_enabled": false,
      "allows_users_to_create_topics": false,
      "can_manage_bots": false,
      "supports_join_request_queries": false
    }
  },
  "getWebhookInfo": {
    "ok": true,
    "result": {
      "url": "https://dimension-football-live-wangxtong.secretmelon37.chatgpt.site/telegram/webhook",
      "has_custom_certificate": false,
      "pending_update_count": 0,
      "max_connections": 1,
      "ip_address": "104.18.23.186",
      "allowed_updates": [
        "message",
        "callback_query"
      ]
    }
  },
  "getMyCommands": {
    "ok": true,
    "result": [
      {
        "command": "start",
        "description": "查看功能菜单"
      }
    ]
  },
  "getMyCommands:all_group_chats": {
    "ok": true,
    "result": []
  },
  "getMyCommands:all_private_chats": {
    "ok": true,
    "result": []
  },
  "getChatMenuButton": {
    "ok": true,
    "result": {
      "type": "commands"
    }
  }
}
```

旧服务（ChatGPT 托管应用，需登录）：https://dimension-football-live-wangxtong.secretmelon37.chatgpt.site/
旧 webhook：`https://dimension-football-live-wangxtong.secretmelon37.chatgpt.site/telegram/webhook`（allowed_updates: message, callback_query；max_connections 1）。
旧按钮（内联键盘，由旧服务生成）：今日赛事、优选门槛、历史回测、模型状态、赛后核账、来源诊断、发布排查。
旧命令：`start` — 查看功能菜单（默认 scope）；群组/私聊 scope 为空。菜单按钮类型：commands。

## 接管操作

1. `deleteWebhook`（`drop_pending_updates=false`，当时 pending_update_count = 0）。
2. 启动长轮询 `python -m ah tg-bot`（`getUpdates`，只读）。
3. `setMyCommands`：default / all_group_chats / all_private_chats 三个 scope，见 `src/ah/tg_bot.py` 中 `COMMANDS`。
4. 菜单按钮保持 `commands`。

## 如何停止 / 回滚

停止先知程序（含看门狗）：

```bash
cd /workspace/ah-predictions && scripts/tg_bot.sh stop
```

恢复旧服务（需在有 Token 的环境执行）：

```bash
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d url=https://dimension-football-live-wangxtong.secretmelon37.chatgpt.site/telegram/webhook \
  -d max_connections=1 -d 'allowed_updates=["message","callback_query"]'
# 恢复旧命令
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setMyCommands" -H 'Content-Type: application/json' \
  -d '{"commands":[{"command":"start","description":"查看功能菜单"}]}'
for s in all_group_chats all_private_chats; do
  curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/deleteMyCommands" -H 'Content-Type: application/json' -d "{\"scope\":{\"type\":\"$s\"}}"
done
```

注意：getWebhookInfo 不返回旧服务可能设置的 `secret_token`。若旧服务校验该头，需由旧服务在其后台重新调用 setWebhook 才能完全恢复。
设置 webhook 后长轮询会收到 409 冲突，因此必须先 `scripts/tg_bot.sh stop`。

## 接管后状态（2026-11-04 20:30 北京时间）

- getWebhookInfo：`url` 为空（pending_update_count 0），长轮询生效。
- 命令（default / all_group_chats / all_private_chats 三个 scope 相同）：
  `/start` 打开先知菜单、`/today` 今日推荐（高信心）、`/sources` 数据源状态、`/stats` 历史命中率 / 回测、
  `/records` 赛前确认 / 赛后结算、`/rules` 规则说明（命中率优先）、`/report` 最新报告与网站看板。`/menu`、`/help` 也会打开菜单（未列出）。
- 菜单按钮：commands。
- 内联键盘（`/start` 或「« 返回菜单」）：
  1. ⚽ 今日推荐 ｜ 📡 数据源状态
  2. 📈 历史命中率 ｜ 🔎 赛前确认 / 🏁 赛后结算
  3. 📜 规则说明 ｜ 📰 最新报告
  4. 🌐 完整看板（链接）
- 旧服务的按钮（旧消息上的内联键盘）点击后只会被静默应答，不再有内容。

## 常驻运行方式

- 盒子没有 systemd（PID 1 为 tini）也没有 cron，因此用 `scripts/tg_bot.sh`：
  `setsid nohup` 启动一个 bash 看门狗，看门狗运行 `python -m ah tg-bot`，进程退出 5 秒后自动重启。
  pid：`logs/tg_bot.supervisor.pid`、`logs/tg_bot.pid`；日志：`logs/tg_bot.log`（>5MB 轮转）；全部 gitignore。
- 盒子重启后：看门狗不会自启（无 init/cron，且 Token 只在代理会话的环境变量里）。
  先知的定时任务（日报 / 赛前确认 / 赛后回测）每次开始时执行 `scripts/tg_bot.sh ensure`（幂等：已运行则什么都不做）。
- 若旧服务重新设置 webhook（getUpdates 返回 409 webhook 冲突），程序会再次 deleteWebhook（最多每 10 分钟一次）。
- 只读：只读本地 reports/、data/、config/；最多每 5 分钟 `git pull --ff-only`（仅工作区干净时）；不发布、不提交、不调用付费数据 API。
