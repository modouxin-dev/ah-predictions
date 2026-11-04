#!/usr/bin/env bash
# 多人共用仓库的安全提交/推送（4 个智能体 + 定时任务共用同一个 .git）。
#
# 用法：scripts/safe_commit.sh [选项] -m "提交说明" 路径1 [路径2 ...]
#
# 流程（全程持有文件锁，同一仓库的所有 worktree 共用一把锁）：
#   1. flock 加锁（默认 <git-common-dir>/ah-safe-commit.lock，可用 --lock-file / AH_LOCK_FILE 改；超时 --lock-timeout 秒）
#   2. 只暂存并只提交指定路径（git add -A -- 路径；git commit -- 路径），作者/提交者为 bot 身份；
#      别人已暂存或未提交的改动不会被带进这次提交
#   3. git pull --rebase --autostash <remote> <branch>（远端没有新提交时跳过；注意 autostash 会把别人
#      已暂存的改动恢复为未暂存，内容不丢）
#   4. 在「干净的临时 worktree（检出待推送的 HEAD）」里跑测试，不受别人未提交改动影响；
#      --skip-tests 跳过；--test-in-place 改为在当前目录跑
#   5. 用 GITHUB_AH_TOKEN 作为 http.extraheader 推送（经 GIT_CONFIG_* 环境变量传入，不出现在命令行/日志里）
#   6. 推送因竞争被拒 -> 重新 pull --rebase（上游有新提交则重跑测试）-> 指数退避重试，最多 --retries 次
#   测试失败 / rebase 冲突时：撤销本次提交（git reset --soft HEAD~1 + 取消暂存这些路径），改动留在工作区，不推送。
#
# 选项：
#   -m, --message MSG     提交说明（必填）
#   --dry-run             只显示将要提交的改动和步骤，不加提交、不 pull、不推送（仍会加锁）
#   --skip-tests          跳过测试（默认要跑）
#   --test-in-place       在当前工作目录跑测试（默认在干净的临时 worktree 里跑）
#   --test-cmd CMD        测试命令（默认 "<.venv>/bin/python -m pytest -q -p no:cacheprovider"；env AH_TEST_CMD）
#   --lock-timeout SEC    等锁超时秒数（默认 300；env AH_LOCK_TIMEOUT）
#   --lock-file PATH      锁文件（env AH_LOCK_FILE）
#   --retries N           推送最多重试次数（默认 4；env AH_PUSH_RETRIES）
#   --remote NAME         默认 origin
#   --branch NAME         默认当前分支
#   --no-push             只提交 + rebase + 测试，不推送
#   -h, --help
#
# 退出码：0 成功（或指定路径没有改动）；2 用法/仓库状态错误；3 等锁超时；4 pull --rebase 冲突（已撤销本次提交）；
#        5 测试失败（已撤销本次提交，未推送）；6 推送失败（重试用尽或鉴权失败，本地提交保留）；7 git add/commit 失败
#
# 密钥：GITHUB_AH_TOKEN 取自环境变量，否则从 ${AH_SECRETS_FILE:-$HOME/.config/ah-secrets.env} 读取那一行；绝不打印。
# 其他环境变量：AH_GIT_NAME / AH_GIT_EMAIL（作者），AH_BACKOFF_BASE（退避基数秒，默认 2）。
set -uo pipefail

MSG=""; DRY=0; SKIP_TESTS=0; IN_PLACE=0; NO_PUSH=0
TEST_CMD="${AH_TEST_CMD:-}"
LOCK_TIMEOUT="${AH_LOCK_TIMEOUT:-300}"
LOCK_FILE="${AH_LOCK_FILE:-}"
RETRIES="${AH_PUSH_RETRIES:-4}"
REMOTE="origin"; BRANCH=""
BACKOFF="${AH_BACKOFF_BASE:-2}"
NAME="${AH_GIT_NAME:-ah-predictions-bot}"
EMAIL="${AH_GIT_EMAIL:-modouxin-dev@users.noreply.github.com}"
PATHS=()

log() { printf '[safe_commit %s] %s\n' "$(TZ=Asia/Shanghai date '+%F %T')" "$*" >&2; }
die() { local c=$1; shift; log "错误：$*"; exit "$c"; }
usage() { sed -n '2,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    -m|--message) [ $# -ge 2 ] || die 2 "-m 需要参数"; MSG="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --skip-tests) SKIP_TESTS=1; shift ;;
    --test-in-place) IN_PLACE=1; shift ;;
    --test-cmd) [ $# -ge 2 ] || die 2 "--test-cmd 需要参数"; TEST_CMD="$2"; shift 2 ;;
    --lock-timeout) [ $# -ge 2 ] || die 2 "--lock-timeout 需要参数"; LOCK_TIMEOUT="$2"; shift 2 ;;
    --lock-file) [ $# -ge 2 ] || die 2 "--lock-file 需要参数"; LOCK_FILE="$2"; shift 2 ;;
    --retries) [ $# -ge 2 ] || die 2 "--retries 需要参数"; RETRIES="$2"; shift 2 ;;
    --remote) [ $# -ge 2 ] || die 2 "--remote 需要参数"; REMOTE="$2"; shift 2 ;;
    --branch) [ $# -ge 2 ] || die 2 "--branch 需要参数"; BRANCH="$2"; shift 2 ;;
    --no-push) NO_PUSH=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; PATHS+=("$@"); break ;;
    -*) die 2 "未知选项 $1（--help 查看用法）" ;;
    *) PATHS+=("$1"); shift ;;
  esac
