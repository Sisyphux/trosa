#!/usr/bin/env bash
# Run the specialist Inbox human-intervention matrix in a real Chromium browser.
#
# Separate from browser_acceptance.sh: it loads a deterministic, namespaced Inbox
# fixture (8 actionable cards), generates fixed upload samples, then drives the
# rendered Inbox through Tabbit. It never converts a missing browser into SKIP.
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${TROSA_PYTHON:-$ROOT/.venv/bin/python}"
PORT="${TROSA_INBOX_BROWSER_PORT:-}"
REHEARSAL_PORT="${TROSA_INBOX_BROWSER_REHEARSAL_PORT:-}"
TASK="${TROSA_INBOX_BROWSER_TASK:-trosa-inbox-specialist-acceptance}"
REQUEST_ID="${TROSA_INBOX_BROWSER_REQUEST_ID:-acceptance-inbox-specialist}"
TABBIT_CLI="${TROSA_TABBIT_CLI:-}"
# Gate (release-test.sh) may already own a migrated rehearsal service; reuse it
# on the same connection and do not stop it on exit.
REUSE_REHEARSAL="${TROSA_INBOX_BROWSER_REUSE_REHEARSAL:-0}"
STOP_REHEARSAL_ON_EXIT=1
SERVICE_LOG=""
SERVICE_PID=""
SAMPLES_DIR=""

fail() {
  printf 'inbox browser acceptance: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
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

[[ -x "$PYTHON_BIN" ]] || fail "找不到项目 Python：$PYTHON_BIN"
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
if [[ -z "$TABBIT_CLI" ]]; then
  TABBIT_CLI="$(command -v tabbit-cli 2>/dev/null || true)"
fi
if [[ -z "$TABBIT_CLI" && -x "${HOME:-}/.local/bin/tabbit-cli" ]]; then
  TABBIT_CLI="${HOME}/.local/bin/tabbit-cli"
fi
[[ -x "$TABBIT_CLI" ]] || fail '找不到 Tabbit Chromium 浏览器控制器（需要 ~/.local/bin/tabbit-cli）；不会把浏览器验收标记为 SKIP'

if [[ "$REUSE_REHEARSAL" == 1 ]]; then
  [[ -n "${TRADE_OS_DATABASE_URL:-}" ]] \
    || fail '复用模式需要门禁先准备 rehearsal 环境（TRADE_OS_DATABASE_URL 为空）'
  STOP_REHEARSAL_ON_EXIT=0
else
  eval "$("$PYTHON_BIN" "$ROOT/tools/postgres_rehearsal.py" env)"
fi

# Deterministic Inbox-only fixture (8 cards) plus fixed upload samples.
fixture_json="$(CRM_ENV=rehearsal "$PYTHON_BIN" "$ROOT/tools/inbox_browser_fixture.py" 2>/dev/null | tail -n 1)"
contact_id="$(printf '%s' "$fixture_json" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["contact_id"])')" \
  || fail "无法解析 fixture contact_id：$fixture_json"
SAMPLES_DIR="$(mktemp -d "${TMPDIR:-/tmp}/trosa-inbox-samples.XXXXXX")"
"$PYTHON_BIN" -c 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from tools.inbox_browser_samples import create; create(Path(sys.argv[2]))' \
  "$ROOT" "$SAMPLES_DIR" || fail '无法生成上传样本'

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
[[ "$ready" == 1 ]] || { sed -n '1,240p' "$SERVICE_LOG" >&2; fail "rehearsal web service did not become ready on $PORT"; }

printf 'inbox browser acceptance: Chromium task=%s url=http://127.0.0.1:%s\n' "$TASK" "$PORT"

set +e
browser_output="$("$TABBIT_CLI" nodejs --task "$TASK" --request-id "$REQUEST_ID" --timeout-ms 180000 \
  < <(sed \
        -e "s|__TROSA_INBOX_BROWSER_URL__|http://127.0.0.1:$PORT|g" \
        -e "s|__TROSA_INBOX_BROWSER_SAMPLES__|$SAMPLES_DIR|g" \
        -e "s|__TROSA_INBOX_BROWSER_CONTACT_ID__|$contact_id|g" \
        "$ROOT/tools/inbox_browser_acceptance.js") 2>&1)"
browser_status=$?
set -e
printf '%s\n' "$browser_output"

if [[ "$browser_status" == 75 ]]; then
  set +e
  receipt_output="$("$TABBIT_CLI" receipt --task "$TASK" --request-id "$REQUEST_ID" --wait-ms 180000 2>&1)"
  browser_status=$?
  set -e
  printf '%s\n' "$receipt_output"
fi

set +e
"$TABBIT_CLI" finish --task "$TASK" --discard >/dev/null 2>&1
finish_status=$?
set -e
[[ "$finish_status" == 0 ]] || printf 'inbox browser acceptance: warning: Tabbit task cleanup returned %s\n' "$finish_status" >&2
[[ "$browser_status" == 0 ]] || fail "real Chromium Inbox acceptance failed (exit $browser_status)"

printf 'inbox browser acceptance: PASS (real Chromium Inbox matrix completed)\n'
