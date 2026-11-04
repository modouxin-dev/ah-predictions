#!/usr/bin/env bash
# 先知 Telegram 按钮机器人守护脚本（长轮询，只读）。
#   scripts/tg_bot.sh start   启动看门狗（看门狗再启动 python -m ah tg-bot，挂掉 5 秒后自动重启）
#   scripts/tg_bot.sh stop    停止看门狗和机器人
#   scripts/tg_bot.sh status  查看状态
#   scripts/tg_bot.sh ensure  没在运行就启动（供定时任务/每次开机后调用，幂等）
# 日志/pid：logs/（已 gitignore）。Token 只从环境变量 TELEGRAM_BOT_TOKEN（或 ah-secrets.env）读取，绝不写入日志。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$ROOT/logs"; mkdir -p "$LOG_DIR"
SUP_PID="$LOG_DIR/tg_bot.supervisor.pid"
BOT_PID="$LOG_DIR/tg_bot.pid"
LOG="$LOG_DIR/tg_bot.log"
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY=python3

alive() { [ -f "$1" ] && kill -0 "$(cat "$1" 2>/dev/null)" 2>/dev/null; }

supervise() {
  echo $$ > "$SUP_PID"
  trap 'kill "$(cat "$BOT_PID" 2>/dev/null)" 2>/dev/null; rm -f "$BOT_PID" "$SUP_PID"; exit 0' TERM INT
  while true; do
    # 日志超过 5MB 轮转一次
    if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 5242880 ]; then mv -f "$LOG" "$LOG.1"; fi
    cd "$ROOT" || exit 1
    PYTHONPATH="$ROOT/src" "$PY" -u -m ah tg-bot >> "$LOG" 2>&1 &
    echo $! > "$BOT_PID"
    wait $!
    code=$?
    echo "$(TZ=Asia/Shanghai date '+%F %T%z') [watchdog] tg-bot exited ($code), restarting in 5s" >> "$LOG"
    sleep 5
  done
}

case "${1:-status}" in
  start|ensure)
    if alive "$SUP_PID"; then echo "running (watchdog pid $(cat "$SUP_PID"), bot pid $(cat "$BOT_PID" 2>/dev/null))"; exit 0; fi
    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] && ! grep -q '^TELEGRAM_BOT_TOKEN=' "${AH_SECRETS_FILE:-$HOME/.config/ah-secrets.env}" 2>/dev/null; then
      echo "TELEGRAM_BOT_TOKEN 未设置，无法启动" >&2; exit 1
    fi
    setsid nohup "$0" _supervise > /dev/null 2>&1 < /dev/null &
    sleep 2
    alive "$SUP_PID" && echo "started (watchdog pid $(cat "$SUP_PID"))" || { echo "start failed, see $LOG" >&2; exit 1; }
    ;;
  stop)
    if alive "$SUP_PID"; then kill "$(cat "$SUP_PID")"; fi
    sleep 1
    alive "$BOT_PID" && kill "$(cat "$BOT_PID")"
    pkill -f -- "-m ah tg-bot" 2>/dev/null   # 兜底：清理残留实例（避免 409 冲突）
    rm -f "$SUP_PID" "$BOT_PID"; echo "stopped"
    ;;
  status)
    if alive "$SUP_PID"; then echo "running (watchdog pid $(cat "$SUP_PID"), bot pid $(cat "$BOT_PID" 2>/dev/null))"; else echo "stopped"; fi
    tail -n 5 "$LOG" 2>/dev/null
    ;;
  _supervise) supervise ;;
  *) echo "usage: $0 start|stop|status|ensure"; exit 2 ;;
esac
