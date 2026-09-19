#!/usr/bin/env bash
# Server-side verified PostgreSQL backup for one Trosa release.
#
# This is the single implementation of "a reliable, verified, restorable
# backup" used by both:
#
#   * release-remote.sh   — the pre-migration release gate (candidate copy);
#   * backup-workbench.sh — on-demand cloud verification / offsite archive.
#
# Guarantees (the whole point of this file):
#   1. the dump is produced by the production backup runner, which runs
#      ``pg_restore --list`` and writes a SHA-256 next to the dump;
#   2. the dump is copied to a durable, release-scoped directory on ECS;
#   3. the copy is re-verified: non-empty, size recorded, SHA-256 matches, and
#      ``pg_restore --list`` succeeds (proving it can actually be restored);
#   4. cloud storage mirroring (OSS) is optional, but when configured it must
#      upload AND self-report a matching SHA-256/size, or the backup is not
#      considered done.
#
# The release never needs to download this backup to the operator's Mac. A
# local archive is a separate, optional convenience.
#
# Exit codes == failure classes:
#   0   ok
#   10  backup_failed                 the dump could not be generated
#   11  backup_verification_failed    checksum / restorability check failed
#   12  backup_verification_failed    configured cloud mirror could not be
#                                     stored or verified
#
# Stdout always ends with one machine-readable line:
#   TROSA_BACKUP_JSON {"status": "...", "failure_class": "...", ...}
set -uo pipefail
export LC_ALL=C

usage() {
  cat <<'EOF'
Usage: backup-remote.sh RELEASE_ID [--bundle] [--json]

  RELEASE_ID   发布 id；备份写入 $TRADE_OS_RELEASE_BACKUP_ROOT/<RELEASE_ID>/
  --bundle     额外生成可下载的 tar.gz（dump + 附件 + manifest），并打印
               ARCHIVE= / SHA256= / DATABASE_DUMP= / DATABASE_SHA256=
  --json       只输出 TROSA_BACKUP_JSON 机器行（人类日志仍写 stderr）

环境：
  TRADE_OS_POSTGRES_ROOT         默认 /opt/trade-os-postgres
  TRADE_OS_DATA_DIR              默认 /var/lib/trade-os
  TRADE_OS_RELEASE_BACKUP_ROOT   默认 /var/lib/trade-os/release-backups
  TRADE_OS_PG_RESTORE_LIST_CMD   覆盖可恢复性检查命令（模板，{dump} 占位）
  TRADE_OS_BACKUP_OSS_URI        可选 OSS 目标前缀，如 oss://bucket/prefix
  TRADE_OS_BACKUP_UPLOAD_CMD     OSS 上传命令模板（{local} {remote} 占位）；
                                 必须自行校验并打印 size= 与 sha256=
EOF
}

RELEASE_ID=""
BUNDLE=0
JSON_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle) BUNDLE=1; shift ;;
    --json) JSON_ONLY=1; shift ;;
    --help|-h) usage; exit 0 ;;
    -*) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
    *) RELEASE_ID=$1; shift ;;
  esac
done

case "$RELEASE_ID" in
  ''|*[!A-Za-z0-9._-]*) printf 'Invalid release id: %s\n' "$RELEASE_ID" >&2; exit 2 ;;
esac

POSTGRES_ROOT="${TRADE_OS_POSTGRES_ROOT:-/opt/trade-os-postgres}"
DATA_DIR="${TRADE_OS_DATA_DIR:-/var/lib/trade-os}"
BACKUP_ROOT="${TRADE_OS_RELEASE_BACKUP_ROOT:-/var/lib/trade-os/release-backups}"
RESTORE_LIST_CMD="${TRADE_OS_PG_RESTORE_LIST_CMD:-}"
OSS_URI="${TRADE_OS_BACKUP_OSS_URI:-}"
UPLOAD_CMD="${TRADE_OS_BACKUP_UPLOAD_CMD:-}"
SNAP_DIR="$BACKUP_ROOT/$RELEASE_ID"

log() { printf '%s\n' "$*" >&2; }

# The runner is Linux (sha256sum/stat -c), but the same helpers are exercised
# by the local regression on macOS, so fall back to the BSD tools there.
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}
file_size() {
  if stat -c%s "$1" >/dev/null 2>&1; then
    stat -c%s "$1"
  else
    stat -f%z "$1"
  fi
}

