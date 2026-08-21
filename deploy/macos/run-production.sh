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

# Keep the system and network available for the named Tunnel, but do not hold
# the screen on. launchd terminates this process with the app during updates.
exec "$CAFFEINATE_BIN" -i "$PYTHON_BIN" "$PROJECT_DIR/serve.py"
