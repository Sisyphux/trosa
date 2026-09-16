#!/usr/bin/env bash
# Run once through Cloud Assistant as root, never through SSH.  It creates the
# non-login account used by regular Trosa Cloud Assistant commands and exposes
# exactly one sudo entrypoint for the existing release runner.
set -euo pipefail

install -d -m 0750 -o root -g root /usr/local/lib/trosa
if ! id -u trosa-operator >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/trosa-operator \
    --shell /usr/sbin/nologin --user-group trosa-operator
fi
usermod -a -G systemd-journal trosa-operator

install -m 0750 -o root -g root /dev/stdin /usr/local/lib/trosa/release-runner <<'RUNNER'
#!/usr/bin/env bash
set -euo pipefail
[[ "$#" == 7 ]] || { echo 'expected: root service release commit github mode destructive' >&2; exit 2; }
remote_root=$1 service_name=$2 release_id=$3 commit=$4 github_remote=$5 mode=$6 allow_destructive=$7
[[ "$remote_root" == /opt/trade-os ]] || exit 2
[[ "$service_name" == trade-os ]] || exit 2
[[ "$release_id" =~ ^[A-Za-z0-9._-]+$ ]] || exit 2
[[ "$commit" =~ ^[0-9a-fA-F]{40}$ ]] || exit 2
[[ "$github_remote" == https://github.com/Sisyphux/trosa ]] || exit 2
[[ "$mode" == publish || "$mode" == rollback ]] || exit 2
[[ "$allow_destructive" == 0 || "$allow_destructive" == 1 ]] || exit 2
runner_file="/tmp/trosa-release-remote-${release_id}.sh"
runner_url="https://raw.githubusercontent.com/Sisyphux/trosa/${commit}/deploy/cloud/release-remote.sh"
curl --fail --location --silent --show-error --max-time 60 "$runner_url" -o "$runner_file"
chmod 0700 "$runner_file"
setsid nohup bash "$runner_file" "$remote_root" "$service_name" "$release_id" "$commit" "$github_remote" "$mode" "$allow_destructive" \
  >"/tmp/trosa-release-${release_id}.launch.log" 2>&1 </dev/null &
printf 'launched %s mode=%s\n' "$release_id" "$mode"
RUNNER

install -m 0440 -o root -g root /dev/stdin /etc/sudoers.d/trosa-cloud-assistant <<'SUDOERS'
Defaults:trosa-operator !requiretty
trosa-operator ALL=(root) NOPASSWD: /usr/local/lib/trosa/release-runner
SUDOERS
visudo -cf /etc/sudoers.d/trosa-cloud-assistant
printf 'TROSA_CLOUD_ASSISTANT_BOOTSTRAP_OK user=trosa-operator\n'