# Print the single machine-readable result line. All inputs are passed as
# positional args so the JSON is always well-formed; ``oss`` is raw JSON.
emit() {
  local status=$1 failure_class=$2 storage=$3 path=$4 size_bytes=$5 sha=$6 restorable=$7 oss_json=$8
  python3 - "$status" "$failure_class" "$storage" "$path" "$size_bytes" "$sha" \
    "$restorable" "$oss_json" "$RELEASE_ID" <<'PY'
import json
import sys

status, failure_class, storage, path, size_bytes, sha, restorable, oss, release = sys.argv[1:10]
try:
    size_value = int(size_bytes) if size_bytes != "" else 0
except ValueError:
    size_value = 0
try:
    oss_doc = json.loads(oss)
except Exception:
    oss_doc = None
print("TROSA_BACKUP_JSON " + json.dumps({
    "release": release,
    "status": status,
    "failure_class": failure_class or None,
    "scope": "pre-migration server-local",
    "storage": storage,
    "path": path,
    "size_bytes": size_value,
    "sha256": sha,
    "restorable": bool(restorable),
    "verified": status == "ok",
    "oss": oss_doc,
}, sort_keys=True, separators=(",", ":")))
PY
}

fail() {
  # fail EXIT_CODE FAILURE_CLASS MESSAGE
  local code=$1 class=$2 message=$3
  log "backup-remote: $message"
  emit "failed" "$class" "" "" "" "" "" "null"
  exit "$code"
}

mkdir -p "$SNAP_DIR" 2>/dev/null || fail 10 backup_failed "cannot create $SNAP_DIR"
chmod 700 "$SNAP_DIR" 2>/dev/null || true

if [ ! -f "$POSTGRES_ROOT/backup.sh" ]; then
  fail 10 backup_failed "postgres backup runner missing at $POSTGRES_ROOT/backup.sh"
fi

backup_log="$SNAP_DIR/backup.log"
if ! (cd "$POSTGRES_ROOT" && ./backup.sh >"$backup_log" 2>&1); then
  fail 10 backup_failed "postgres backup.sh failed; see $backup_log"
fi
if [ "$JSON_ONLY" != 1 ]; then
  cat "$backup_log" 2>/dev/null || true
fi

dump_rel=$(sed -n 's/^backup=//p' "$backup_log" | tail -n 1)
reported_sha=$(sed -n 's/^sha256=//p' "$backup_log" | tail -n 1)
if [ -z "$dump_rel" ] || [ "${dump_rel#/}" != "$dump_rel" ] || [ -z "$reported_sha" ]; then
  fail 11 backup_verification_failed "backup.sh did not report a relative dump path and checksum"
fi
src="$POSTGRES_ROOT/$dump_rel"
if [ ! -s "$src" ]; then
  fail 11 backup_verification_failed "dump missing or empty: $src"
fi

actual_sha=$(sha256_of "$src")
if [ "$actual_sha" != "$reported_sha" ]; then
  fail 11 backup_verification_failed "dump checksum mismatch: reported=$reported_sha actual=$actual_sha"
fi

# Prove restorability. backup.sh already ran pg_restore --list inside the
# container; re-run it independently against the generated dump. Tests inject
# TRADE_OS_PG_RESTORE_LIST_CMD.
DEFAULT_RESTORE_LIST="(cd \"$POSTGRES_ROOT\" && docker compose exec -T postgres pg_restore --list \"/backups/$(basename "$dump_rel")\") >/dev/null 2>&1"
restore_cmd="$DEFAULT_RESTORE_LIST"
if [ -n "$RESTORE_LIST_CMD" ]; then
  restore_cmd="${RESTORE_LIST_CMD//\{dump\}/$src}"
fi
if ! eval "$restore_cmd"; then
  fail 11 backup_verification_failed "dump is not restorable (pg_restore --list failed)"
fi

# Durable, release-scoped copy.
dst="$SNAP_DIR/database.dump"
cp -f -- "$src" "$dst" 2>/dev/null || fail 11 backup_verification_failed "cannot copy dump to $dst"
chmod 600 "$dst" 2>/dev/null || true
size_bytes=$(file_size "$dst" 2>/dev/null || printf '')
if [ -z "$size_bytes" ] || [ "$size_bytes" -le 0 ]; then
  fail 11 backup_verification_failed "durable copy is empty: $dst"
