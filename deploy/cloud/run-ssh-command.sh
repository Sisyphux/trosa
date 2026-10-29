#!/usr/bin/env bash
# Execute one bounded command on the production server over SSH.
#
# Host routing lives in the SSH config (default alias: trosa-new). The key is
# the operator's own SSH key, read by ssh itself; this script never sees or
# stores a secret. The remote user is set by that alias (ubuntu on trosa-new).
#
# Outcome contract (exit codes), kept identical to the old transport so every
# caller keeps the same three-way semantics:
#   <remote exit code>  the command ran to completion and produced an exit code.
#   2                   the SSH session was never established (name resolution,
#                       refused connection, auth or host-key failure). The
#                       command did not run; safe to resubmit.
#   124                 the session may have been established and the outcome
#                       is UNKNOWN. Callers MUST NOT treat this as "not
#                       launched"; they re-query by release id (check/status)
#                       or rely on server-side idempotency.
set -euo pipefail

if [[ "$#" != 2 ]]; then
  printf 'Usage: %s HOST COMMAND\n' "$0" >&2
  exit 2
fi
host=$1
remote_command=$2

ssh_bin="$(command -v ssh || true)"
if [[ -z "$ssh_bin" ]]; then
  printf 'SSH client not found in PATH; command was not sent.\n' >&2
  exit 2
fi

# The command travels base64-encoded so quoting never reaches the login shell.
# It runs under bash -c with stdin closed, so nested tools (docker compose,
# pg_dump, psql) cannot consume the rest of the transport stream.
payload="$(printf '%s' "$remote_command" | base64 | tr -d '\n')"
stderr_file="$(mktemp "${TMPDIR:-/tmp}/trosa-ssh-run.XXXXXX")"
trap 'rm -f "$stderr_file"' EXIT

rc=0
"$ssh_bin" -T -o BatchMode=yes -o ConnectTimeout=10 \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
  "$host" "bash -c \"\$(printf '%s' '$payload' | base64 -d)\" </dev/null" \
  2>"$stderr_file" || rc=$?

cat "$stderr_file" >&2

if [[ "$rc" == 255 ]]; then
  # ssh itself failed (exit 255 is also what a remote command may return, so
  # only the ssh-side messages below are treated as "never connected").
  if grep -q -E '^ssh: connect to host|^Could not resolve hostname|Permission denied \(publickey|Host key verification failed|No route to host' "$stderr_file"; then
    printf 'SSH session was not established; the command was not sent.\n' >&2
    exit 2
  fi
  printf 'SSH outcome unknown (connection failed after the session may have started).\n' >&2
  exit 124
fi
exit "$rc"
