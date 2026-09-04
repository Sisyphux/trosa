#!/usr/bin/env bash
# Show the cloud service, tunnel, local health, resources, and recent errors.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${TRADE_OS_WORKBENCH_ENV:-$SCRIPT_DIR/workbench.env}"
source "$ENV_FILE"
: "${TRADE_OS_ECS_REGION:?TRADE_OS_ECS_REGION is required}"
: "${TRADE_OS_ECS_INSTANCE_ID:?TRADE_OS_ECS_INSTANCE_ID is required}"
REMOTE_ROOT="${TRADE_OS_REMOTE_ROOT:-/opt/trade-os}"

case "$REMOTE_ROOT" in
  ''|*[!A-Za-z0-9._/-]*)
    printf 'Invalid remote root: %s\n' "$REMOTE_ROOT" >&2
    exit 1
    ;;
esac

# Keep the SSM input short. The detailed, machine-readable status script is
# shipped inside the release and runs on ECS, where /proc and systemd exist.
remote_command="bash '$REMOTE_ROOT/current/deploy/cloud/status-remote.sh'"

# Status is read-only and must remain available when the local SSH path is
# blocked while the Workbench control plane is healthy. The command runner
# keeps the interactive Workbench session as a compatibility fallback.
# Keep the compatibility path bounded as well; a failed read must not occupy
# the workbench refresh loop for the full maintenance-command timeout.
TRADE_OS_PREFER_WORKBENCH=1 \
TROSA_WORKBENCH_EXPECT_TIMEOUT=45 \
  "$SCRIPT_DIR/run-workbench-command.sh" \
  "$TRADE_OS_ECS_INSTANCE_ID" \
  "$TRADE_OS_ECS_REGION" \
  "$remote_command"
