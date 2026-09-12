#!/bin/zsh
# Trade OS macOS production launcher. Run only through com.tradeos.app.
#
# `caffeinate -i` keeps the host awake during idle periods while allowing its
# display to sleep normally. A manually requested system sleep still works and
# is recovered by the independent health monitor after wake.
set -euo pipefail

PROJECT_DIR="${TRADE_OS_PROJECT_DIR:-/Users/luoxin/Library/Application Support/TradeOS/runtime}"
SERVICE_DIR="${TRADE_OS_SERVICE_DIR:-/Users/luoxin/Library/Application Support/TradeOS}"
ENV_FILE="$SERVICE_DIR/env.production"
PYTHON_BIN="$PROJECT_DIR/.venv-mac/bin/python"
CAFFEINATE_BIN="${TRADE_OS_CAFFEINATE_BIN:-/usr/bin/caffeinate}"

if [[ ! -r "$ENV_FILE" ]]; then
  print -u2 "Missing production environment file: $ENV_FILE"
  exit 1
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  print -u2 "Missing production Python: $PYTHON_BIN"
  exit 1
fi
if [[ ! -x "$CAFFEINATE_BIN" ]]; then
  print -u2 "Missing macOS caffeinate executable: $CAFFEINATE_BIN"
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

if [[ "${CRM_ENV:-}" != "production" ]]; then
  print -u2 "Formal Trosa service requires CRM_ENV=production: $ENV_FILE"
  exit 1
fi
if [[ "${TRADE_OS_DATA_BACKEND:-}" != "postgres" || -z "${TRADE_OS_DATABASE_URL:-}" ]]; then
  print -u2 "Formal Trosa service requires TRADE_OS_DATA_BACKEND=postgres and TRADE_OS_DATABASE_URL"
  exit 1
fi
if [[ -z "${PGPASSFILE:-}" || ! -r "${PGPASSFILE}" ]]; then
  print -u2 "Formal Trosa service requires a readable PGPASSFILE"
  exit 1
fi

# Keep the system and network available for the named Tunnel, but do not hold
# the screen on. launchd terminates this process with the app during updates.
exec "$CAFFEINATE_BIN" -i "$PYTHON_BIN" "$PROJECT_DIR/serve.py"