fi
copy_sha=$(sha256_of "$dst")
if [ "$copy_sha" != "$actual_sha" ]; then
  fail 11 backup_verification_failed "durable copy checksum mismatch after copy"
fi

storage="ecs-durable"
oss_json="null"
if [ -n "$OSS_URI" ]; then
  remote="$OSS_URI/$RELEASE_ID/database.dump"
  if [ -z "$UPLOAD_CMD" ]; then
    fail 12 backup_verification_failed \
      "TRADE_OS_BACKUP_OSS_URI is set but TRADE_OS_BACKUP_UPLOAD_CMD is not; refusing an unverified cloud mirror"
  fi
  upload_rendered="${UPLOAD_CMD//\{local\}/$dst}"
  upload_rendered="${upload_rendered//\{remote\}/$remote}"
  upload_out=$(eval "$upload_rendered" 2>&1)
  upload_rc=$?
  if [ "$upload_rc" != 0 ]; then
    log "$upload_out"
    fail 12 backup_verification_failed "cloud mirror upload failed (exit $upload_rc)"
  fi
  if [ "$JSON_ONLY" != 1 ]; then log "$upload_out"; fi
  oss_sha=$(printf '%s\n' "$upload_out" | sed -n 's/^sha256=//p' | tail -n 1)
  oss_size=$(printf '%s\n' "$upload_out" | sed -n 's/^size=//p' | tail -n 1)
  oss_uri=$(printf '%s\n' "$upload_out" | sed -n 's/^oss_uri=//p' | tail -n 1)
  if [ -z "$oss_sha" ] || [ "$oss_sha" != "$actual_sha" ]; then
    fail 12 backup_verification_failed "cloud mirror reported checksum does not match local dump"
  fi
  if [ -n "$oss_size" ] && [ "$oss_size" != "$size_bytes" ]; then
    fail 12 backup_verification_failed "cloud mirror reported size does not match local dump"
  fi
  oss_json=$(python3 - "$oss_uri" "$remote" "$size_bytes" "$oss_sha" <<'PY'
import json
import sys
uri, fallback, size, sha = sys.argv[1:5]
print(json.dumps({"uri": uri or fallback, "size_bytes": int(size), "sha256": sha, "verified": True}, sort_keys=True))
PY
)
  storage="ecs-durable+oss"
fi

if [ "$BUNDLE" = 1 ]; then
  archive="/tmp/trosa-postgres-backup-$RELEASE_ID.tar.gz"
  staging="/tmp/trosa-postgres-backup-$RELEASE_ID"
  rm -rf -- "$staging"
  mkdir -p -- "$staging" || fail 10 backup_failed "cannot create bundle staging dir"
  cp -f -- "$dst" "$staging/database.dump"
  if [ -d "$DATA_DIR/uploads/customer_files" ]; then
    tar -C "$DATA_DIR" -cf "$staging/uploads.tar" uploads/customer_files 2>/dev/null || true
  fi
  {
    printf 'format=trosa-postgres-backup-v1\n'
    printf 'release=%s\n' "$RELEASE_ID"
    printf 'database_dump=database.dump\n'
    printf 'database_dump_sha256=%s\n' "$actual_sha"
    printf 'database_dump_size=%s\n' "$size_bytes"
    if [ -f "$staging/uploads.tar" ]; then printf 'attachments=uploads.tar\n'; else printf 'attachments=none\n'; fi
  } >"$staging/manifest.txt"
  tar -C "$staging" -czf "$archive" . 2>/dev/null || fail 10 backup_failed "cannot create bundle archive"
  rm -rf -- "$staging"
  bundle_sha=$(sha256_of "$archive")
  printf 'ARCHIVE=%s\n' "$archive"
  printf 'SHA256=%s\n' "$bundle_sha"
  printf 'DATABASE_DUMP=%s\n' "$dst"
  printf 'DATABASE_SHA256=%s\n' "$actual_sha"
fi

log "backup ok: $dst ($size_bytes bytes, sha256=$actual_sha, storage=$storage)"
emit "ok" "" "$storage" "$dst" "$size_bytes" "$actual_sha" "true" "$oss_json"
