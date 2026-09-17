#!/usr/bin/env bash
# Read recent Trade OS logs without SSH or an interactive Workbench session.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=release-env.sh
source "$SCRIPT_DIR/release-env.sh"
ENV_FILE="$(trosa_resolve_workbench_env "$SCRIPT_DIR")"
trosa_warn_legacy_workbench_env "$ENV_FILE"
source "$ENV_FILE"
: "${TRADE_OS_ECS_REGION:?TRADE_OS_ECS_REGION is required}"
: "${TRADE_OS_ECS_INSTANCE_ID:?TRADE_OS_ECS_INSTANCE_ID is required}"
LINES="${TRADE_OS_LOG_LINES:-120}"

TRADE_OS_CLOUD_ASSISTANT_TIMEOUT=90 bash "$SCRIPT_DIR/run-cloud-assistant-command.sh" \
  "$TRADE_OS_ECS_INSTANCE_ID" \
  "$TRADE_OS_ECS_REGION" \
  "journalctl -u trade-os -n '$LINES' --no-pager"
