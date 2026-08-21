#!/usr/bin/env bash
# Show the cloud service, tunnel, local health, resources, and recent errors.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${TRADE_OS_WORKBENCH_ENV:-$SCRIPT_DIR/workbench.env}"
source "$ENV_FILE"
: "${TRADE_OS_ECS_REGION:?TRADE_OS_ECS_REGION is required}"
: "${TRADE_OS_ECS_INSTANCE_ID:?TRADE_OS_ECS_INSTANCE_ID is required}"

workbench exec \
  --instance-id "$TRADE_OS_ECS_INSTANCE_ID" \
  --region "$TRADE_OS_ECS_REGION" \
  --user-name root \
  --command 'systemctl --no-pager --full status trade-os cloudflared || true; curl --fail --silent --show-error http://127.0.0.1:8080/api/network/ping || true; free -h; df -hT /; readlink -f /opt/trade-os/current 2>/dev/null || true; journalctl -u trade-os -p warning..alert -n 20 --no-pager || true'
