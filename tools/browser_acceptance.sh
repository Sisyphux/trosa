#!/usr/bin/env bash
# Run the Trosa core workflow in a real headless Chromium browser.
#
# This is intentionally separate from jsdom/DOM regression tests. It starts the
# isolated PostgreSQL rehearsal service, launches a fresh Playwright Chromium
# (pinned by browser-extension/package-lock.json -- never a developer's desktop
# browser and never a shared browser task), and fails when the browser capability
# is unavailable instead of silently marking acceptance as skipped.
#
# Concurrency: every run gets a unique RUN_ID (and a RUN_TAG baked into the
# records it creates), and only the browser phase is wrapped in a machine-wide
# lock from deploy/cloud/lib-release-lock.sh. Two gates on one machine therefore
# serialize the short browser phase instead of racing for the same browser/ports.
#
# Exit codes are meaningful to the release gate:
#   0   pass
#   20  product assertion (release-test.sh never retries this)
#   21  infrastructure fault (release-test.sh retries once, records a flake event)
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# 机器级锁原语：只在浏览器阶段持锁；sourcing 无副作用。
# shellcheck source=../deploy/cloud/lib-release-lock.sh
source "$ROOT/deploy/cloud/lib-release-lock.sh"
PYTHON_BIN="${TROSA_PYTHON:-$ROOT/.venv/bin/python}"
PORT="${TROSA_BROWSER_ACCEPTANCE_PORT:-}"
REHEARSAL_PORT="${TROSA_BROWSER_REHEARSAL_PORT:-}"
REUSE_REHEARSAL="${TROSA_BROWSER_ACCEPTANCE_REUSE_REHEARSAL:-0}"
# 每次验收独占一次运行：RUN_ID 是本次运行的唯一标识（等价于旧的浏览器任务/请求号），
# RUN_TAG 是写进验收记录的可见唯一标记，搜索断言据此恰好命中一条。
RUN_ID="${TROSA_BROWSER_ACCEPTANCE_RUN_ID:-core-$(date -u +%Y%m%dT%H%M%S)-$$-$RANDOM}"
RUN_TAG="${TROSA_BROWSER_ACCEPTANCE_RUN_TAG:-BAR$(date -u +%H%M%S)$(printf '%04X' "$RANDOM")}"
STOP_REHEARSAL_ON_EXIT=1
SERVICE_LOG=""
SERVICE_PID=""
LOCK_HELD=0

fail() {
  printf 'browser acceptance: %s\n' "$*" >&2
  exit 1
}

# 基础设施故障：退出码 21，交回门禁按规则重跑一次并记 flake。
fail_infra() {
  printf 'browser acceptance: %s\n' "$*" >&2
  exit 21
}

# 锁与产物目录落在共享 git 目录下：同机所有 worktree 共用同一个锁与产物目录。
GIT_COMMON_DIR="$(git -C "$ROOT" rev-parse --git-common-dir 2>/dev/null || true)"
if [[ -z "$GIT_COMMON_DIR" ]]; then
  GIT_COMMON_DIR="$ROOT/.git"
