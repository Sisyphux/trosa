#!/bin/zsh
# Install the always-running Mac helper that opens the read-only weekly board
# only while this Mac owns its fixed office address.
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
SERVICE_DIR="${TRADE_OS_SERVICE_DIR:-/Users/luoxin/Library/Application Support/TradeOS}"
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
ENV_FILE="$SERVICE_DIR/weekly-lan.env"
PLIST_TARGET="$LAUNCH_AGENT_DIR/com.tradeos.weekly-lan.plist"
USER_ID="$(/usr/bin/id -u)"

for required in weekly-lan-gateway.py run-weekly-lan.sh com.tradeos.weekly-lan.plist.example; do
  if [[ ! -r "$SCRIPT_DIR/$required" ]]; then
    print -u2 "Missing $SCRIPT_DIR/$required"
    exit 1
  fi
done

/usr/bin/plutil -lint "$SCRIPT_DIR/com.tradeos.weekly-lan.plist.example" >/dev/null
/bin/mkdir -p "$SERVICE_DIR/logs" "$LAUNCH_AGENT_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  TOKEN="$(/usr/bin/openssl rand -hex 32)"
  /usr/bin/printf '%s\n' \
    'TRADE_OS_WEEKLY_LAN_HOST=192.168.0.58' \
    'TRADE_OS_WEEKLY_LAN_PORT=8080' \
    'TRADE_OS_WEEKLY_ALLOWED_NETWORKS=192.168.0.0/23' \
    'TRADE_OS_WEEKLY_UPSTREAM=https://app.trosa.space' \
    "TRADE_OS_WEEKLY_GATEWAY_TOKEN=$TOKEN" > "$ENV_FILE"
  /bin/chmod 600 "$ENV_FILE"
fi

set -a
source "$ENV_FILE"
set +a
if [[ -z "${TRADE_OS_WEEKLY_GATEWAY_TOKEN:-}" ]]; then
  print -u2 "weekly-lan.env is missing TRADE_OS_WEEKLY_GATEWAY_TOKEN"
  exit 1
fi

/usr/bin/install -m 700 "$SCRIPT_DIR/weekly-lan-gateway.py" "$SERVICE_DIR/weekly-lan-gateway.py"
/usr/bin/install -m 700 "$SCRIPT_DIR/run-weekly-lan.sh" "$SERVICE_DIR/run-weekly-lan.sh"
/usr/bin/install -m 644 "$SCRIPT_DIR/com.tradeos.weekly-lan.plist.example" "$PLIST_TARGET"

/bin/launchctl bootout "gui/$USER_ID/com.tradeos.weekly-lan" 2>/dev/null || true
/bin/launchctl enable "gui/$USER_ID/com.tradeos.weekly-lan"
/bin/launchctl bootstrap "gui/$USER_ID" "$PLIST_TARGET"
/bin/launchctl kickstart -k "gui/$USER_ID/com.tradeos.weekly-lan"

DIGEST="$(/usr/bin/printf '%s' "$TRADE_OS_WEEKLY_GATEWAY_TOKEN" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}')"
print "公司周报入口已安装；Mac 连接公司网络后自动提供 http://192.168.0.58:8080"
print "CRM_WEEKLY_GATEWAY_TOKEN_SHA256=$DIGEST"
