#!/usr/bin/env bash
# Execute one bounded command through Alibaba Cloud Assistant, never SSH.
# The Cloud Assistant API authenticates the operator's RAM AccessKey; the
# command runs as the dedicated non-login trosa-operator account by default.
#
# Outcome contract (exit codes):
#   0 / <remote exit code>  terminal: command ran and produced an exit code.
#   2                       the command was rejected before submission
#                           (Cloud Assistant API refused); safe to resubmit.
#   124                     submitted but outcome UNKNOWN: the invocation was
#                           accepted (or acceptance is ambiguous) and no
#                           terminal state could be read before the deadline.
#                           Callers MUST NOT treat this as "not launched".
#                           They should re-query by release id (check/status)
#                           or rely on server-side idempotency.
set -euo pipefail

if [[ "$#" != 3 ]]; then
  printf 'Usage: %s INSTANCE_ID REGION COMMAND\n' "$0" >&2
  exit 2
fi
instance_id=$1
region=$2
remote_command=$3
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
client="${TRADE_OS_CLOUD_ASSISTANT_CLIENT:-$script_dir/cloud-assistant.py}"
username="${TRADE_OS_CLOUD_ASSISTANT_USER:-trosa-operator}"
timeout="${TRADE_OS_CLOUD_ASSISTANT_TIMEOUT:-120}"

# ClientToken makes a re-submission of this exact command idempotent, so a
# bounded retry after an ambiguous network failure is always safe.
client_token="$(python3 -c 'import uuid;print(uuid.uuid4())')"

run_err="$(mktemp /tmp/trosa-ca-run.XXXXXX)"
trap 'rm -f "$run_err"' EXIT
if ! launch="$(python3 "$client" run --region "$region" --instance-id "$instance_id" --username "$username" --timeout "$timeout" --client-token "$client_token" --command "$remote_command" 2>"$run_err")"; then
  # cloud-assistant.py already retried transient failures.  An ambiguous
  # network death here does NOT prove the command was rejected: keep the
  # unknown semantics instead of a "not launched" failure.  An explicit API
  # rejection (HTTP 4xx, bad credentials) stays a hard "not submitted".
  if grep -q '^CLOUD_ASSISTANT_AMBIGUOUS' "$run_err" 2>/dev/null; then
    printf 'Cloud Assistant submission outcome unknown (transport failed before InvokeId was returned)\n' >&2
    exit 124
  fi
  printf 'Cloud Assistant rejected the command before submission:\n' >&2
  cat "$run_err" >&2
  exit 2
fi
read -r invoke_id command_id < <(printf '%s' "$launch" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["InvokeId"], d["CommandId"])')
# From here on the invocation was accepted by Cloud Assistant: every later
# failure is "result unknown", never "command failed / not launched".
grace="${TRADE_OS_CLOUD_ASSISTANT_POLL_GRACE:-30}"
deadline=$((SECONDS + timeout + grace))
while (( SECONDS < deadline )); do
  if ! response="$(python3 "$client" get --region "$region" --invoke-id "$invoke_id" --command-id "$command_id" 2>/dev/null)"; then
    # DescribeInvocations failed (network/auth blip). The invocation itself is
    # still accepted and running server-side: retry, never abort here.
    sleep 2
    continue
  fi
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
' 2>/dev/null || printf 'PENDING')"
  if [[ "$result" != PENDING* ]]; then
    read -r marker exit_code payload <<<"$result"
    printf '%s' "$payload" | base64 -d
    [[ "$exit_code" == 0 ]] && exit 0
    exit "$exit_code"
  fi
  sleep 2
done
# Accepted but never terminal before the deadline: report "submitted, unknown"
# with the ids so callers can re-query the same invocation later.
printf 'Cloud Assistant invocation is still pending: invoke_id=%s command_id=%s\n' "$invoke_id" "$command_id" >&2
printf 'CLOUD_ASSISTANT_PENDING invoke_id=%s command_id=%s\n' "$invoke_id" "$command_id" >&2
exit 124