fi
case "$GIT_COMMON_DIR" in
  /*) ;;
  *) GIT_COMMON_DIR="$ROOT/$GIT_COMMON_DIR" ;;
esac
LOCK_DIR="${TROSA_BROWSER_ACCEPTANCE_LOCK_DIR:-$GIT_COMMON_DIR/trosa-tasks/browser-acceptance.lock}"
ARTIFACT_DIR="${TROSA_BROWSER_ACCEPTANCE_ARTIFACTS:-$GIT_COMMON_DIR/trosa-tasks/browser-artifacts}"
export TROSA_BROWSER_ACCEPTANCE_ARTIFACTS="$ARTIFACT_DIR"
export TROSA_BROWSER_ACCEPTANCE_RUN_ID="$RUN_ID"
export TROSA_BROWSER_ACCEPTANCE_RUN_TAG="$RUN_TAG"

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ "$LOCK_HELD" == 1 && -n "$LOCK_DIR" ]]; then
    trosa_lock_release "$LOCK_DIR"
    LOCK_HELD=0
  fi
  if [[ -n "$SERVICE_PID" ]] && kill -0 "$SERVICE_PID" 2>/dev/null; then
    kill "$SERVICE_PID" 2>/dev/null || true
    wait "$SERVICE_PID" 2>/dev/null || true
  fi
  if [[ -n "$SERVICE_LOG" && -f "$SERVICE_LOG" ]]; then
    rm -f -- "$SERVICE_LOG"
  fi
  # 复用模式下 PostgreSQL 由门禁拥有并统一回收，这里不动它。
  if [[ "$STOP_REHEARSAL_ON_EXIT" == 1 ]]; then
    "$PYTHON_BIN" "$ROOT/tools/postgres_rehearsal.py" stop >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

[[ -x "$PYTHON_BIN" ]] || fail_infra "找不到项目 Python：$PYTHON_BIN"
command -v node >/dev/null 2>&1 || fail_infra '找不到 node，无法启动锁定的 Playwright Chromium'
# 缺失浏览器依赖一律硬失败，绝不静默标记为 SKIP；这是基础设施故障，门禁可重跑一次。
[[ -f "$ROOT/browser-extension/node_modules/playwright/package.json" ]] \
  || fail_infra "找不到锁定的 Playwright（$ROOT/browser-extension/node_modules/playwright）；请在 browser-extension 执行 npm ci；不会把浏览器验收标记为 SKIP"
# 启动前回收上一次中断遗留的孤儿演练服务，避免端口/CPU 逐日累积。
# 只清理父进程已消失的进程；并发运行的其它门禁服务仍持有活父进程，不会被触碰。
if [[ -f "$ROOT/tools/rehearsal_hygiene.py" ]]; then
  "$PYTHON_BIN" "$ROOT/tools/rehearsal_hygiene.py" clean --apply --orphans-only \
    --min-process-age 30 >/dev/null 2>&1 || true
fi
if [[ -z "$PORT" ]]; then
  PORT="$("$PYTHON_BIN" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
fi
if [[ -z "$REHEARSAL_PORT" ]]; then
  REHEARSAL_PORT="$("$PYTHON_BIN" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
fi
export TROSA_REHEARSAL_PORT="$REHEARSAL_PORT"

if [[ "$REUSE_REHEARSAL" == 1 ]]; then
  # 门禁（release-test.sh）已在同一 TROSA_REHEARSAL_PORT 上启动并迁移了 rehearsal
  # 服务，环境变量也已通过 shell 传入。这里只重载确定性 fixture，不重启服务：
  # postgres_rehearsal.py test 里的集成测试会改动 rehearsal 数据，浏览器验收需要
  # 一份确定的起始数据，所以必须重建 fixture；但服务/连接刻意复用。
  [[ -n "${TRADE_OS_DATABASE_URL:-}" ]] \
    || fail_infra '复用模式需要门禁先准备 rehearsal 环境（TRADE_OS_DATABASE_URL 为空）'
  CRM_ENV=rehearsal "$PYTHON_BIN" "$ROOT/tools/postgres_rehearsal.py" fixture >/dev/null
  STOP_REHEARSAL_ON_EXIT=0
else
  eval "$("$PYTHON_BIN" "$ROOT/tools/postgres_rehearsal.py" env)"
  CRM_ENV=rehearsal "$PYTHON_BIN" "$ROOT/tools/postgres_rehearsal.py" fixture >/dev/null
fi

SERVICE_LOG="$(mktemp "${TMPDIR:-/tmp}/trosa-browser-acceptance.XXXXXX")"
# ``exec`` replaces the subshell with the Python server, so ``SERVICE_PID`` is
# the real process.  Without it the subshell forks the server as a child, and
# killing ``SERVICE_PID`` orphaned the server instead of stopping it -- that is
# how dozens of ``serve_rehearsal.py`` processes leaked onto developer machines
# (see tools/rehearsal_hygiene.py).
(
  cd "$ROOT"
  exec env \
    CRM_ENV=rehearsal \
    TROSA_REHEARSAL=1 \
    TRADE_OS_DATA_BACKEND=postgres \
    TRADE_OS_DATABASE_URL="$TRADE_OS_DATABASE_URL" \
    TROSA_REHEARSAL_DATABASE_URL="$TROSA_REHEARSAL_DATABASE_URL" \
    TROSA_REHEARSAL_PORT="$TROSA_REHEARSAL_PORT" \
    TROSA_REHEARSAL_DB="$TROSA_REHEARSAL_DB" \
    CRM_BIND_HOST=127.0.0.1 \
    CRM_PORT="$PORT" \
    "$PYTHON_BIN" "$ROOT/serve_rehearsal.py" >"$SERVICE_LOG" 2>&1
) &
SERVICE_PID=$!

ready=0
for _ in $(seq 1 80); do
  if curl --fail --silent --show-error --max-time 2 "http://127.0.0.1:$PORT/api/network/ping" >/dev/null 2>&1; then
    ready=1
    break
  fi
  if ! kill -0 "$SERVICE_PID" 2>/dev/null; then
    break
  fi
  sleep 0.25
done
[[ "$ready" == 1 ]] || { sed -n '1,240p' "$SERVICE_LOG" >&2; fail_infra "rehearsal web service did not become ready on $PORT"; }

export TROSA_BROWSER_ACCEPTANCE_URL="http://127.0.0.1:$PORT"
printf 'browser acceptance: Chromium run=%s tag=%s url=%s\n' \
  "$RUN_ID" "$RUN_TAG" "$TROSA_BROWSER_ACCEPTANCE_URL"

# 机器级锁只包住浏览器阶段（约 12-20 秒）：保证同机并发的门禁不会同时抢占浏览器
# 与端口。超时给得足够宽，让排队的门禁串行通过而不是直接失败。
if ! trosa_lock_acquire "$LOCK_DIR" 300 900; then
  fail_infra "无法获取浏览器验收机器锁：$LOCK_DIR（300s 超时）"
fi
LOCK_HELD=1
printf 'browser acceptance: 已获取机器锁 %s\n' "$LOCK_DIR"

set +e
node "$ROOT/tools/run_browser_acceptance.cjs" core
browser_status=$?
set -e
trosa_lock_release "$LOCK_DIR"; LOCK_HELD=0

# 把 node 驱动的退出码原样传回门禁（0/20/21），由门禁决定是否重跑。
[[ "$browser_status" == 0 ]] || exit "$browser_status"

printf 'browser acceptance: PASS (Playwright headless Chromium, run %s)\n' "$RUN_ID"
