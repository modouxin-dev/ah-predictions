#!/usr/bin/env bash
# 把运行时数据源状态（var/source_status.json，gitignore）发布为入库快照 data/source_status.json，并经 safe_commit 提交推送。
# 只在快照内容确实变化时提交。建议由日报定时任务每天调用一次（例如日报提交前后），而不是每次请求都提交。
#   scripts/publish_source_status.sh [--dry-run] [--skip-tests]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${AH_SOURCE_STATUS_FILE:-$ROOT/var/source_status.json}"
DST="$ROOT/data/source_status.json"
[ -f "$SRC" ] || { echo "没有运行时状态 $SRC，跳过" >&2; exit 0; }
if cmp -s "$SRC" "$DST"; then echo "快照已是最新，跳过" >&2; exit 0; fi
for a in "$@"; do [ "$a" = "--dry-run" ] && { echo "[dry-run] 将把 $SRC 复制到 $DST 并提交" >&2; exec "$ROOT/scripts/safe_commit.sh" "$@" -m "source status snapshot" data/source_status.json; }; done
cp "$SRC" "$DST.tmp" && mv "$DST.tmp" "$DST"
exec "$ROOT/scripts/safe_commit.sh" "$@" -m "source status snapshot $(TZ=Asia/Shanghai date '+%F %H:%M') 北京时间" data/source_status.json
