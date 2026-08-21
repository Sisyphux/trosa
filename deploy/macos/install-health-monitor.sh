#!/bin/zsh
# Install the user-level LaunchAgent that checks Trade OS at login and every
# minute. It never touches the SQLite data directory.
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
SERVICE_DIR="${TRADE_OS_SERVICE_DIR:-/Users/luoxin/Library/Application Support/TradeOS}"
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
PLIST_SOURCE="$SCRIPT_DIR/com.tradeos.health.plist.example"
PLIST_TARGET="$LAUNCH_AGENT_DIR/com.tradeos.health.plist"
HEALTH_SCRIPT_SOURCE="$SCRIPT_DIR/check-and-repair.sh"
HEALTH_SCRIPT_TARGET="$SERVICE_DIR/check-and-repair.sh"
USER_ID="$(/usr/bin/id -u)"
LAUNCHCTL_BIN="/bin/launchctl"

if [[ ! -r "$PLIST_SOURCE" || ! -r "$HEALTH_SCRIPT_SOURCE" ]]; then
  print -u2 "Missing Trade OS health-monitor installation files."
  exit 1
fi

/usr/bin/plutil -lint "$PLIST_SOURCE" >/dev/null
/bin/mkdir -p "$SERVICE_DIR" "$SERVICE_DIR/logs" "$LAUNCH_AGENT_DIR"
/usr/bin/install -m 700 "$HEALTH_SCRIPT_SOURCE" "$HEALTH_SCRIPT_TARGET"
/usr/bin/install -m 644 "$PLIST_SOURCE" "$PLIST_TARGET"

"$LAUNCHCTL_BIN" bootout "gui/$USER_ID/com.tradeos.health" 2>/dev/null || true
"$LAUNCHCTL_BIN" bootstrap "gui/$USER_ID" "$PLIST_TARGET"
"$LAUNCHCTL_BIN" kickstart -k "gui/$USER_ID/com.tradeos.health"

print "Trade OS 自检已启用：登录启动后与每分钟执行一次。"
print "手动运行：$HEALTH_SCRIPT_TARGET"
