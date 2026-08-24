#!/usr/bin/env bash
# Run one command through Workbench, including its interactive SSM fallback.
set -euo pipefail

if [ "$#" -ne 3 ]; then
  printf 'Usage: %s INSTANCE_ID REGION COMMAND\n' "$0" >&2
  exit 2
fi

instance_id=$1
region=$2
remote_command=$3
workbench_bin=$(command -v workbench || true)
if [ -z "$workbench_bin" ]; then
  printf 'Workbench CLI not found in PATH\n' >&2
  exit 127
fi

# Workbench's SSM mode exposes an interactive shell but rejects its
# non-interactive `exec` command. Compress the script into one slowly typed
# shell line so the terminal transport cannot corrupt multiline commands.
if command -v expect >/dev/null 2>&1; then
  command_payload=$(printf '%s\n' "$remote_command" | gzip -c | base64 | tr -d '\n')
  export TROSA_WORKBENCH_BIN="$workbench_bin"
  export TROSA_WORKBENCH_INSTANCE_ID="$instance_id"
  export TROSA_WORKBENCH_REGION="$region"
  export TROSA_WORKBENCH_COMMAND_PAYLOAD="$command_payload"
  expect <<'EOF'
set timeout 900
set workbench_bin $env(TROSA_WORKBENCH_BIN)
set instance_id $env(TROSA_WORKBENCH_INSTANCE_ID)
set region $env(TROSA_WORKBENCH_REGION)
set command_payload $env(TROSA_WORKBENCH_COMMAND_PAYLOAD)
# Split the completion marker in the sent command so Expect cannot mistake the
# shell's local echo of the command for the marker produced by the server.
set command "printf '%s' '$command_payload' | base64 -d | gzip -d | bash; status=\$?; printf 'TROSA_MANAGER_'; printf 'COMMAND_END exit=%s\\n' \$status"
set send_slow {10 .001}

spawn $workbench_bin connect --instance-id $instance_id --region $region --user-name root --new
expect {
    -re {root@[^#\r\n]+# } {}
    timeout { puts stderr "Timed out waiting for the Workbench remote shell"; exit 1 }
    eof { puts stderr "Workbench session closed before the remote shell was ready"; exit 1 }
}
send -s -- "$command\r"
expect {
    -re {TROSA_MANAGER_COMMAND_END exit=[0-9]+} {}
    timeout { puts stderr "Timed out waiting for the Workbench command"; exit 1 }
    eof { puts stderr "Workbench session closed before the command completed"; exit 1 }
}
send "\004"
expect eof
EOF
else
  "$workbench_bin" exec \
    --instance-id "$instance_id" \
    --region "$region" \
    --user-name root \
    --command "$remote_command"
fi
