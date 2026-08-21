#!/usr/bin/env bash
# Build a clean Trade OS release archive without data, secrets, virtualenvs,
# logs, or local development artifacts.
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${TRADE_OS_SOURCE_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
RELEASE_ID="${TRADE_OS_RELEASE_ID:-$(date -u +%Y%m%d%H%M%S)}"
OUTPUT_DIR="${TRADE_OS_RELEASE_OUTPUT_DIR:-${TMPDIR:-/tmp}}"
ARCHIVE_PATH="$OUTPUT_DIR/trosa-${RELEASE_ID}.tar.gz"
STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/trosa-release.XXXXXX")"

cleanup() {
  rm -rf "$STAGE_DIR"
}
trap cleanup EXIT

rsync -a --delete \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='.venv-mac' \
  --exclude='.env' \
  --exclude='.env.*' \
  --exclude='data' \
  --exclude='data/***' \
  --exclude='logs' \
  --exclude='tmp' \
  --exclude='archive' \
  --exclude='.pytest_cache' \
  --exclude='__pycache__' \
  --exclude='.DS_Store' \
  --exclude='deploy/macos/cloudflared.yml' \
  --exclude='deploy/cloud/workbench.env' \
  "$SOURCE_DIR/" "$STAGE_DIR/"

COPYFILE_DISABLE=1 tar -czf "$ARCHIVE_PATH" -C "$STAGE_DIR" .
printf '%s\n' "$ARCHIVE_PATH"
