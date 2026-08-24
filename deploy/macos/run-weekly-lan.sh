#!/bin/zsh
# Keep the cloud weekly board available on the Mac's fixed office LAN address.
set -euo pipefail

SERVICE_DIR="${TRADE_OS_SERVICE_DIR:-/Users/luoxin/Library/Application Support/TradeOS}"
ENV_FILE="$SERVICE_DIR/weekly-lan.env"
GATEWAY_SCRIPT="$SERVICE_DIR/weekly-lan-gateway.py"
PYTHON_BIN="${TRADE_OS_WEEKLY_PYTHON:-/usr/bin/python3}"

if [[ ! -r "$ENV_FILE" ]]; then
  print -u2 "Missing weekly LAN environment file: $ENV_FILE"
  exit 1
fi
if [[ ! -r "$GATEWAY_SCRIPT" ]]; then
  print -u2 "Missing weekly LAN gateway: $GATEWAY_SCRIPT"
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

exec "$PYTHON_BIN" "$GATEWAY_SCRIPT"
