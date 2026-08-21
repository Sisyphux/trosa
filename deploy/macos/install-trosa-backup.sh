#!/usr/bin/env bash
# Install the daily ECS-to-Mac backup LaunchAgent.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BACKUP_SCRIPT="$PROJECT_ROOT/deploy/cloud/backup-workbench.sh"
USER_ID="$(id -u)"
PLIST_PATH="$HOME/Library/LaunchAgents/com.trosa.backup.plist"
LOG_DIR="$HOME/Library/Application Support/trosa/logs"

if [[ ! -x "$BACKUP_SCRIPT" ]]; then
  chmod +x "$BACKUP_SCRIPT"
fi
mkdir -p "$(dirname "$PLIST_PATH")" "$LOG_DIR"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.trosa.backup</string>
    <key>ProgramArguments</key>
    <array>
        <string>$BACKUP_SCRIPT</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>3</integer>
        <key>Minute</key>
        <integer>30</integer>
    </dict>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/backup.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/backup-error.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$USER_ID/com.trosa.backup" 2>/dev/null || true
launchctl bootstrap "gui/$USER_ID" "$PLIST_PATH"
launchctl enable "gui/$USER_ID/com.trosa.backup"
printf 'installed %s; daily backup at 03:30\n' "$PLIST_PATH"
