#!/usr/bin/env bash
# Show the production service, tunnel, local health, resources, and recent errors.
# Runs status-remote.sh on the server over SSH (alias: TRADE_OS_SSH_HOST).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=release-env.sh
source "$SCRIPT_DIR/release-env.sh"
ENV_FILE="$(trosa_resolve_workbench_env "$SCRIPT_DIR")"
trosa_warn_legacy_workbench_env "$ENV_FILE"
source "$ENV_FILE"
REMOTE_ROOT="${TRADE_OS_REMOTE_ROOT:-/opt/trade-os}"

case "$REMOTE_ROOT" in
  ''|*[!A-Za-z0-9._/-]*)
    printf 'Invalid remote root: %s\n' "$REMOTE_ROOT" >&2
    exit 1
    ;;
esac

# The detailed, machine-readable status script is shipped inside the release
# and runs on the server, where /proc and systemd exist.
remote_command="bash '$REMOTE_ROOT/current/deploy/cloud/status-remote.sh'"

bash "$SCRIPT_DIR/run-ssh-command.sh" "$(trosa_ssh_host)" "$remote_command"
