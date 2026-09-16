#!/usr/bin/env bash
# Execute one bounded command through Alibaba Cloud Assistant, never SSH.
# The Cloud Assistant API authenticates the operator's RAM AccessKey; the
# command runs as the dedicated non-login trosa-operator account by default.
set -euo pipefail

if [[ "$#" != 3 ]]; then
  printf 'Usage: %s INSTANCE_ID REGION COMMAND\n' "$0" >&2
  exit 2
fi
instance_id=$1
region=$2
remote_command=$3
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
username="${TRADE_OS_CLOUD_ASSISTANT_USER:-trosa-operator}"
timeout="${TRADE_OS_CLOUD_ASSISTANT_TIMEOUT:-120}"

launch="$(python3 "$script_dir/cloud-assistant.py" run --region "$region" --instance-id "$instance_id" --username "$username" --timeout "$timeout" --command "$remote_command")"
read -r invoke_id command_id < <(printf '%s' "$launch" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["InvokeId"], d["CommandId"])')
deadline=$((SECONDS + timeout + 30))
while (( SECONDS < deadline )); do
  response="$(python3 "$script_dir/cloud-assistant.py" get --region "$region" --invoke-id "$invoke_id" --command-id "$command_id")"
  result="$(printf '%s' "$response" | python3 -c '
import base64,json,sys
d=json.load(sys.stdin); rows=d.get("Invocations",{}).get("Invocation",[])
if not rows: print("PENDING"); raise SystemExit
i=rows[0]; per=i.get("InvokeInstances",{}).get("InvokeInstance",[])
if not per: print("PENDING"); raise SystemExit
r=per[0]; status=r.get("InvocationStatus", "Pending")
if status in {"Pending", "Running", "Stopping"}: print("PENDING"); raise SystemExit
out=base64.b64decode(r.get("Output", "")).decode("utf-8", "replace")
print("DONE", r.get("ExitCode", 1), base64.b64encode(out.encode()).decode())
')"
  if [[ "$result" != PENDING* ]]; then
    read -r marker exit_code payload <<<"$result"
    printf '%s' "$payload" | base64 -d
    [[ "$exit_code" == 0 ]] && exit 0
    exit "$exit_code"
  fi
  sleep 2
done
printf 'Cloud Assistant invocation is still pending: invoke_id=%s command_id=%s\n' "$invoke_id" "$command_id" >&2
exit 124
