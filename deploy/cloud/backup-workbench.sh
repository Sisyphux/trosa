#!/usr/bin/env bash
# Create and verify a PostgreSQL + attachment backup on the production server
# (over SSH), then OPTIONALLY download it to the Mac.
#
# Why this was rewritten: the release gate used to run this script before every
# database-sensitive publish, and any failure to transfer the archive to the Mac
# (scp / SSH file stream) blocked the release even though a perfectly good,
# verified backup already existed on the server. The download is now optional
# and can never block a release.
#
# Two independent outcomes, never conflated:
#   1. Server backup (generation + checksum + restorability + optional offsite
#      mirror) — this is the safety gate. Failure exits 10/11/12.
#   2. Local archive download — a convenience. Failure exits 20
#      (``local_download_failed``) and does not mean the backup failed.
#
# Usage:
#   backup-workbench.sh [--cloud-only] [--download=auto|require|never] [--json]
#
#   --cloud-only           只在服务器生成并校验备份，不下载。
#   --download=auto         尝试下载；失败打印 LOCAL_DOWNLOAD_FAILED 并以 20 退出。
#   --download=require      同 auto；调用方可据此把本地归档视为必需。
#   --download=never        明确不下载。
#
# Exit codes:
#   0   ok
#   10  backup_failed
#   11  backup_verification_failed
#   12  backup_verification_failed (configured offsite mirror failed)
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

SSH_HOST="$(trosa_ssh_host)"
DATA_DIR="${TRADE_OS_DATA_DIR:-/var/lib/trade-os}"
POSTGRES_ROOT="${TRADE_OS_POSTGRES_ROOT:-/opt/trade-os-postgres}"
REMOTE_ROOT="${TRADE_OS_REMOTE_ROOT:-/opt/trade-os}"
LOCAL_BACKUP_ROOT="${TRADE_OS_LOCAL_BACKUP_DIR:-$HOME/Library/Application Support/trosa/backups}"
STAMP="$(date -u +%Y%m%d%H%M%S)"
RELEASE_ID="${STAMP}"
ARCHIVE_NAME="trosa-postgres-backup-${STAMP}.tar.gz"
# The server-side helper derives its bundle path from RELEASE_ID and returns it
# on an ARCHIVE= line; this default only applies if that line is missing.
REMOTE_ARCHIVE="/tmp/${ARCHIVE_NAME}"
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

