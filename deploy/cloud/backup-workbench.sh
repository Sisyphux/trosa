#!/usr/bin/env bash
# Create and verify a PostgreSQL + attachment backup on ECS, then OPTIONALLY
# download it to the Mac.
#
# Why this was rewritten: the release gate used to run this script before every
# database-sensitive publish, and any failure to transfer the archive to the Mac
# (Workbench download / scp / SSH file stream) blocked the release even though a
# perfectly good, verified backup already existed in the cloud. The download is
# now optional and can never block a release.
#
# Two independent outcomes, never conflated:
#   1. Cloud backup (generation + checksum + restorability + optional OSS
#      mirror) — this is the safety gate. Failure exits 10/11/12.
#   2. Local archive download — a convenience. Failure exits 20
#      (``local_download_failed``) and does not mean the backup failed.
#
# Usage:
#   backup-workbench.sh [--cloud-only] [--download=auto|require|never] [--json]
#
#   --cloud-only           只在 ECS 生成并校验云端备份，不下载。
#   --download=auto         尝试下载；失败打印 LOCAL_DOWNLOAD_FAILED 并以 20 退出。
#   --download=require      同 auto；调用方可据此把本地归档视为必需。
#   --download=never        明确不下载。
#
# Exit codes:
#   0   ok
#   10  backup_failed
#   11  backup_verification_failed
#   12  backup_verification_failed (configured cloud mirror failed)
#   20  local_download_failed
set -uo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=release-env.sh
source "$SCRIPT_DIR/release-env.sh"
ENV_FILE="$(trosa_resolve_workbench_env "$SCRIPT_DIR")"
if [[ ! -r "$ENV_FILE" ]]; then
  printf '缺少发布配置 %s。\n把 deploy/cloud/workbench.env.example 复制到 %s/trosa/workbench.env 并填好路由信息。\n' \
    "$ENV_FILE" "${XDG_CONFIG_HOME:-$HOME/.config}" >&2
  exit 1
fi
trosa_warn_legacy_workbench_env "$ENV_FILE"
source "$ENV_FILE"

: "${TRADE_OS_ECS_REGION:?TRADE_OS_ECS_REGION is required}"
: "${TRADE_OS_ECS_INSTANCE_ID:?TRADE_OS_ECS_INSTANCE_ID is required}"
DATA_DIR="${TRADE_OS_DATA_DIR:-/var/lib/trade-os}"
POSTGRES_ROOT="${TRADE_OS_POSTGRES_ROOT:-/opt/trade-os-postgres}"
REMOTE_ROOT="${TRADE_OS_REMOTE_ROOT:-/opt/trade-os}"
LOCAL_BACKUP_ROOT="${TRADE_OS_LOCAL_BACKUP_DIR:-$HOME/Library/Application Support/trosa/backups}"
STAMP="$(date -u +%Y%m%d%H%M%S)"
RELEASE_ID="backup-${STAMP}"
ARCHIVE_NAME="trosa-postgres-backup-${STAMP}.tar.gz"
REMOTE_ARCHIVE="/tmp/${ARCHIVE_NAME}"
# Transport: auto (try scp then Workbench), ssh (scp only), workbench (only).
TRANSFER="${TRADE_OS_BACKUP_TRANSFER:-auto}"
DOWNLOAD_MODE="auto"
CLOUD_ONLY=0
JSON_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cloud-only) CLOUD_ONLY=1; shift ;;
    --download=*) DOWNLOAD_MODE="${1#--download=}"; shift ;;
    --require-download) DOWNLOAD_MODE="require"; shift ;;
    --json) JSON_ONLY=1; shift ;;
    --help|-h)
      sed -n '1,40p' "$0" | sed -n '/^Usage:/,/^Exit codes:/p'
      exit 0 ;;
    *) printf '未知参数：%s\n' "$1" >&2; exit 2 ;;
  esac
