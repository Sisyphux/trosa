#!/bin/zsh
# Publish the current working copy to the Mac-hosted production runtime.
# It deliberately leaves the single live data directory, virtual environment,
# private settings and logs untouched.
set -euo pipefail

SOURCE_DIR="${TRADE_OS_SOURCE_DIR:-/Users/luoxin/Desktop/Trosa}"
SERVICE_DIR="${TRADE_OS_SERVICE_DIR:-/Users/luoxin/Library/Application Support/TradeOS}"
RUNTIME_DIR="$SERVICE_DIR/runtime"
TUNNEL_DIR="$SERVICE_DIR/tunnel"
DOMAIN_URL="${CRM_PUBLIC_URL:-https://app.trosa.space}"
CURL_BIN="${TRADE_OS_CURL_BIN:-/usr/bin/curl}"

if [[ ! -f "$SOURCE_DIR/app.py" || ! -d "$RUNTIME_DIR" ]]; then
  print -u2 "Trade OS source or production runtime is missing."
  exit 1
fi
if [[ ! -x "$CURL_BIN" ]]; then
  print -u2 "Missing curl executable: $CURL_BIN"
  exit 1
fi

# Anchor the business database exclusion to the project root. A broad
# "data" pattern also hides Pi's provider catalog under node_modules/**/data.
rsync -a --delete \
  --exclude '.git' \
  --exclude '.env' \
  --exclude '.env.production' \
  --exclude '/data' \
  --exclude '/data/***' \
  --exclude 'logs' \
  --exclude '.venv' \
  --exclude '.venv-mac' \
  "$SOURCE_DIR/" "$RUNTIME_DIR/"

mkdir -p "$TUNNEL_DIR"
rsync -a --delete \
  "$SOURCE_DIR/deploy/macos/cloudflared.yml" \
  "$SOURCE_DIR/deploy/macos/cloudflared-connect-proxy.py" \
  "$SOURCE_DIR/deploy/macos/run-cloudflared.sh" \
  "$TUNNEL_DIR/"
chmod 600 "$TUNNEL_DIR/cloudflared.yml"
chmod 700 "$TUNNEL_DIR/run-cloudflared.sh"

# Keep the production launcher in sync as well. In particular, it owns the
# idle-sleep assertion that keeps the local service and Tunnel online while the
# display is off.
install -m 700 \
  "$SOURCE_DIR/deploy/macos/run-production.sh" \
  "$SERVICE_DIR/run-production.sh"

# Keep the independent health monitor current without copying production data,
# private settings, virtual environments or logs.
install -m 700 \
  "$SOURCE_DIR/deploy/macos/check-and-repair.sh" \
  "$SERVICE_DIR/check-and-repair.sh"

launchctl kickstart -k "gui/$(id -u)/com.tradeos.app"

# The application needs a few seconds to initialise its scheduler and SQLite.
for _ in {1..12}; do
  if "$CURL_BIN" --fail --silent --max-time 2 http://127.0.0.1:8080/api/network/ping >/dev/null \
    && "$CURL_BIN" --fail --silent --max-time 2 http://127.0.0.1:8080/api/auth/users >/dev/null; then
    print "Trade OS published successfully: $DOMAIN_URL"
    exit 0
  fi
  sleep 2
done

print -u2 "Production service did not become healthy after publishing."
exit 1