done
[ -n "$MSG" ] || die 2 "缺少 -m 提交说明（--help 查看用法）"
[ ${#PATHS[@]} -gt 0 ] || die 2 "至少指定一个路径（不支持全量提交，避免带上别人的改动）"
[[ "$LOCK_TIMEOUT" =~ ^[0-9]+$ ]] || die 2 "--lock-timeout 必须是整数秒"
[[ "$RETRIES" =~ ^[0-9]+$ ]] || die 2 "--retries 必须是整数"
command -v flock >/dev/null || die 2 "需要 flock（util-linux）"

TOP="$(git rev-parse --show-toplevel 2>/dev/null)" || die 2 "当前目录不是 git 仓库"
cd "$TOP" || die 2 "无法进入 $TOP"
GIT_COMMON="$(cd "$(git rev-parse --git-common-dir)" && pwd)"
GIT_DIR_ABS="$(cd "$(git rev-parse --git-dir)" && pwd)"
CUR="$(git symbolic-ref --quiet --short HEAD)" || die 2 "当前是 detached HEAD，请先切到分支"
BRANCH="${BRANCH:-$CUR}"
[ "$BRANCH" = "$CUR" ] || die 2 "--branch $BRANCH 与当前分支 $CUR 不一致（本脚本不切换分支）"
LOCK_FILE="${LOCK_FILE:-$GIT_COMMON/ah-safe-commit.lock}"

if [ -z "$TEST_CMD" ]; then
  PY=""
  for c in "$TOP/.venv/bin/python" "$(dirname "$GIT_COMMON")/.venv/bin/python"; do [ -x "$c" ] && { PY="$c"; break; }; done
  PY="${PY:-python3}"
  TEST_CMD="$PY -m pytest -q -p no:cacheprovider"
fi

# ---- 鉴权（只经环境变量传给 git，不进 argv、不打印） ----
TOKEN="${GITHUB_AH_TOKEN:-}"
if [ -z "$TOKEN" ]; then
  SF="${AH_SECRETS_FILE:-$HOME/.config/ah-secrets.env}"
  [ -r "$SF" ] && TOKEN="$(sed -n 's/^\(export \)\{0,1\}GITHUB_AH_TOKEN=//p' "$SF" | tail -n1 | tr -d "\"'\r")"
fi
git_auth() {
  if [ -n "$TOKEN" ]; then
    GIT_TERMINAL_PROMPT=0 GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraheader \
      GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$TOKEN" | base64 -w0)" git "$@"
  else
    GIT_TERMINAL_PROMPT=0 git "$@"
  fi
}
redact() { if [ -n "$TOKEN" ]; then sed "s#${TOKEN//#/\\#}#***#g"; else cat; fi; }

# ---- 加锁 ----
exec 9>>"$LOCK_FILE" || die 2 "无法打开锁文件 $LOCK_FILE"
log "等待锁 $LOCK_FILE（最多 ${LOCK_TIMEOUT}s）"
flock -w "$LOCK_TIMEOUT" 9 || die 3 "等锁超时（${LOCK_TIMEOUT}s），可能有其他智能体正在提交"
log "已加锁"

for d in rebase-merge rebase-apply MERGE_HEAD CHERRY_PICK_HEAD; do
  [ -e "$GIT_DIR_ABS/$d" ] && die 2 "仓库正处于 $d 状态（可能有人正在操作），拒绝继续"
done

remote_has_branch() { git_auth ls-remote --exit-code --heads "$REMOTE" "$BRANCH" >/dev/null 2>&1; }

pull_rebase() {
  if ! remote_has_branch; then log "远端没有 $BRANCH 分支，跳过 pull"; return 0; fi
  git_auth fetch -q "$REMOTE" "$BRANCH" 2>&1 | redact >&2
  if git merge-base --is-ancestor FETCH_HEAD HEAD 2>/dev/null; then
    log "远端 $REMOTE/$BRANCH 没有新提交，无需 rebase"; return 0
  fi
  local out
  out="$(git_auth -c user.name="$NAME" -c user.email="$EMAIL" pull --rebase --autostash "$REMOTE" "$BRANCH" 2>&1)"
  local rc=$?
  printf '%s\n' "$out" | redact >&2
  if [ $rc -ne 0 ]; then
    [ -d "$GIT_DIR_ABS/rebase-merge" ] || [ -d "$GIT_DIR_ABS/rebase-apply" ] && git rebase --abort >/dev/null 2>&1
    return 1
  fi
  return 0
}

COMMITTED=0
undo_commit() {
  # 非破坏性撤销：提交退回暂存区，再把这些路径取消暂存；工作区内容不动
  [ $COMMITTED -eq 1 ] || return 0
  git reset -q --soft HEAD~1 && git restore --staged -- "${PATHS[@]}" 2>/dev/null
  log "已撤销本次本地提交，改动仍保留在工作区"
}

run_tests() {
  [ $SKIP_TESTS -eq 1 ] && { log "跳过测试（--skip-tests）"; return 0; }
  local rc
  if [ $IN_PLACE -eq 1 ]; then
    log "在当前目录跑测试：$TEST_CMD"
    (cd "$TOP" && PYTHONPATH="$TOP/src${PYTHONPATH:+:$PYTHONPATH}" bash -c "$TEST_CMD") >&2; rc=$?
  else
    local tmp; tmp="$(mktemp -d "${TMPDIR:-/tmp}/ah-safe-commit.XXXXXX")"
    git worktree add -q --detach "$tmp/wt" HEAD >/dev/null 2>&1 || { log "无法创建临时 worktree"; rm -rf "$tmp"; return 1; }
    log "在干净 worktree（HEAD $(git rev-parse --short HEAD)）跑测试：$TEST_CMD"
    (cd "$tmp/wt" && PYTHONPATH="$tmp/wt/src${PYTHONPATH:+:$PYTHONPATH}" bash -c "$TEST_CMD") >&2; rc=$?
    git worktree remove --force "$tmp/wt" >/dev/null 2>&1; rm -rf "$tmp"
  fi
  return $rc
}

# ---- 暂存 / 提交（只限指定路径） ----
if [ $DRY -eq 1 ]; then
  log "[dry-run] 分支 $BRANCH -> $REMOTE；锁 $LOCK_FILE；测试：$([ $SKIP_TESTS -eq 1 ] && echo 跳过 || echo "$TEST_CMD")"
  log "[dry-run] 将提交以下路径的改动："
  git status --short --untracked-files=all -- "${PATHS[@]}" >&2
  [ -n "$(git status --porcelain --untracked-files=all -- "${PATHS[@]}")" ] || log "[dry-run] 指定路径没有改动"
  log "[dry-run] 不会提交、pull 或推送"
  exit 0
fi

git add -A -- "${PATHS[@]}" || die 7 "git add 失败"
if git diff --cached --quiet -- "${PATHS[@]}"; then
  log "指定路径没有改动，无需提交"
  NOTHING=1
else
  NOTHING=0
  GIT_AUTHOR_NAME="$NAME" GIT_AUTHOR_EMAIL="$EMAIL" GIT_COMMITTER_NAME="$NAME" GIT_COMMITTER_EMAIL="$EMAIL" \
    git commit -q -m "$MSG" -- "${PATHS[@]}" || die 7 "git commit 失败"
  COMMITTED=1
  log "已提交 $(git rev-parse --short HEAD)：$MSG"
fi

if [ $NOTHING -eq 1 ]; then
  # 没有新提交：若本地也不领先远端则直接结束
  if ! remote_has_branch || [ -z "$(git_auth fetch -q "$REMOTE" "$BRANCH" 2>/dev/null; git rev-list "FETCH_HEAD..HEAD" 2>/dev/null)" ]; then
    exit 0
  fi
  log "本地有未推送的提交，继续推送流程"
fi

pull_rebase || { undo_commit; die 4 "pull --rebase 冲突，已 abort"; }
TESTED="$(git rev-parse HEAD)"
run_tests || { undo_commit; die 5 "测试失败，未推送"; }

[ $NO_PUSH -eq 1 ] && { log "--no-push：已提交 $(git rev-parse --short HEAD)，未推送"; echo "$(git rev-parse HEAD)"; exit 0; }

attempt=0
while :; do
  out="$(git_auth push "$REMOTE" "HEAD:refs/heads/$BRANCH" 2>&1)"; rc=$?
  printf '%s\n' "$out" | redact >&2
  if [ $rc -eq 0 ]; then
    log "推送成功：$(git rev-parse HEAD)"
    git rev-parse HEAD
    exit 0
  fi
  if printf '%s' "$out" | grep -qiE 'Authentication failed|could not read Username|Permission to .* denied|403|401|Repository not found'; then
    die 6 "推送鉴权失败（检查 GITHUB_AH_TOKEN），本地提交保留"
  fi
  attempt=$((attempt + 1))
  [ $attempt -le "$RETRIES" ] || die 6 "推送重试 $RETRIES 次仍失败，本地提交 $(git rev-parse --short HEAD) 保留"
  wait_s=$(( BACKOFF * (1 << (attempt - 1)) ))
  [ "$BACKOFF" -gt 0 ] && wait_s=$(( wait_s + RANDOM % (BACKOFF + 1) ))
  log "推送被拒（可能有并发提交），${wait_s}s 后重新 rebase 并重试（第 $attempt/$RETRIES 次）"
  sleep "$wait_s"
  pull_rebase || { undo_commit; die 4 "重试时 pull --rebase 冲突，已 abort"; }
  if [ "$(git rev-parse HEAD)" != "$TESTED" ]; then
    TESTED="$(git rev-parse HEAD)"
    run_tests || { undo_commit; die 5 "rebase 后测试失败，未推送"; }
  fi
done
