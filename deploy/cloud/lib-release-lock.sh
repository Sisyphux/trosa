#!/usr/bin/env bash
# Portable, sourceable locking primitives for Trosa's multi-Agent workflow.
#
# Why this is not `flock`
# ----------------------
# Development and release-candidate assembly happen on macOS, where `flock` is
# not part of the base system.  ECS (Linux) uses `flock` for the production
# release lock, but the local workflows (task migration-number reservation and
# local release serialization) need a lock that works everywhere and survives a
# crashed holder.
#
# The lock is a directory created with `mkdir` (atomic on every POSIX file
# system) that carries an ``owner`` file with ``<pid> <unix-seconds>``.  A lock
# whose owner process is gone -- or whose timestamp is older than the staleness
# window -- is reclaimed, so a `kill -9` cannot wedge the workflow forever.
#
# Sourcing this file has no side effects.  Callers are expected to run with
# ``set -euo pipefail`` and to handle non-zero returns themselves.
#
# API
# ---
#   trosa_lock_acquire LOCK_DIR [TIMEOUT_SECONDS] [STALE_SECONDS]
#       -> 0 acquired, 1 timeout.  Creates LOCK_DIR's parent when needed.
#   trosa_lock_release LOCK_DIR
#   trosa_next_migration_number MAIN_ROOT TASK_META_DIR
#       -> prints the next four-digit migration number.  The caller must hold
#          the reservation lock while computing *and* persisting it.

trosa_lock_owner_file() { printf '%s/owner' "$1"; }

trosa_lock_pid_alive() {
  local pid=$1
  [ -n "$pid" ] || return 1
  case "$pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  kill -0 "$pid" 2>/dev/null
}

# Acquire LOCK_DIR.  TIMEOUT bounds how long we wait for a live holder;
# STALE bounds how long a lock with an unreadable/dead owner may persist.
trosa_lock_acquire() {
  local lock_dir=$1
  local timeout=${2:-30}
  local stale=${3:-600}
  local owner pid ts now start
  [ -n "$lock_dir" ] || return 1
  mkdir -p -- "$(dirname "$lock_dir")" 2>/dev/null || true
  start=$(date +%s)
  while :; do
    if mkdir -- "$lock_dir" 2>/dev/null; then
      printf '%s %s\n' "$$" "$(date +%s)" >"$(trosa_lock_owner_file "$lock_dir")" 2>/dev/null || true
      return 0
    fi
    owner=$(cat "$(trosa_lock_owner_file "$lock_dir")" 2>/dev/null || true)
    pid=""
    ts=""
    if [ -n "$owner" ]; then
      read -r pid ts _ <<<"$owner" || true
    fi
    if [ -n "$pid" ] && ! trosa_lock_pid_alive "$pid"; then
      rm -rf -- "$lock_dir" 2>/dev/null || true
      continue
    fi
    now=$(date +%s)
    if [ -n "$ts" ] && [ "$ts" -eq "$ts" ] 2>/dev/null && [ $((now - ts)) -ge "$stale" ]; then
      rm -rf -- "$lock_dir" 2>/dev/null || true
      continue
    fi
    if [ $((now - start)) -ge "$timeout" ]; then
      return 1
    fi
    sleep 0.1
  done
}

trosa_lock_release() {
  [ -n "${1:-}" ] || return 0
  rm -rf -- "$1" 2>/dev/null || true
}

# Print the next free migration number.  Scans every worktree of MAIN_ROOT plus
# already-reserved task metadata, so two concurrent tasks that hold the
# reservation lock can never be handed the same number.  A single Python
# process does the scan so subshells cannot lose the running maximum.
trosa_next_migration_number() {
  local main_root=$1 meta_dir=$2
  python3 - "$main_root" "$meta_dir" <<'PY'
import glob
import json
import os
import subprocess
import sys

main_root, meta_dir = sys.argv[1], sys.argv[2]
numbers = []

def take(name):
    prefix = name.split("_", 1)[0]
    if prefix.isdigit():
        numbers.append(int(prefix))

try:
    listing = subprocess.check_output(
        ["git", "-C", main_root, "worktree", "list", "--porcelain"],
        text=True, stderr=subprocess.DEVNULL,
    )
except Exception:
    listing = ""
for line in listing.splitlines():
    if not line.startswith("worktree "):
        continue
    wt = line[len("worktree "):]
    mdir = os.path.join(wt, "migrations")
    if not os.path.isdir(mdir):
        continue
    for path in glob.glob(os.path.join(mdir, "*.sql")):
        take(os.path.basename(path))

if os.path.isdir(meta_dir):
    for path in sorted(glob.glob(os.path.join(meta_dir, "*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
        except (OSError, ValueError):
            continue
        value = str(doc.get("reserved_migration") or "")
        if value.isdigit():
            numbers.append(int(value))

print(f"{max(numbers, default=0) + 1:04d}")
PY
}
