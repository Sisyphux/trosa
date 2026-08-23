#!/usr/bin/env bash
# Show the cloud service, tunnel, local health, resources, and recent errors.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${TRADE_OS_WORKBENCH_ENV:-$SCRIPT_DIR/workbench.env}"
source "$ENV_FILE"
: "${TRADE_OS_ECS_REGION:?TRADE_OS_ECS_REGION is required}"
: "${TRADE_OS_ECS_INSTANCE_ID:?TRADE_OS_ECS_INSTANCE_ID is required}"

remote_command=$(cat <<'EOF'
set -u
app_status=$(systemctl is-active trade-os 2>/dev/null || true)
tunnel_status=$(systemctl is-active cloudflared 2>/dev/null || true)
health_status=down
if curl --fail --silent --show-error http://127.0.0.1:8080/api/network/ping; then
  health_status=ok
fi
release=$(readlink -f /opt/trade-os/current 2>/dev/null || true)
release=${release##*/}
if [ -z "$release" ]; then release=none; fi
printf 'TROSA_MANAGER_STATUS app=%s tunnel=%s health=%s release=%s\n' "$app_status" "$tunnel_status" "$health_status" "$release"
systemctl --no-pager --full status trade-os cloudflared || true
free -h
df -hT /
journalctl -u trade-os -p warning..alert -n 20 --no-pager || true
EOF
)

workbench exec \
  --instance-id "$TRADE_OS_ECS_INSTANCE_ID" \
  --region "$TRADE_OS_ECS_REGION" \
  --user-name root \
  --command "$remote_command"