# The verified-backup implementation is backup-remote.sh from the deployed
# release (current, else the newest release that has it). The backup runner and
# the PostgreSQL dump belong to root, so the whole remote step runs under sudo;
# the bundle is handed back to the SSH user so it can be copied down.
remote_command=$(cat <<EOF
sudo -n bash -s <<'TROSA_BACKUP'
set -uo pipefail
umask 077
REMOTE_ROOT='$REMOTE_ROOT'
POSTGRES_ROOT='$POSTGRES_ROOT'
DATA_DIR='$DATA_DIR'
RELEASE_ID='$RELEASE_ID'
BUNDLE_PATH="/tmp/trosa-postgres-backup-\$RELEASE_ID.tar.gz"
# Failed downloads can leave bundles behind; keep /tmp bounded.
find /tmp -maxdepth 1 -name 'trosa-postgres-backup-*.tar.gz' -mtime +2 -delete 2>/dev/null || true
helper=''
for candidate in "\$REMOTE_ROOT/current/deploy/cloud/backup-remote.sh" \$(ls -1dt "\$REMOTE_ROOT"/releases/*/deploy/cloud/backup-remote.sh 2>/dev/null); do
  if [ -f "\$candidate" ]; then helper="\$candidate"; break; fi
done
if [ -z "\$helper" ]; then
  printf '%s\\n' 'backup-remote.sh is missing from every deployed release' >&2
  exit 10
fi
TRADE_OS_POSTGRES_ROOT="\$POSTGRES_ROOT" TRADE_OS_DATA_DIR="\$DATA_DIR" \
  bash "\$helper" "\$RELEASE_ID" $BUNDLE_FLAG
rc=\$?
if [ -f "\$BUNDLE_PATH" ]; then chown "\${SUDO_USER:-root}" "\$BUNDLE_PATH"; fi
exit \$rc
TROSA_BACKUP
EOF
)

runner="${TRADE_OS_BACKUP_RUNNER:-$SCRIPT_DIR/run-ssh-command.sh}"
output="$(bash "$runner" "$SSH_HOST" "$remote_command" 2>&1)"
backup_rc=$?
[[ "$JSON_ONLY" == 1 ]] || printf '%s\n' "$output"

backup_json="$(printf '%s\n' "$output" | grep '^TROSA_BACKUP_JSON ' | tail -n 1 | sed 's/^TROSA_BACKUP_JSON //')"
remote_sha="$(printf '%s\n' "$output" | sed -n 's/^SHA256=//p' | tail -n 1)"
remote_archive="$(printf '%s\n' "$output" | sed -n 's/^ARCHIVE=//p' | tail -n 1)"
# The helper names the bundle after its release id; trust the returned path.
[[ -n "$remote_archive" ]] && REMOTE_ARCHIVE="$remote_archive"

# Classify the server backup result. A non-zero helper exit means the backup is
# not trustworthy, regardless of any transfer outcome.
if [[ "$backup_rc" != 0 ]]; then
  case "$backup_rc" in
    10) printf 'TROSA_BACKUP_STATUS backup_failed\n' >&2 ;;
    11|12) printf 'TROSA_BACKUP_STATUS backup_verification_failed\n' >&2 ;;
    *) printf 'TROSA_BACKUP_STATUS backup_failed\n' >&2 ;;
  esac
  printf '服务器备份未完成（helper exit %s）；这不是本地下载问题。\n' "$backup_rc" >&2
  exit "$backup_rc"
fi
if [[ -z "$backup_json" ]]; then
  printf 'TROSA_BACKUP_STATUS backup_verification_failed\n' >&2
  printf '服务器备份未返回可校验结果。\n' >&2
  exit 11
fi
printf 'TROSA_BACKUP_STATUS ok\n'
[[ "$JSON_ONLY" == 1 ]] || printf '服务器备份已校验：%s\n' "$backup_json"

if [[ "$DOWNLOAD_MODE" == "never" ]]; then
  [[ "$JSON_ONLY" == 1 ]] || printf '跳过本地下载（--cloud-only / --download=never）。\n'
  exit 0
fi

if [[ -z "$remote_sha" ]]; then
  printf 'TROSA_LOCAL_ARCHIVE local_download_failed（远端未返回归档校验值）\n' >&2
  exit 20
fi

# Bound every transfer attempt. macOS has no coreutils ``timeout``; a release
# must never wait on the optional local archive indefinitely.
run_with_timeout() {
  local limit=$1; shift
  "$@" &
  local pid=$! waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [[ "$waited" -ge "$limit" ]]; then
      kill -TERM "$pid" 2>/dev/null || true
      sleep 1
      kill -KILL "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      return 124
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$pid"
}

download_once() {
  local attempt_timeout="${TRADE_OS_BACKUP_DOWNLOAD_TIMEOUT:-60}"
  # Test hook: a caller-supplied fetcher receives <remote> <local-dir>.
  if [[ -n "${TRADE_OS_BACKUP_FETCH:-}" ]]; then
    run_with_timeout "$attempt_timeout" "$TRADE_OS_BACKUP_FETCH" "$REMOTE_ARCHIVE" "$LOCAL_BACKUP_ROOT"
    return $?
  fi
  run_with_timeout "$attempt_timeout" scp -o BatchMode=yes -o ConnectTimeout=10 \
    "${SSH_HOST}:${REMOTE_ARCHIVE}" "$LOCAL_BACKUP_ROOT/"
}

# The expected local path once the transfer finishes.
EXPECTED_LOCAL="$LOCAL_BACKUP_ROOT/$(basename "$REMOTE_ARCHIVE")"

# A transfer is successful when the artifact on disk is complete and matches
# the server-reported checksum. Do not trust scp's exit status alone: it can
# leave a fully written file behind and then hang, which the watchdog kills.
local_archive_verified() {
  [[ -f "$EXPECTED_LOCAL" ]] || return 1
  [[ "$(shasum -a 256 "$EXPECTED_LOCAL" | awk '{print $1}')" == "$remote_sha" ]]
}

download_with_retry() {
  local attempt
  for attempt in 1 2; do
    if download_once; then return 0; fi
    if local_archive_verified; then return 0; fi
    sleep 2
  done
  local_archive_verified
}

downloaded=0
if download_with_retry; then downloaded=1; fi

if [[ "$downloaded" != 1 ]]; then
  printf 'TROSA_LOCAL_ARCHIVE local_download_failed\n' >&2
  printf '本地归档下载失败（scp）；服务器备份已完成并通过校验，发布不受影响。\n' >&2
  exit 20
fi

local_archive="$EXPECTED_LOCAL"
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

bash "$runner" "$SSH_HOST" "rm -f '$REMOTE_ARCHIVE'" >/dev/null 2>&1 || true

printf 'TROSA_LOCAL_ARCHIVE ok\n'
printf 'local backup=%s\nsha256=%s\n' "$local_archive" "$local_sha"
