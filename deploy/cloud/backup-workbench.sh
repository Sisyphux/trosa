#!/usr/bin/env bash
# Create a verified PostgreSQL + attachment backup and download it to the Mac.
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
DATA_DIR="${TRADE_OS_DATA_DIR:-/var/lib/trade-os}"
POSTGRES_ROOT="${TRADE_OS_POSTGRES_ROOT:-/opt/trade-os-postgres}"
LOCAL_BACKUP_ROOT="${TRADE_OS_LOCAL_BACKUP_DIR:-$HOME/Library/Application Support/trosa/backups}"
STAMP="$(date -u +%Y%m%d%H%M%S)"
ARCHIVE_NAME="trosa-postgres-backup-${STAMP}.tar.gz"
REMOTE_ARCHIVE="/tmp/${ARCHIVE_NAME}"

mkdir -p "$LOCAL_BACKUP_ROOT"

remote_command=$(cat <<EOF
set -euo pipefail
DATA_DIR='$DATA_DIR'
POSTGRES_ROOT='$POSTGRES_ROOT'
ARCHIVE='$REMOTE_ARCHIVE'
cd "\$POSTGRES_ROOT"
backup_log="/tmp/trosa-postgres-backup-${STAMP}.database.log"
./backup.sh | tee "\$backup_log"
dump_rel="\$(sed -n 's/^backup=//p' "\$backup_log" | tail -n 1)"
database_sha="\$(sed -n 's/^sha256=//p' "\$backup_log" | tail -n 1)"
if [ -z "\$dump_rel" ] || [ -z "\$database_sha" ]; then
  printf '%s\\n' 'PostgreSQL backup did not return a dump path and checksum.' >&2
  exit 1
fi
if [ "\${dump_rel#/}" != "\$dump_rel" ]; then
  printf '%s\\n' 'Unexpected absolute PostgreSQL dump path.' >&2
  exit 1
fi
dump_path="\$POSTGRES_ROOT/\$dump_rel"
test -s "\$dump_path"
test "\$(sha256sum "\$dump_path" | awk '{print \$1}')" = "\$database_sha"

staging="/tmp/trosa-postgres-backup-${STAMP}"
rm -rf "\$staging"
mkdir -p "\$staging"
cp -- "\$dump_path" "\$staging/database.dump"
if [ -d "\$DATA_DIR/uploads/customer_files" ]; then
  tar -C "\$DATA_DIR" -cf "\$staging/uploads.tar" uploads/customer_files
fi
{
  printf 'format=trosa-postgres-backup-v1\\n'
  printf 'database_dump=database.dump\\n'
  printf 'database_dump_sha256=%s\\n' "\$database_sha"
  if [ -f "\$staging/uploads.tar" ]; then
    printf 'attachments=uploads.tar\\n'
  else
    printf 'attachments=none\\n'
  fi
} > "\$staging/manifest.txt"
tar -C "\$staging" -czf "\$ARCHIVE" .
rm -rf "\$staging"
bundle_sha="\$(sha256sum "\$ARCHIVE" | awk '{print \$1}')"
rm -f "\$backup_log"
printf 'ARCHIVE=%s\\n' "\$ARCHIVE"
printf 'SHA256=%s\\n' "\$bundle_sha"
printf 'DATABASE_DUMP=%s\\n' "\$dump_path"
printf 'DATABASE_SHA256=%s\\n' "\$database_sha"
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
tar -tzf "$local_archive" >/dev/null

# Keep exactly the requested rolling window of local daily archives.
find "$LOCAL_BACKUP_ROOT" -type f -name 'trosa-postgres-backup-*.tar.gz' -mtime +14 -delete

"$SCRIPT_DIR/run-workbench-command.sh" \
  "$TRADE_OS_ECS_INSTANCE_ID" \
  "$TRADE_OS_ECS_REGION" \
  "rm -f '$REMOTE_ARCHIVE'"

printf 'local backup=%s\nsha256=%s\n' "$local_archive" "$local_sha"
