#!/usr/bin/env bash
# Create a verified application-level backup and download it to the Mac.
# This does not create or use an Alibaba Cloud ECS disk snapshot.
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
DATA_DIR="${TRADE_OS_DATA_DIR:-/var/lib/trade-os}"
LOCAL_BACKUP_ROOT="${TRADE_OS_LOCAL_BACKUP_DIR:-$HOME/Library/Application Support/trosa/backups}"
STAMP="$(date -u +%Y%m%d%H%M%S)"
ARCHIVE_NAME="trosa-backup-${STAMP}.tar.gz"
REMOTE_ARCHIVE="/tmp/${ARCHIVE_NAME}"

mkdir -p "$LOCAL_BACKUP_ROOT"

remote_command=$(cat <<EOF
set -eu
REMOTE_ROOT='$REMOTE_ROOT'
DATA_DIR='$DATA_DIR'
ARCHIVE='$REMOTE_ARCHIVE'
cd "\$REMOTE_ROOT/current"
runuser -u tradeos -- env CRM_DB_PATH="\$DATA_DIR" ARCHIVE_PATH="\$ARCHIVE" \$REMOTE_ROOT/venv/bin/python - <<'PY'
import hashlib
import os
import tarfile
from pathlib import Path

from db import backup_database

result = backup_database('workbench_download')
if result.get('failed'):
    raise SystemExit('backup failed: ' + repr(result['failed']))
backup_dir = Path(result['path']).resolve()
archive_path = Path(os.environ['ARCHIVE_PATH']).resolve()
with tarfile.open(archive_path, 'w:gz') as archive:
    for path in sorted(backup_dir.rglob('*')):
        archive.add(path, arcname=str(path.relative_to(backup_dir)))
digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
print('ARCHIVE=' + str(archive_path))
print('SHA256=' + digest)
print('BACKUP_PATH=' + str(backup_dir))
PY
EOF
)

output="$("$SCRIPT_DIR/run-workbench-command.sh" \
  "$TRADE_OS_ECS_INSTANCE_ID" \
  "$TRADE_OS_ECS_REGION" \
  "$remote_command")"
printf '%s\n' "$output"

remote_sha="$(printf '%s\n' "$output" | sed -n 's/^SHA256=//p' | tail -n 1)"
if [[ -z "$remote_sha" ]]; then
  printf '%s\n' 'Remote backup did not return a checksum.' >&2
  exit 1
fi

if [[ -n "${TRADE_OS_SSH_HOST:-}" ]]; then
  scp -o BatchMode=yes -o ConnectTimeout=10 \
    "${TRADE_OS_SSH_HOST}:${REMOTE_ARCHIVE}" "$LOCAL_BACKUP_ROOT/"
else
  workbench download "$REMOTE_ARCHIVE" "$LOCAL_BACKUP_ROOT/" \
    --instance-id "$TRADE_OS_ECS_INSTANCE_ID" --region "$TRADE_OS_ECS_REGION" --force
fi

local_archive="$LOCAL_BACKUP_ROOT/$ARCHIVE_NAME"
local_sha="$(shasum -a 256 "$local_archive" | awk '{print $1}')"
if [[ "$local_sha" != "$remote_sha" ]]; then
  printf 'Backup checksum mismatch: remote=%s local=%s\n' "$remote_sha" "$local_sha" >&2
  exit 1
fi

# Keep exactly the requested rolling window of local daily archives.
find "$LOCAL_BACKUP_ROOT" -type f -name 'trosa-backup-*.tar.gz' -mtime +14 -delete

"$SCRIPT_DIR/run-workbench-command.sh" \
  "$TRADE_OS_ECS_INSTANCE_ID" \
  "$TRADE_OS_ECS_REGION" \
  "rm -f '$REMOTE_ARCHIVE'"

printf 'local backup=%s\nsha256=%s\n' "$local_archive" "$local_sha"
