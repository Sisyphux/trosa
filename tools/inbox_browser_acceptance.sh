#!/usr/bin/env bash
# Run the specialist Inbox human-intervention matrix in a real headless Chromium.
#
# Separate from browser_acceptance.sh: it loads a deterministic, namespaced Inbox
# fixture (10 actionable questions), generates fixed upload samples, then drives
# the rendered Inbox through a fresh Playwright Chromium pinned by
# browser-extension/package-lock.json. It never converts a missing browser into
# SKIP.
#
# Concurrency: like the core acceptance, every run gets a unique RUN_ID and only
# the browser phase is wrapped in the shared machine-wide browser lock.
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
PORT="${TROSA_INBOX_BROWSER_PORT:-}"
REHEARSAL_PORT="${TROSA_INBOX_BROWSER_REHEARSAL_PORT:-}"
REUSE_REHEARSAL="${TROSA_INBOX_BROWSER_REUSE_REHEARSAL:-0}"
# 每次验收独占一次运行：RUN_ID 是本次运行的唯一标识，等价于旧的浏览器任务/请求号。
RUN_ID="${TROSA_INBOX_BROWSER_RUN_ID:-${TROSA_BROWSER_ACCEPTANCE_RUN_ID:-inbox-$(date -u +%Y%m%dT%H%M%S)-$$-$RANDOM}}"
STOP_REHEARSAL_ON_EXIT=1
SERVICE_LOG=""
SERVICE_PID=""
SAMPLES_DIR=""
LOCK_HELD=0

fail() {
  printf 'inbox browser acceptance: %s\n' "$*" >&2
  exit 1
}

# 基础设施故障：退出码 21，交回门禁按规则重跑一次并记 flake。
fail_infra() {
  printf 'inbox browser acceptance: %s\n' "$*" >&2
  exit 21
}

# 锁与产物目录落在共享 git 目录下：与核心验收共用同一把机器级浏览器锁。
GIT_COMMON_DIR="$(git -C "$ROOT" rev-parse --git-common-dir 2>/dev/null || true)"
if [[ -z "$GIT_COMMON_DIR" ]]; then
  GIT_COMMON_DIR="$ROOT/.git"
fi
case "$GIT_COMMON_DIR" in
  /*) ;;
  *) GIT_COMMON_DIR="$ROOT/$GIT_COMMON_DIR" ;;
esac
LOCK_DIR="${TROSA_INBOX_BROWSER_LOCK_DIR:-$GIT_COMMON_DIR/trosa-tasks/browser-acceptance.lock}"
ARTIFACT_DIR="${TROSA_BROWSER_ACCEPTANCE_ARTIFACTS:-$GIT_COMMON_DIR/trosa-tasks/browser-artifacts}"
export TROSA_BROWSER_ACCEPTANCE_ARTIFACTS="$ARTIFACT_DIR"
export TROSA_BROWSER_ACCEPTANCE_RUN_ID="$RUN_ID"

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
  if [[ -n "$SAMPLES_DIR" && -d "$SAMPLES_DIR" ]]; then
    rm -rf -- "$SAMPLES_DIR"
  fi
  if [[ "$STOP_REHEARSAL_ON_EXIT" == 1 ]]; then
    "$PYTHON_BIN" "$ROOT/tools/postgres_rehearsal.py" stop >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

[[ -x "$PYTHON_BIN" ]] || fail_infra "找不到项目 Python：$PYTHON_BIN"
command -v node >/dev/null 2>&1 || fail_infra '找不到 node，无法启动锁定的 Playwright Chromium'
[[ -f "$ROOT/browser-extension/node_modules/playwright/package.json" ]] \
  || fail_infra "找不到锁定的 Playwright（$ROOT/browser-extension/node_modules/playwright）；请在 browser-extension 执行 npm ci；不会把浏览器验收标记为 SKIP"
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
  [[ -n "${TRADE_OS_DATABASE_URL:-}" ]] \
    || fail_infra '复用模式需要门禁先准备 rehearsal 环境（TRADE_OS_DATABASE_URL 为空）'
  STOP_REHEARSAL_ON_EXIT=0
else
  eval "$("$PYTHON_BIN" "$ROOT/tools/postgres_rehearsal.py" env)"
fi

# Deterministic Inbox-only fixture (10 questions) plus fixed upload samples.
fixture_json="$(CRM_ENV=rehearsal "$PYTHON_BIN" "$ROOT/tools/inbox_browser_fixture.py" 2>/dev/null | tail -n 1)"
contact_id="$(printf '%s' "$fixture_json" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["contact_id"])')" \
  || fail_infra "无法解析 fixture contact_id：$fixture_json"
SAMPLES_DIR="$(mktemp -d "${TMPDIR:-/tmp}/trosa-inbox-samples.XXXXXX")"
"$PYTHON_BIN" -c 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from tools.inbox_browser_samples import create; create(Path(sys.argv[2]))' \
  "$ROOT" "$SAMPLES_DIR" || fail_infra '无法生成上传样本'

SERVICE_LOG="$(mktemp "${TMPDIR:-/tmp}/trosa-inbox-acceptance.XXXXXX")"
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

export TROSA_INBOX_BROWSER_URL="http://127.0.0.1:$PORT"
export TROSA_INBOX_BROWSER_SAMPLES="$SAMPLES_DIR"
export TROSA_INBOX_BROWSER_CONTACT_ID="$contact_id"
printf 'inbox browser acceptance: Chromium run=%s url=%s\n' "$RUN_ID" "$TROSA_INBOX_BROWSER_URL"

# 与核心验收共用同一把机器级锁，只包住浏览器阶段。
if ! trosa_lock_acquire "$LOCK_DIR" 300 900; then
  fail_infra "无法获取浏览器验收机器锁：$LOCK_DIR（300s 超时）"
fi
LOCK_HELD=1
printf 'inbox browser acceptance: 已获取机器锁 %s\n' "$LOCK_DIR"

set +e
node "$ROOT/tools/run_browser_acceptance.cjs" inbox
browser_status=$?
set -e
trosa_lock_release "$LOCK_DIR"; LOCK_HELD=0

[[ "$browser_status" == 0 ]] || exit "$browser_status"

printf 'inbox browser acceptance: PASS (Playwright headless Chromium Inbox matrix, run %s)\n' "$RUN_ID"
