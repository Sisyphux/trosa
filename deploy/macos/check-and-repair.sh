#!/bin/zsh
# Check the production application and its Cloudflare Tunnel, then restart only
# the unhealthy component. It is safe to run interactively or from LaunchAgent.
set -u

SERVICE_DIR="${TRADE_OS_SERVICE_DIR:-/Users/luoxin/Library/Application Support/TradeOS}"
ENV_FILE="$SERVICE_DIR/env.production"
LOG_DIR="$SERVICE_DIR/logs"
LOG_FILE="$LOG_DIR/health-monitor.log"
LOCK_DIR="$SERVICE_DIR/.health-check-lock"
APP_LABEL="com.tradeos.app"
TUNNEL_LABEL="com.tradeos.tunnel"
USER_ID="$(/usr/bin/id -u)"
LAUNCHCTL_BIN="/bin/launchctl"
CURL_BIN="${TRADE_OS_CURL_BIN:-/usr/bin/curl}"
LOCAL_HEALTH_URL="http://127.0.0.1:${CRM_PORT:-8080}/api/network/ping"
DEFAULT_PUBLIC_URL="https://app.trosa.space"
QUIET=0

usage() {
  print "Usage: ${0:t} [--quiet]"
  print "Checks the local Trade OS API and public Tunnel endpoint, then restarts an unhealthy service."
}

case "${1:-}" in
  "") ;;
  --quiet) QUIET=1 ;;
  --help|-h) usage; exit 0 ;;
  *) usage; exit 2 ;;
esac

if [[ -r "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

LOCAL_HEALTH_URL="http://127.0.0.1:${CRM_PORT:-8080}/api/network/ping"
PUBLIC_HEALTH_URL="${CRM_PUBLIC_URL:-$DEFAULT_PUBLIC_URL}"
PUBLIC_HEALTH_URL="${PUBLIC_HEALTH_URL%/}/api/network/ping"

report() {
  (( QUIET )) || print -- "$*"
}

write_log() {
  /bin/mkdir -p "$LOG_DIR"
  print -- "$(/bin/date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
  report "$*"
}

release_lock() {
  /bin/rm -f "$LOCK_DIR/pid"
  /bin/rmdir "$LOCK_DIR" 2>/dev/null || true
}

acquire_lock() {
  if /bin/mkdir "$LOCK_DIR" 2>/dev/null; then
    print -- "$$" > "$LOCK_DIR/pid"
    trap release_lock EXIT INT TERM
    return 0
  fi

  local owner_pid=""
  if [[ -r "$LOCK_DIR/pid" ]]; then
    owner_pid="$(<"$LOCK_DIR/pid")"
  fi
  if [[ "$owner_pid" == <-> ]] && /bin/kill -0 "$owner_pid" 2>/dev/null; then
    report "已有自检任务正在运行，跳过本次检查。"
    return 1
  fi

  # A previous process ended unexpectedly. This directory is a fixed,
  # service-local lock target and contains no CRM data.
  /bin/rm -f "$LOCK_DIR/pid"
  /bin/rmdir "$LOCK_DIR" 2>/dev/null || {
    report "无法取得自检锁，跳过本次检查。"
    return 1
  }
  /bin/mkdir "$LOCK_DIR"
  print -- "$$" > "$LOCK_DIR/pid"
  trap release_lock EXIT INT TERM
}

http_status() {
  local url="$1"
  local http_code=""
  http_code="$("$CURL_BIN" --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 4 --max-time 12 "$url" 2>/dev/null)" || {
    print "000"
    return 0
  }
  if [[ "$http_code" == <-> ]]; then
    print -- "$http_code"
  else
    print "000"
  fi
}

wait_for_local_api() {
  local http_code=""
  integer attempt
  for (( attempt = 1; attempt <= 15; attempt++ )); do
    http_code="$(http_status "$LOCAL_HEALTH_URL")"
    [[ "$http_code" == "200" ]] && return 0
    /bin/sleep 2
  done
  write_log "本机应用在重启后仍未通过健康检查（HTTP $http_code）。"
  return 1
}

public_is_healthy() {
  local http_code="$1"
  # 2xx/3xx, as well as Access's unauthenticated responses, prove that the
  # request reached Cloudflare and the named Tunnel rather than a 1033 page.
  [[ "$http_code" == 2* || "$http_code" == 3* || "$http_code" == "401" || "$http_code" == "403" ]]
}

wait_for_public_endpoint() {
  local http_code=""
  integer attempt
  for (( attempt = 1; attempt <= 15; attempt++ )); do
    http_code="$(http_status "$PUBLIC_HEALTH_URL")"
    public_is_healthy "$http_code" && return 0
    /bin/sleep 2
  done
  write_log "公网 Tunnel 在重启后仍未通过检查（HTTP $http_code）。"
  return 1
}

restart_service() {
  local label="$1"
  local description="$2"
  write_log "检测到${description}异常，正在重启 ${label}。"
  if ! "$LAUNCHCTL_BIN" kickstart -k "gui/$USER_ID/$label" >/dev/null 2>&1; then
    write_log "无法重启 ${label}；请检查 LaunchAgent 是否已安装。"
    return 1
  fi
  return 0
}

acquire_lock || exit 0

local_status="$(http_status "$LOCAL_HEALTH_URL")"
if [[ "$local_status" != "200" ]]; then
  restart_service "$APP_LABEL" "本机 Trade OS 服务" || exit 1
  wait_for_local_api || exit 1
fi

public_status="$(http_status "$PUBLIC_HEALTH_URL")"
if ! public_is_healthy "$public_status"; then
  # A second probe avoids restarting a healthy tunnel for a short DNS or edge
  # fluctuation. Cloudflare 1033 normally persists through this short delay.
  /bin/sleep 4
  public_status="$(http_status "$PUBLIC_HEALTH_URL")"
  if ! public_is_healthy "$public_status"; then
    restart_service "$TUNNEL_LABEL" "公网 Tunnel（HTTP $public_status）" || exit 1
    wait_for_public_endpoint || exit 1
  fi
fi

report "Trade OS 与公网 Tunnel 均正常。"
