#!/usr/bin/env bash
# Read recent Trade OS logs over SSH (alias: TRADE_OS_SSH_HOST).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=release-env.sh
source "$SCRIPT_DIR/release-env.sh"
ENV_FILE="$(trosa_resolve_workbench_env "$SCRIPT_DIR")"
trosa_warn_legacy_workbench_env "$ENV_FILE"
source "$ENV_FILE"
LINES="${TRADE_OS_LOG_LINES:-120}"

bash "$SCRIPT_DIR/run-ssh-command.sh" "$(trosa_ssh_host)" \
  "journalctl -u trade-os -n '$LINES' --no-pager"
