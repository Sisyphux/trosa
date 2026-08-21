#!/usr/bin/env bash
# Switch to the newest release that is not currently active, then verify it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${TRADE_OS_WORKBENCH_ENV:-$SCRIPT_DIR/workbench.env}"
source "$ENV_FILE"
: "${TRADE_OS_ECS_REGION:?TRADE_OS_ECS_REGION is required}"
: "${TRADE_OS_ECS_INSTANCE_ID:?TRADE_OS_ECS_INSTANCE_ID is required}"
REMOTE_ROOT="${TRADE_OS_REMOTE_ROOT:-/opt/trade-os}"

remote_command=$(cat <<EOF
set -eu
ROOT='$REMOTE_ROOT'
CURRENT="\$(readlink -f "\$ROOT/current" 2>/dev/null || true)"
TARGET=''
for release in \$(find "\$ROOT/releases" -mindepth 1 -maxdepth 1 -type d -print | sort -r); do
  if [ "\$release" != "\$CURRENT" ]; then
    TARGET="\$release"
    break
  fi
done
if [ -z "\$TARGET" ]; then
  printf '%s\n' 'No previous release is available.' >&2
  exit 1
fi
ln -sfn "\$TARGET" "\$ROOT/current.next"
mv -Tf "\$ROOT/current.next" "\$ROOT/current"
systemctl restart trade-os
curl --fail --silent --show-error http://127.0.0.1:8080/api/network/ping
printf 'rolled back to %s\n' "\$TARGET"
EOF
)

workbench exec \
  --instance-id "$TRADE_OS_ECS_INSTANCE_ID" \
  --region "$TRADE_OS_ECS_REGION" \
  --user-name root \
  --command "$remote_command"
