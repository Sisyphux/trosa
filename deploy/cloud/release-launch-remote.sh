#!/usr/bin/env bash
# Server-side release launcher. trosa-release sends this file over SSH and runs
# it as root:  sudo -n bash -s -- ARGS <<'TROSA_LAUNCH' ... TROSA_LAUNCH
#
# It does three things and returns at once:
#   1. validate the arguments (the only accepted shape of a release request);
#   2. download release-remote.sh from the public GitHub archive of that exact
#      commit;
#   3. start that runner detached (setsid nohup) so it survives the SSH session.
# Backup, migration, switch, health check and rollback all run inside
# release-remote.sh, and the operator polls its result file.
#
# Nothing has to be installed on the server ahead of time: this file travels
# with the operator's checkout, and the runner it starts comes from the commit
# being released.
#
# Usage (sent by trosa-release, never by hand):
#   release-launch-remote.sh REMOTE_ROOT SERVICE_NAME RELEASE_ID COMMIT_SHA GITHUB_REMOTE MODE ALLOW_DESTRUCTIVE
set -euo pipefail

if [[ "$#" != 7 ]]; then
  echo 'expected: remote_root service release commit github mode destructive' >&2
  exit 2
fi
remote_root=$1 service_name=$2 release_id=$3 commit=$4 github_remote=$5 mode=$6 allow_destructive=$7
[[ "$remote_root" == /opt/trade-os ]] || exit 2
[[ "$service_name" == trade-os ]] || exit 2
[[ "$release_id" =~ ^[A-Za-z0-9._-]+$ ]] || exit 2
[[ "$commit" =~ ^[0-9a-fA-F]{40}$ ]] || exit 2
[[ "$github_remote" == https://github.com/Sisyphux/trosa ]] || exit 2
[[ "$mode" == publish || "$mode" == rollback ]] || exit 2
[[ "$allow_destructive" == 0 || "$allow_destructive" == 1 ]] || exit 2
if [[ "$mode" == publish ]]; then mode=deploy; fi

runner_file="/tmp/trosa-release-remote-${release_id}.sh"
runner_url="https://raw.githubusercontent.com/Sisyphux/trosa/${commit}/deploy/cloud/release-remote.sh"
curl --fail --location --silent --show-error --max-time 60 "$runner_url" -o "$runner_file"
chmod 0700 "$runner_file"
setsid nohup bash "$runner_file" "$remote_root" "$service_name" "$release_id" "$commit" "$github_remote" "$mode" "$allow_destructive" \
  >"/tmp/trosa-release-${release_id}.launch.log" 2>&1 </dev/null &
printf 'launched %s mode=%s\n' "$release_id" "$mode"
