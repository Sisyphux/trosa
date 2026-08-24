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

if [[ ! "$GITHUB_REMOTE" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  printf 'A public GitHub repository URL is required: %s\n' "$GITHUB_REMOTE" >&2
  exit 1
fi

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
case "$REMOTE_ROOT" in
  ''|*[!A-Za-z0-9._/-]*)
    printf 'Invalid remote root: %s\n' "$REMOTE_ROOT" >&2
    exit 1
    ;;
esac
case "$SERVICE_NAME" in
  ''|*[!A-Za-z0-9_.@-]*)
    printf 'Invalid service name: %s\n' "$SERVICE_NAME" >&2
    exit 1
    ;;
esac

# The detailed publisher is part of the exact commit being released. SSM
# only receives a short command, avoiding input-buffer corruption in its
# interactive-only terminal mode.
GITHUB_REPOSITORY="${GITHUB_REMOTE#https://github.com/}"
REMOTE_SCRIPT_URL="https://raw.githubusercontent.com/$GITHUB_REPOSITORY/$COMMIT_SHA/deploy/cloud/publish-remote.sh"
remote_command=$(cat <<EOF
set -euo pipefail
curl --fail --location --silent --show-error --max-time 60 '$REMOTE_SCRIPT_URL' | \
  bash -s -- '$REMOTE_ROOT' '$SERVICE_NAME' '$RELEASE_ID' '$COMMIT_SHA' '$GITHUB_REMOTE'
EOF
)

"$SCRIPT_DIR/run-workbench-command.sh" \
  "$TRADE_OS_ECS_INSTANCE_ID" \
  "$TRADE_OS_ECS_REGION" \
  "$remote_command"
