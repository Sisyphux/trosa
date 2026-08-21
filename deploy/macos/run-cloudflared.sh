#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${TRADE_OS_PROJECT_DIR:-/Users/luoxin/Desktop/Trosa}"
if [[ -r "$SCRIPT_DIR/cloudflared.yml" && -r "$SCRIPT_DIR/cloudflared-connect-proxy.py" ]]; then
  CONFIG_FILE="$SCRIPT_DIR/cloudflared.yml"
  PROXY_HELPER="$SCRIPT_DIR/cloudflared-connect-proxy.py"
else
  CONFIG_FILE="$PROJECT_DIR/deploy/macos/cloudflared.yml"
  PROXY_HELPER="$PROJECT_DIR/deploy/macos/cloudflared-connect-proxy.py"
fi
PYTHON_BIN="${TRADE_OS_CLOUDFLARED_PROXY_PYTHON:-/usr/bin/python3}"

if [[ ! -r "$CONFIG_FILE" ]]; then
  print -u2 "Missing Tunnel configuration: $CONFIG_FILE"
  exit 1
fi
if [[ ! -r "$PROXY_HELPER" ]]; then
  print -u2 "Missing Cloudflare proxy helper: $PROXY_HELPER"
  exit 1
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  print -u2 "Missing proxy helper Python: $PYTHON_BIN"
  exit 1
fi

"$PYTHON_BIN" "$PROXY_HELPER" &
HELPER_PID=$!
CLOUDFLARED_PID=0

cleanup() {
  if (( CLOUDFLARED_PID > 0 )); then
    kill -TERM "$CLOUDFLARED_PID" 2>/dev/null || true
  fi
  kill "$HELPER_PID" 2>/dev/null || true
  if (( CLOUDFLARED_PID > 0 )); then
    wait "$CLOUDFLARED_PID" 2>/dev/null || true
  fi
  wait "$HELPER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

sleep 1
/usr/local/bin/cloudflared \
  --config "$CONFIG_FILE" \
  --no-autoupdate \
  --protocol http2 \
  tunnel run \
  --dns-resolver-addrs 127.0.0.1:15353 &
CLOUDFLARED_PID=$!
wait "$CLOUDFLARED_PID"
