#!/usr/bin/env bash
# Run the Trosa core workflow in a real Chromium browser.
#
# This is intentionally separate from jsdom/DOM regression tests. It starts
# the isolated PostgreSQL rehearsal service, drives the visible app through the
# Tabbit Browser's Chromium runtime, and fails when the browser capability is
# unavailable instead of silently marking acceptance as skipped.
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${TROSA_PYTHON:-$ROOT/.venv/bin/python}"
PORT="${TROSA_BROWSER_ACCEPTANCE_PORT:-}"
REHEARSAL_PORT="${TROSA_BROWSER_REHEARSAL_PORT:-}"
TASK="${TROSA_BROWSER_ACCEPTANCE_TASK:-trosa-browser-acceptance}"
REQUEST_ID="${TROSA_BROWSER_ACCEPTANCE_REQUEST_ID:-acceptance-core-workflow}"
TABBIT_CLI="${TROSA_TABBIT_CLI:-}"
SERVICE_LOG=""
SERVICE_PID=""

fail() {
  printf 'browser acceptance: %s\n' "$*" >&2
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
  "$PYTHON_BIN" "$ROOT/tools/postgres_rehearsal.py" stop >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT INT TERM

[[ -x "$PYTHON_BIN" ]] || fail "找不到项目 Python：$PYTHON_BIN"
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

eval "$("$PYTHON_BIN" "$ROOT/tools/postgres_rehearsal.py" env)"
CRM_ENV=rehearsal "$PYTHON_BIN" "$ROOT/tools/postgres_rehearsal.py" fixture >/dev/null

SERVICE_LOG="$(mktemp "${TMPDIR:-/tmp}/trosa-browser-acceptance.XXXXXX")"
(
  cd "$ROOT"
  env \
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

export TROSA_BROWSER_ACCEPTANCE_URL="http://127.0.0.1:$PORT"
printf 'browser acceptance: Chromium task=%s url=%s\n' "$TASK" "$TROSA_BROWSER_ACCEPTANCE_URL"

set +e
browser_output="$("$TABBIT_CLI" nodejs --task "$TASK" --request-id "$REQUEST_ID" --timeout-ms 120000 \
  < <(sed "s|http://127.0.0.1:18180|http://127.0.0.1:$PORT|g" "$ROOT/tools/browser_acceptance.js") 2>&1)"
browser_status=$?
set -e
printf '%s\n' "$browser_output"

# A bounded executor timeout leaves the same request running. Read that exact
# receipt once before deciding, and never replay a possible mutation.
if [[ "$browser_status" == 75 ]]; then
  set +e
  receipt_output="$("$TABBIT_CLI" receipt --task "$TASK" --request-id "$REQUEST_ID" --wait-ms 120000 2>&1)"
  browser_status=$?
  set -e
  printf '%s\n' "$receipt_output"
fi

set +e
"$TABBIT_CLI" finish --task "$TASK" --discard >/dev/null 2>&1
finish_status=$?
set -e
[[ "$finish_status" == 0 ]] || printf 'browser acceptance: warning: Tabbit task cleanup returned %s\n' "$finish_status" >&2
[[ "$browser_status" == 0 ]] || fail "real Chromium acceptance failed (exit $browser_status)"

printf 'browser acceptance: PASS (real Chromium interaction completed)\n'