done
case "$DOWNLOAD_MODE" in auto|require|never) ;; *) printf '非法 --download 值：%s\n' "$DOWNLOAD_MODE" >&2; exit 2 ;; esac
[[ "$CLOUD_ONLY" == 1 ]] && DOWNLOAD_MODE="never"

for pair in "RELEASE_ID:$RELEASE_ID" "REMOTE_ROOT:$REMOTE_ROOT" \
            "POSTGRES_ROOT:$POSTGRES_ROOT" "DATA_DIR:$DATA_DIR"; do
  value="${pair#*:}"
  [[ "$value" != *"'"* ]] || { printf '非法值（含单引号）：%s\n' "${pair%%:*}" >&2; exit 2; }
done

mkdir -p "$LOCAL_BACKUP_ROOT"

USE_BUNDLE=0
[[ "$DOWNLOAD_MODE" != "never" ]] && USE_BUNDLE=1
BUNDLE_FLAG=""
[[ "$USE_BUNDLE" == 1 ]] && BUNDLE_FLAG="--bundle"

# The pre-migration helper is the single verified-backup implementation. Use
# the deployed copy (current release, else the newest release that has it); a
# legacy inline path keeps the tool usable on an ECS that predates the helper.
remote_command=$(cat <<EOF
set -uo pipefail
REMOTE_ROOT='$REMOTE_ROOT'
POSTGRES_ROOT='$POSTGRES_ROOT'
DATA_DIR='$DATA_DIR'
RELEASE_ID='$RELEASE_ID'
if [ -r /etc/trade-os/trade-os.env ]; then set -a; . /etc/trade-os/trade-os.env; set +a; fi
helper=''
for candidate in "\$REMOTE_ROOT/current/deploy/cloud/backup-remote.sh" \$(ls -1dt "\$REMOTE_ROOT"/releases/*/deploy/cloud/backup-remote.sh 2>/dev/null); do
  if [ -f "\$candidate" ]; then helper="\$candidate"; break; fi
done
if [ -n "\$helper" ]; then
  TRADE_OS_POSTGRES_ROOT="\$POSTGRES_ROOT" TRADE_OS_DATA_DIR="\$DATA_DIR" \
    bash "\$helper" "\$RELEASE_ID" $BUNDLE_FLAG
  exit \$?
fi

# ---- legacy inline fallback (ECS predates backup-remote.sh) ----
cd "\$POSTGRES_ROOT"
backup_log="/tmp/trosa-postgres-backup-\${RELEASE_ID}.database.log"
./backup.sh | tee "\$backup_log"
dump_rel="\$(sed -n 's/^backup=//p' "\$backup_log" | tail -n 1)"
database_sha="\$(sed -n 's/^sha256=//p' "\$backup_log" | tail -n 1)"
if [ -z "\$dump_rel" ] || [ -z "\$database_sha" ]; then
  printf '%s\\n' 'PostgreSQL backup did not return a dump path and checksum.' >&2
  exit 10
fi
if [ "\${dump_rel#/}" != "\$dump_rel" ]; then
  printf '%s\\n' 'Unexpected absolute PostgreSQL dump path.' >&2
  exit 11
fi
dump_path="\$POSTGRES_ROOT/\$dump_rel"
test -s "\$dump_path" || { printf '%s\\n' 'dump missing' >&2; exit 11; }
actual_sha="\$(sha256sum "\$dump_path" | awk '{print \$1}')"
[ "\$actual_sha" = "\$database_sha" ] || { printf '%s\\n' 'dump checksum mismatch' >&2; exit 11; }
(cd "\$POSTGRES_ROOT" && docker compose exec -T postgres pg_restore --list "/backups/\$(basename "\$dump_rel")") >/dev/null 2>&1 \\
  || { printf '%s\\n' 'dump is not restorable' >&2; exit 11; }
if [ "$USE_BUNDLE" = "1" ]; then
  staging="/tmp/trosa-postgres-backup-\${RELEASE_ID}"
  rm -rf "\$staging"; mkdir -p "\$staging"
  cp -- "\$dump_path" "\$staging/database.dump"
  if [ -d "\$DATA_DIR/uploads/customer_files" ]; then
    tar -C "\$DATA_DIR" -cf "\$staging/uploads.tar" uploads/customer_files
  fi
  { printf 'format=trosa-postgres-backup-v1\\n'
    printf 'database_dump=database.dump\\n'
    printf 'database_dump_sha256=%s\\n' "\$database_sha"
    if [ -f "\$staging/uploads.tar" ]; then printf 'attachments=uploads.tar\\n'; else printf 'attachments=none\\n'; fi
  } > "\$staging/manifest.txt"
  tar -C "\$staging" -czf "\$REMOTE_ARCHIVE" .
  rm -rf "\$staging"
  printf 'ARCHIVE=%s\\n' "\$REMOTE_ARCHIVE"
  printf 'SHA256=%s\\n' "\$(sha256sum "\$REMOTE_ARCHIVE" | awk '{print \$1}')"
fi
printf 'DATABASE_DUMP=%s\\n' "\$dump_path"
printf 'DATABASE_SHA256=%s\\n' "\$database_sha"
EOF
)

runner="${TRADE_OS_BACKUP_RUNNER:-$SCRIPT_DIR/run-workbench-command.sh}"
output="$(bash "$runner" "$TRADE_OS_ECS_INSTANCE_ID" "$TRADE_OS_ECS_REGION" "$remote_command" 2>&1)"
backup_rc=$?
[[ "$JSON_ONLY" == 1 ]] || printf '%s\n' "$output"

backup_json="$(printf '%s\n' "$output" | grep '^TROSA_BACKUP_JSON ' | tail -n 1 | sed 's/^TROSA_BACKUP_JSON //')"
remote_sha="$(printf '%s\n' "$output" | sed -n 's/^SHA256=//p' | tail -n 1)"

# Classify the cloud backup result. A non-zero helper exit means the cloud
# backup is not trustworthy, regardless of any transfer outcome.
if [[ "$backup_rc" != 0 ]]; then
  case "$backup_rc" in
    10) printf 'TROSA_BACKUP_STATUS backup_failed\n' >&2 ;;
    11|12) printf 'TROSA_BACKUP_STATUS backup_verification_failed\n' >&2 ;;
    *) printf 'TROSA_BACKUP_STATUS backup_failed\n' >&2 ;;
  esac
  printf '云端备份未完成（helper exit %s）；这不是本地下载问题。\n' "$backup_rc" >&2
  exit "$backup_rc"
fi
if [[ -n "$backup_json" ]]; then
  printf 'TROSA_BACKUP_STATUS ok\n'
  [[ "$JSON_ONLY" == 1 ]] || printf '云端备份已校验：%s\n' "$backup_json"
else
  # Legacy fallback prints no JSON but a DATABASE_SHA256 line.
  legacy_sha="$(printf '%s\n' "$output" | sed -n 's/^DATABASE_SHA256=//p' | tail -n 1)"
  if [[ -z "$legacy_sha" ]]; then
    printf 'TROSA_BACKUP_STATUS backup_verification_failed\n' >&2
    printf '云端备份未返回可校验结果。\n' >&2
    exit 11
  fi
  printf 'TROSA_BACKUP_STATUS ok\n'
fi

if [[ "$DOWNLOAD_MODE" == "never" ]]; then
  [[ "$JSON_ONLY" == 1 ]] || printf '跳过本地下载（--cloud-only / --download=never）。\n'
  exit 0
fi

if [[ -z "$remote_sha" ]]; then
  printf 'TROSA_LOCAL_ARCHIVE local_download_failed（远端未返回归档校验值）\n' >&2
  exit 20
fi

download_once() {
  local transport=$1
  # Test hook: a caller-supplied fetcher receives <remote> <local-dir> <transport>.
  if [[ -n "${TRADE_OS_BACKUP_FETCH:-}" ]]; then
    "$TRADE_OS_BACKUP_FETCH" "$REMOTE_ARCHIVE" "$LOCAL_BACKUP_ROOT" "$transport"
    return $?
  fi
  case "$transport" in
    ssh)
      [[ -n "${TRADE_OS_SSH_HOST:-}" ]] || return 127
      scp -o BatchMode=yes -o ConnectTimeout=10 \
        "${TRADE_OS_SSH_HOST}:${REMOTE_ARCHIVE}" "$LOCAL_BACKUP_ROOT/"
      ;;
    workbench)
      command -v workbench >/dev/null 2>&1 || return 127
      workbench download "$REMOTE_ARCHIVE" "$LOCAL_BACKUP_ROOT/" \
        --instance-id "$TRADE_OS_ECS_INSTANCE_ID" --region "$TRADE_OS_ECS_REGION" --force
      ;;
    *) return 2 ;;
  esac
}

download_with_retry() {
  local transport=$1 attempt
  for attempt in 1 2 3; do
    if download_once "$transport"; then return 0; fi
    sleep 2
  done
  return 1
}

downloaded=0
case "$TRANSFER" in
  ssh) download_with_retry ssh && downloaded=1 ;;
  workbench) download_with_retry workbench && downloaded=1 ;;
  auto)
    # Prefer the key-based SSH data path when configured (Workbench's session
    # relay can time out independently of port 22); fall back to Workbench.
    if [[ -n "${TRADE_OS_SSH_HOST:-}" ]] && download_with_retry ssh; then
      downloaded=1
    elif download_with_retry workbench; then
      downloaded=1
    elif [[ -n "${TRADE_OS_SSH_HOST:-}" ]] && download_with_retry ssh; then
      downloaded=1
    fi
    ;;
  *) printf '未知 TRADE_OS_BACKUP_TRANSFER=%s\n' "$TRANSFER" >&2; exit 2 ;;
esac

if [[ "$downloaded" != 1 ]]; then
  printf 'TROSA_LOCAL_ARCHIVE local_download_failed\n' >&2
  printf '本地归档下载失败（workbench/scp/SSH 文件流）；云端备份已完成并通过校验，发布不受影响。\n' >&2
  exit 20
fi

local_archive="$LOCAL_BACKUP_ROOT/$ARCHIVE_NAME"
if [[ ! -f "$local_archive" ]]; then
  printf 'TROSA_LOCAL_ARCHIVE local_download_failed（下载后找不到 %s）\n' "$local_archive" >&2
  exit 20
fi
local_sha="$(shasum -a 256 "$local_archive" | awk '{print $1}')"
if [[ "$local_sha" != "$remote_sha" ]]; then
  printf 'TROSA_LOCAL_ARCHIVE local_download_failed（校验不一致 remote=%s local=%s）\n' "$remote_sha" "$local_sha" >&2
  exit 20
fi
tar -tzf "$local_archive" >/dev/null 2>&1 || {
  printf 'TROSA_LOCAL_ARCHIVE local_download_failed（归档无法读取）\n' >&2
  exit 20
}

# Keep the rolling window of local archives (best effort).
find "$LOCAL_BACKUP_ROOT" -type f -name 'trosa-postgres-backup-*.tar.gz' -mtime +14 -delete 2>/dev/null || true

bash "$runner" "$TRADE_OS_ECS_INSTANCE_ID" "$TRADE_OS_ECS_REGION" "rm -f '$REMOTE_ARCHIVE'" >/dev/null 2>&1 || true

printf 'TROSA_LOCAL_ARCHIVE ok\n'
printf 'local backup=%s\nsha256=%s\n' "$local_archive" "$local_sha"
