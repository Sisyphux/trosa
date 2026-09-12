#!/bin/zsh

# Trosa's human entrypoint is the already-published PostgreSQL service.  This
# launcher is deliberately a browser opener; it never creates a local Flask
# process or a SQLite business store.
set -u

PUBLIC_URL="${TROSA_PUBLIC_URL:-https://app.trosa.space}"
LOCAL_URL="${TROSA_LOCAL_URL:-http://127.0.0.1:8080}"
PING_URL="${LOCAL_URL%/}/api/network/ping"

if PING_BODY="$(/usr/bin/curl -fsS --max-time 1 "$PING_URL" 2>/dev/null)"; then
  if print -r -- "$PING_BODY" | /usr/bin/grep -q '"status"[[:space:]]*:[[:space:]]*"ok"' \
      && print -r -- "$PING_BODY" | /usr/bin/grep -q '"backend"[[:space:]]*:[[:space:]]*"postgresql"' \
      && print -r -- "$PING_BODY" | /usr/bin/grep -q '"runtime_contract"[[:space:]]*:[[:space:]]*"trosa-postgresql-v1"' \
      && print -r -- "$PING_BODY" | /usr/bin/grep -q '"formal_runtime"[[:space:]]*:[[:space:]]*true'; then
    print -r -- "检测到本机 PostgreSQL Trosa 服务，正在打开 $LOCAL_URL"
    /usr/bin/open "$LOCAL_URL"
    exit 0
  fi
  print -u2 -- "警告：本机 8080 不是受信任的 PostgreSQL Trosa 服务，已跳过。"
fi

print -r -- "正在打开正式 Trosa：$PUBLIC_URL"
/usr/bin/open "$PUBLIC_URL"
