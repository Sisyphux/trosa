#!/usr/bin/env bash
# Read cloud status and resources. This file is executed on ECS by Workbench.
set -u

app_status=$(systemctl is-active trade-os 2>/dev/null || true)
tunnel_status=$(systemctl is-active cloudflared 2>/dev/null || true)
health_status=down
ping_body=$(curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8080/api/network/ping 2>/dev/null || true)
app_body=$(curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8080/ 2>/dev/null || true)
if [ -n "$ping_body" ] \
  && printf '%s' "$ping_body" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"' \
  && printf '%s' "$ping_body" | grep -q '"backend"[[:space:]]*:[[:space:]]*"postgresql"' \
  && printf '%s' "$ping_body" | grep -q '"runtime_contract"[[:space:]]*:[[:space:]]*"trosa-postgresql-v1"' \
  && printf '%s' "$ping_body" | grep -q '"formal_runtime"[[:space:]]*:[[:space:]]*true' \
  && printf '%s' "$app_body" | grep -qi '<!DOCTYPE html>'; then
  health_status=ok
fi
release=$(readlink -f /opt/trade-os/current 2>/dev/null || true)
release=${release##*/}
if [ -z "$release" ]; then release=none; fi
printf 'TROSA_MANAGER_STATUS app=%s tunnel=%s health=%s release=%s\n' "$app_status" "$tunnel_status" "$health_status" "$release"

# Machine-readable deployment record for trosa-release status --json.
# Always emitted; values fall back to explicit "unknown" instead of guessing.
if [ -f /opt/trade-os/.deploy-state.json ]; then
  deploy_state=$(cat /opt/trade-os/.deploy-state.json 2>/dev/null | tr -d '\n' || true)
else
  deploy_state=''
fi
if [ -z "$deploy_state" ]; then
  deploy_state='{"production":{"id":"'"$release"'","commit":"unknown"},"previous":{"id":"unknown","commit":"unknown"},"previous_healthy":{"id":"unknown","commit":"unknown"},"deploy_state":"legacy","note":"no .deploy-state.json yet; production pointer is the current symlink"}'
fi
if [ -f /opt/trade-os/.last-deploy-result.json ]; then
  last_result=$(cat /opt/trade-os/.last-deploy-result.json 2>/dev/null | tr -d '\n' || true)
else
  last_result='null'
fi
if [ -f "/opt/trade-os/releases/$release/release.json" ]; then
  release_manifest=$(cat "/opt/trade-os/releases/$release/release.json" 2>/dev/null | tr -d '\n' || true)
else
  release_manifest='null'
fi
migration_ledger='null'
if [ -n "${TRADE_OS_DATABASE_URL:-}" ] && [ -f /etc/trade-os/trade-os.env ]; then
  set -a
  # shellcheck disable=SC1091
  . /etc/trade-os/trade-os.env 2>/dev/null || true
  set +a
fi
if [ -n "${TRADE_OS_DATABASE_URL:-}" ]; then
  migration_ledger=$(python3 - <<'PYEOF' 2>/dev/null || printf 'null'
import json, os
try:
    import psycopg
    with psycopg.connect(os.environ["TRADE_OS_DATABASE_URL"]) as conn:
        with conn.cursor() as c:
            c.execute("SELECT count(*), max(applied_at) FROM audit.schema_migrations")
            count, applied_at = c.fetchone()
    print(json.dumps({"applied_count": count, "last_applied_at": str(applied_at)}))
except Exception as e:
    print(json.dumps({"error": str(e)[:200]}))
PYEOF
)
fi
printf 'TROSA_DEPLOY_JSON {"deploy_state":%s,"last_result":%s,"release_manifest":%s,"migration_ledger":%s}\n' \
  "$deploy_state" "$last_result" "$release_manifest" "$migration_ledger"

# Keep the resource line stable and machine-readable so the Mac workbench can
# present host facts without scraping the human-oriented diagnostic output.
cpu_snapshot() {
  awk '/^cpu / { idle=$5+$6; total=$2+$3+$4+$5+$6+$7+$8; printf "%.0f %.0f\n", total, idle; exit }' /proc/stat 2>/dev/null || true
}
cpu_before=$(cpu_snapshot)
sleep 0.35
cpu_after=$(cpu_snapshot)
cpu_pct=unknown
if [ -n "$cpu_before" ] && [ -n "$cpu_after" ]; then
  cpu_total_before=$(printf '%s\n' "$cpu_before" | awk '{print $1}')
  cpu_idle_before=$(printf '%s\n' "$cpu_before" | awk '{print $2}')
  cpu_total_after=$(printf '%s\n' "$cpu_after" | awk '{print $1}')
  cpu_idle_after=$(printf '%s\n' "$cpu_after" | awk '{print $2}')
  cpu_pct=$(awk -v total_before="$cpu_total_before" -v idle_before="$cpu_idle_before" \
    -v total_after="$cpu_total_after" -v idle_after="$cpu_idle_after" \
    'BEGIN { total=total_after-total_before; idle=idle_after-idle_before; if (total > 0) printf "%.1f", (1-idle/total)*100; else print "unknown" }')
fi

memory=$(free -b 2>/dev/null | awk '/^Mem:/ {print $2, $3; exit}')
mem_total=$(printf '%s\n' "$memory" | awk '{print $1}')
mem_used=$(printf '%s\n' "$memory" | awk '{print $2}')
mem_pct=unknown
if [ -n "$mem_total" ] && [ "$mem_total" -gt 0 ] 2>/dev/null; then
  mem_pct=$(awk -v used="$mem_used" -v total="$mem_total" 'BEGIN { printf "%.1f", used/total*100 }')
else
  mem_total=unknown
  mem_used=unknown
fi

disk=$(df -Pk / 2>/dev/null | awk 'NR > 1 {gsub("%", "", $5); print $3*1024, $2*1024, $5; exit}')
disk_used=$(printf '%s\n' "$disk" | awk '{print $1}')
disk_total=$(printf '%s\n' "$disk" | awk '{print $2}')
disk_pct=$(printf '%s\n' "$disk" | awk '{print $3}')
if [ -z "$disk_used" ] || [ -z "$disk_total" ]; then
  disk_used=unknown
  disk_total=unknown
  disk_pct=unknown
fi

load=$(awk '{print $1, $2, $3}' /proc/loadavg 2>/dev/null || true)
load_1=$(printf '%s\n' "$load" | awk '{print $1}')
load_5=$(printf '%s\n' "$load" | awk '{print $2}')
load_15=$(printf '%s\n' "$load" | awk '{print $3}')
if [ -z "$load_1" ] || [ -z "$load_5" ] || [ -z "$load_15" ]; then
  load_1=unknown
  load_5=unknown
  load_15=unknown
fi

uptime_seconds=$(awk '{printf "%.0f", $1}' /proc/uptime 2>/dev/null || true)
if [ -z "$uptime_seconds" ]; then uptime_seconds=unknown; fi
printf 'TROSA_MANAGER_RESOURCE cpu_pct=%s mem_used=%s mem_total=%s mem_pct=%s disk_used=%s disk_total=%s disk_pct=%s load_1=%s load_5=%s load_15=%s uptime_seconds=%s\n' \
  "$cpu_pct" "$mem_used" "$mem_total" "$mem_pct" "$disk_used" "$disk_total" "$disk_pct" \
  "$load_1" "$load_5" "$load_15" "$uptime_seconds"

systemctl --no-pager --full status trade-os cloudflared || true
free -h
df -hT /
journalctl -u trade-os -p warning..alert -n 20 --no-pager || true
