#!/usr/bin/env bash
# Run one command through key-based SSH when configured, otherwise Workbench.
set -euo pipefail

if [ "$#" -ne 3 ]; then
  printf 'Usage: %s INSTANCE_ID REGION COMMAND\n' "$0" >&2
  exit 2
fi

instance_id=$1
region=$2
remote_command=$3

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${TRADE_OS_WORKBENCH_ENV:-$SCRIPT_DIR/workbench.env}"
if [[ -z "${TRADE_OS_SSH_HOST:-}" && -r "$ENV_FILE" ]]; then
  # This optional local setting contains only an SSH host alias, never a
  # password or cloud credential.
  source "$ENV_FILE"
fi

prefer_workbench="$(/usr/bin/printenv TRADE_OS_PREFER_WORKBENCH || true)"
ssh_host="$(/usr/bin/printenv TRADE_OS_SSH_HOST || true)"
if [[ "$prefer_workbench" != "1" && -n "$ssh_host" ]]; then
  ssh_bin="$(command -v ssh || true)"
  if [[ -z "$ssh_bin" ]]; then
    printf 'SSH client not found in PATH\n' >&2
    exit 127
  fi
  exec "$ssh_bin" -o BatchMode=yes -o ConnectTimeout=10 "$ssh_host" "$remote_command"
fi

workbench_bin=$(command -v workbench || true)
if [ -z "$workbench_bin" ]; then
  printf 'Workbench CLI not found in PATH\n' >&2
  exit 127
fi

# Workbench now supports a non-interactive command on the current CLI. Prefer
# it for read-only status checks so a blocked local SSH port does not make the
# monitoring page look broken. If an older CLI or an SSM-only session rejects
# exec, continue to the interactive compatibility path below.
if [[ "$prefer_workbench" == "1" ]]; then
  if output="$("$workbench_bin" exec \
      --instance-id "$instance_id" \
      --region "$region" \
      --user-name root \
      --timeout 120 \
      --command "$remote_command" 2>&1)"; then
    printf '%s\n' "$output"
    exit 0
  else
    exec_status=$?
    printf 'Workbench 非交互检查失败（退出码 %s），改用交互式会话重试。\n' "$exec_status" >&2
    printf '%s\n' "$output" >&2
  fi
fi

# Workbench's SSM mode exposes an interactive shell but rejects its
# non-interactive `exec` command. Compress the script into one slowly typed
# shell line so the terminal transport cannot corrupt multiline commands.
if command -v expect >/dev/null 2>&1; then
  expect_timeout="$(/usr/bin/printenv TROSA_WORKBENCH_EXPECT_TIMEOUT || true)"
  if [[ -z "$expect_timeout" ]]; then
    expect_timeout=900
  fi
  command_payload=$(printf '%s\n' "$remote_command" | gzip -c | base64 | tr -d '\n')
  export TROSA_WORKBENCH_BIN="$workbench_bin"
  export TROSA_WORKBENCH_INSTANCE_ID="$instance_id"
  export TROSA_WORKBENCH_REGION="$region"
  export TROSA_WORKBENCH_COMMAND_PAYLOAD="$command_payload"
  export TROSA_WORKBENCH_EXPECT_TIMEOUT="$expect_timeout"
  expect <<'EOF'
set timeout $env(TROSA_WORKBENCH_EXPECT_TIMEOUT)
set workbench_bin $env(TROSA_WORKBENCH_BIN)
set instance_id $env(TROSA_WORKBENCH_INSTANCE_ID)
set region $env(TROSA_WORKBENCH_REGION)
set command_payload $env(TROSA_WORKBENCH_COMMAND_PAYLOAD)
# Split the completion marker in the sent command so Expect cannot mistake the
# shell's local echo of the command for the marker produced by the server.
set command "printf '%s' '$command_payload' | base64 -d | gzip -d | bash; status=\$?; printf 'TROSA_MANAGER_'; printf 'COMMAND_END exit=%s\\n' \$status"
set send_slow {1 .003}
set command_exit 1

spawn $workbench_bin connect --instance-id $instance_id --region $region --user-name root --new
expect {
    -re {root@[^#\r\n]+# } {}
    timeout { puts stderr "Timed out waiting for the Workbench remote shell"; exit 1 }
    eof { puts stderr "Workbench session closed before the remote shell was ready"; exit 1 }
}
send -s -- "$command\r"
expect {
    -re {TROSA_MANAGER_COMMAND_END exit=([0-9]+)} { set command_exit $expect_out(1,string) }
    timeout { puts stderr "Timed out waiting for the Workbench command"; exit 1 }
    eof { puts stderr "Workbench session closed before the command completed"; exit 1 }
}
send "\004"
expect eof
exit $command_exit
EOF
else
  "$workbench_bin" exec \
    --instance-id "$instance_id" \
    --region "$region" \
    --user-name root \
    --command "$remote_command"
fi
