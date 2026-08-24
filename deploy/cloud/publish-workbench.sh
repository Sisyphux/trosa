#!/usr/bin/env bash
# Publish the current worktree as an atomic release through Workbench.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${TRADE_OS_WORKBENCH_ENV:-$SCRIPT_DIR/workbench.env}"
if [[ ! -r "$ENV_FILE" ]]; then
  printf 'Missing %s. Copy workbench.env.example first.\n' "$ENV_FILE" >&2
  exit 1
fi
source "$ENV_FILE"

: "${TRADE_OS_ECS_REGION:?TRADE_OS_ECS_REGION is required}"
: "${TRADE_OS_ECS_INSTANCE_ID:?TRADE_OS_ECS_INSTANCE_ID is required}"
REMOTE_ROOT="${TRADE_OS_REMOTE_ROOT:-/opt/trade-os}"
SERVICE_NAME="${TRADE_OS_SERVICE_NAME:-trade-os}"
SOURCE_DIR="${TRADE_OS_SOURCE_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
GITHUB_REMOTE="${TRADE_OS_GITHUB_REPOSITORY:-$(git -C "$SOURCE_DIR" remote get-url origin 2>/dev/null || true)}"
COMMIT_SHA="$(git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null || true)"
RELEASE_ID="${TRADE_OS_RELEASE_ID:-$(date -u +%Y%m%d%H%M%S)}"

case "$GITHUB_REMOTE" in
  https://github.com/*.git) GITHUB_REMOTE="${GITHUB_REMOTE%.git}" ;;
  https://github.com/*) ;;
  git@github.com:*) GITHUB_REMOTE="https://github.com/${GITHUB_REMOTE#git@github.com:}"; GITHUB_REMOTE="${GITHUB_REMOTE%.git}" ;;
  *)
    printf 'A public GitHub origin is required for SSM publishing: %s\n' "$GITHUB_REMOTE" >&2
    exit 1
    ;;
esac

if [[ -z "$COMMIT_SHA" || ! "$COMMIT_SHA" =~ ^[0-9a-fA-F]{40}$ ]]; then
  printf 'Unable to resolve the local HEAD commit.\n' >&2
  exit 1
fi

case "$RELEASE_ID" in
  ''|*[!A-Za-z0-9._-]*)
  printf 'Invalid release id: %s\n' "$RELEASE_ID" >&2
  exit 1
  ;;
esac

remote_command=$(cat <<EOF
set -eu
REMOTE_ROOT='$REMOTE_ROOT'
SERVICE_NAME='$SERVICE_NAME'
RELEASE_ID='$RELEASE_ID'
COMMIT_SHA='$COMMIT_SHA'
GITHUB_REMOTE='$GITHUB_REMOTE'
ARCHIVE_NAME="trosa-$COMMIT_SHA.tar.gz"
ARCHIVE_PATH="/tmp/$ARCHIVE_NAME"
RELEASE_DIR="\$REMOTE_ROOT/releases/\$RELEASE_ID"
PREVIOUS="\$(readlink -f "\$REMOTE_ROOT/current" 2>/dev/null || true)"
curl --fail --location --silent --show-error --max-time 180 \
  "\$GITHUB_REMOTE/archive/\$COMMIT_SHA.tar.gz" \
  -o "\$ARCHIVE_PATH"
rm -rf "\$RELEASE_DIR"
mkdir -p "\$RELEASE_DIR"
tar -xzf "\$ARCHIVE_PATH" -C "\$RELEASE_DIR" --strip-components=1
"\$REMOTE_ROOT/venv/bin/pip" install --disable-pip-version-check -r "\$RELEASE_DIR/requirements.txt"
"\$REMOTE_ROOT/venv/bin/python" -m py_compile \
  "\$RELEASE_DIR/app.py" "\$RELEASE_DIR/db.py" "\$RELEASE_DIR/scheduler.py" "\$RELEASE_DIR/serve.py"
chown -R root:root "\$RELEASE_DIR"
ln -sfn "\$RELEASE_DIR" "\$REMOTE_ROOT/current.next"
mv -Tf "\$REMOTE_ROOT/current.next" "\$REMOTE_ROOT/current"
systemctl daemon-reload
if ! systemctl restart "\$SERVICE_NAME"; then
  if [ -n "\$PREVIOUS" ]; then
    ln -sfn "\$PREVIOUS" "\$REMOTE_ROOT/current.next"
    mv -Tf "\$REMOTE_ROOT/current.next" "\$REMOTE_ROOT/current"
    systemctl restart "\$SERVICE_NAME" || true
  fi
  exit 1
fi
healthy=0
for attempt in \$(seq 1 15); do
  if curl --fail --silent --show-error --max-time 2 http://127.0.0.1:8080/api/network/ping >/dev/null; then
    healthy=1
    break
  fi
  sleep 1
done
if [ "\$healthy" != 1 ]; then
  if [ -n "\$PREVIOUS" ]; then
    ln -sfn "\$PREVIOUS" "\$REMOTE_ROOT/current.next"
    mv -Tf "\$REMOTE_ROOT/current.next" "\$REMOTE_ROOT/current"
    systemctl restart "\$SERVICE_NAME" || true
  fi
  journalctl -u "\$SERVICE_NAME" -n 80 --no-pager || true
  exit 1
fi
rm -f "\$ARCHIVE_PATH"
find "\$REMOTE_ROOT/releases" -mindepth 1 -maxdepth 1 -type d -print | sort -r | tail -n +6 | xargs -r rm -rf
printf 'published %s\n' "\$RELEASE_ID"
EOF
)

"$SCRIPT_DIR/run-workbench-command.sh" \
  "$TRADE_OS_ECS_INSTANCE_ID" \
  "$TRADE_OS_ECS_REGION" \
  "$remote_command"
