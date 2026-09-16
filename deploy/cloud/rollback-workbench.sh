#!/usr/bin/env bash
# Switch to the newest release that is not currently active, then verify it.
#
# DEPRECATED: 请使用 deploy/cloud/trosa-release rollback（按 previous_healthy
# 指针回滚、带深度健康检查与机器可读结果）。本脚本冻结保留至新机制验证后删除。
set -euo pipefail
printf 'DEPRECATED: rollback-workbench.sh 已冻结，请改用 deploy/cloud/trosa-release rollback\n' >&2

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

"$SCRIPT_DIR/run-workbench-command.sh" \
  "$TRADE_OS_ECS_INSTANCE_ID" \
  "$TRADE_OS_ECS_REGION" \
  "$remote_command"
