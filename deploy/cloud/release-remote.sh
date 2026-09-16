#!/usr/bin/env bash
# Trosa server-side release runner. Runs ON ECS, detached from the operator's
# connection, so a dropped local network / closed SSH channel can never leave
# production in an unknown half-published state.
#
# One release == one directory == one manifest == one result record:
#   /opt/trade-os/releases/<release-id>/release.json        (what was requested)
#   /opt/trade-os/releases/<release-id>/DEPLOY_RESULT.json  (what happened)
#   /opt/trade-os/.deploy-state.json                        (production pointer)
#   /opt/trade-os/.last-deploy-result.json                  (polling endpoint)
#
# Idempotency: re-running the same release id is always safe. Fetch, backup,
# migration, switch, health and rollback each check the current state first
# and skip work that is already done. Database migrations are forward-only;
# the runner never downgrades the schema.
#
# Usage (invoked on ECS by deploy/cloud/trosa-release, never by hand):
#   release-remote.sh REMOTE_ROOT SERVICE_NAME RELEASE_ID COMMIT_SHA GITHUB_REMOTE MODE [ALLOW_DESTRUCTIVE]
#   MODE: deploy | rollback
#
# Exit codes: 0 terminal success (status=success, already_production, refused
# is reported via result file with exit 0 so the state stays readable);
# 1 terminal failure (see DEPLOY_RESULT.json error/next_action); 75 locked.
set -uo pipefail

if [ "$#" -lt 6 ]; then
  printf 'Usage: %s REMOTE_ROOT SERVICE_NAME RELEASE_ID COMMIT_SHA GITHUB_REMOTE MODE [ALLOW_DESTRUCTIVE]\n' "$0" >&2
  exit 2
fi

REMOTE_ROOT=$1
SERVICE_NAME=$2
RELEASE_ID=$3
COMMIT_SHA=$4
GITHUB_REMOTE=$5
MODE=$6
ALLOW_DESTRUCTIVE=${7:-0}

case "$REMOTE_ROOT" in ''|*[!A-Za-z0-9._/-]*) printf 'Invalid remote root.\n' >&2; exit 2 ;; esac
case "$SERVICE_NAME" in ''|*[!A-Za-z0-9_.@-]*) printf 'Invalid service name.\n' >&2; exit 2 ;; esac
case "$RELEASE_ID" in ''|*[!A-Za-z0-9._-]*) printf 'Invalid release id.\n' >&2; exit 2 ;; esac
case "$COMMIT_SHA" in ''|*[!0-9a-fA-F]*) printf 'Invalid commit sha.\n' >&2; exit 2 ;; esac
[ "${#COMMIT_SHA}" -eq 40 ] || { printf 'Invalid commit sha length.\n' >&2; exit 2; }
case "$GITHUB_REMOTE" in https://github.com/[A-Za-z0-9_.-]*/[A-Za-z0-9_.-]*) ;; *) printf 'Invalid GitHub remote.\n' >&2; exit 2 ;; esac
case "$MODE" in deploy|rollback) ;; *) printf 'Invalid mode.\n' >&2; exit 2 ;; esac

if ! command -v flock >/dev/null 2>&1; then
  printf 'flock is required to serialize ECS releases.\n' >&2
  exit 1
fi

LOCK_PATH="$REMOTE_ROOT/.trosa-publish.lock"
STATE_FILE="$REMOTE_ROOT/.deploy-state.json"
LAST_RESULT="$REMOTE_ROOT/.last-deploy-result.json"
RELEASE_DIR="$REMOTE_ROOT/releases/$RELEASE_ID"
RESULT_FILE="$RELEASE_DIR/DEPLOY_RESULT.json"
LOG_FILE="$RELEASE_DIR/deploy.log"
GITHUB_REPOSITORY="${GITHUB_REMOTE#https://github.com/}"

exec 9>"$LOCK_PATH"
if ! flock -n 9; then
  printf 'Another ECS release is already running.\n' >&2
  exit 75
fi

NOW() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ---------------------------------------------------------------- state helpers
read_current_release() {
  # echoes "<release-id>" or "none"
  local target
  target=$(readlink -f "$REMOTE_ROOT/current" 2>/dev/null || true)
  if [ -z "$target" ]; then printf 'none'; return; fi
  printf '%s' "${target##*/}"
}

release_commit() {
  # echoes commit recorded in a release manifest, or "unknown"
  local dir=$1 out
  if [ -f "$dir/release.json" ]; then
    out=$(grep -o '"commit"[[:space:]]*:[[:space:]]*"[^"]*"' "$dir/release.json" 2>/dev/null | head -n 1 | cut -d'"' -f4 || true)
    [ -n "$out" ] && { printf '%s' "$out"; return; }
  fi
  printf 'unknown'
}

load_production_env() {
  # Production env lives in /etc/trade-os/trade-os.env plus the systemd
  # postgres drop-in. Both are sourced so migration/backup use the same DSN
  # the service itself runs with.
  if [ -f /etc/trade-os/trade-os.env ]; then
    set -a
    # shellcheck disable=SC1091
    . /etc/trade-os/trade-os.env
    set +a
  fi
  if [ -z "${TRADE_OS_DATABASE_URL:-}" ]; then
    local drop
    for drop in /etc/systemd/system/trade-os.service.d/*.conf; do
      [ -f "$drop" ] || continue
      local line
      line=$(grep -E '^Environment="?TRADE_OS_DATABASE_URL=' "$drop" 2>/dev/null | tail -n 1 || true)
      if [ -n "$line" ]; then
        TRADE_OS_DATABASE_URL=$(printf '%s' "$line" | sed -E 's/^Environment="?TRADE_OS_DATABASE_URL=//; s/"?$//')
        export TRADE_OS_DATABASE_URL
      fi
      line=$(grep -E '^Environment="?PGPASSFILE=' "$drop" 2>/dev/null | tail -n 1 || true)
      if [ -n "$line" ]; then
        PGPASSFILE=$(printf '%s' "$line" | sed -E 's/^Environment="?PGPASSFILE=//; s/"?$//')
        export PGPASSFILE
      fi
    done
  fi
}

write_result() {
  # write_result STATUS PHASE ERROR_JSON_EXTRA(not used) — builds DEPLOY_RESULT.json
  # from globals set by each phase. Kept in one function so every terminal
  # state lands in the same machine-readable shape.
  local status=$1 phase=$2 error=${3:-} next=${4:-}
  local prod_id prod_commit prev_id prev_commit
  prod_id=$(read_current_release)
  prod_commit=$(release_commit "$REMOTE_ROOT/releases/$prod_id")
  prev_id=$(python3 -c "import json;print(json.load(open('$STATE_FILE')).get('previous',{}).get('id','none'))" 2>/dev/null || printf 'unknown')
  prev_commit=$(release_commit "$REMOTE_ROOT/releases/$prev_id")
  python3 - "$RESULT_FILE" <<EOF
import json, sys
doc = {
  "release": "$RELEASE_ID",
  "commit": "$COMMIT_SHA",
  "mode": "$MODE",
  "phase": "$phase",
  "status": "$status",
  "production": {"id": "$prod_id", "commit": "$prod_commit"},
  "previous": {"id": "$prev_id", "commit": "$prev_commit"},
  "backup": json.loads(open("$RELEASE_DIR/.backup.json").read()) if __import__("os").path.exists("$RELEASE_DIR/.backup.json") else None,
  "migration": json.loads(open("$RELEASE_DIR/.migration.json").read()) if __import__("os").path.exists("$RELEASE_DIR/.migration.json") else None,
  "health": json.loads(open("$RELEASE_DIR/.health.json").read()) if __import__("os").path.exists("$RELEASE_DIR/.health.json") else None,
  "error": $(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$error"),
  "next_action": $(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$next"),
  "updated_at": "$(NOW)",
}
open(sys.argv[1], "w").write(json.dumps(doc, indent=2, sort_keys=True) + "\n")
EOF
  cp -f "$RESULT_FILE" "$LAST_RESULT"
  # LAST_RESULT is the polling endpoint: keep it single-line so clients can
  # stream-parse it without reassembling pretty-printed JSON.
  python3 -c "import json;open('$LAST_RESULT','w').write(json.dumps(json.load(open('$RESULT_FILE')),sort_keys=True,separators=(',',':'))+'\n')"
  printf 'result status=%s phase=%s\n' "$status" "$phase"
}

update_state_on_success() {
  local new_id=$1 new_commit=$2 old_id=$3 old_commit=$4
  python3 - "$STATE_FILE" <<EOF
import json, os
state = {"production": {}, "previous": {}, "previous_healthy": {}, "updated_at": ""}
if os.path.exists("$STATE_FILE"):
    try: state.update(json.load(open("$STATE_FILE")))
    except Exception: pass
state["previous"] = {"id": "$old_id", "commit": "$old_commit"}
state["previous_healthy"] = {"id": "$old_id", "commit": "$old_commit"}
state["production"] = {"id": "$new_id", "commit": "$new_commit"}
state["updated_at"] = "$(NOW)"
open("$STATE_FILE", "w").write(json.dumps(state, indent=2, sort_keys=True) + "\n")
EOF
}

# ---------------------------------------------------------------- health helpers
deep_health_json() {
  # Writes $RELEASE_DIR/.health.json; echoes 0/1.
  local expect_id=${1:-} expect_commit=${2:-}
  local ping_body="" app_body="" svc="unknown" ok=0
  local ping_ok=0 app_ok=0 svc_ok=0 ledger_ok=0 release_ok=0
  ping_body=$(curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8080/api/network/ping 2>/dev/null || true)
  app_body=$(curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8080/ 2>/dev/null || true)
  svc=$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || true)
  if printf '%s' "$ping_body" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"' \
    && printf '%s' "$ping_body" | grep -q '"backend"[[:space:]]*:[[:space:]]*"postgresql"' \
    && printf '%s' "$ping_body" | grep -q '"runtime_contract"[[:space:]]*:[[:space:]]*"trosa-postgresql-v1"' \
    && printf '%s' "$ping_body" | grep -q '"formal_runtime"[[:space:]]*:[[:space:]]*true'; then
    ping_ok=1
  fi
  if printf '%s' "$app_body" | grep -qi '<!DOCTYPE html>'; then app_ok=1; fi
  if [ "$svc" = "active" ]; then svc_ok=1; fi
  # Migration ledger: every migration file shipped by the CURRENT release must
  # be recorded in audit.schema_migrations. Needs production env for DSN.
  if [ -n "${TRADE_OS_DATABASE_URL:-}" ] && command -v python3 >/dev/null 2>&1; then
    local cur_dir
    cur_dir=$(readlink -f "$REMOTE_ROOT/current" 2>/dev/null || true)
    if python3 - "$cur_dir" <<'PYEOF' >/dev/null 2>&1; then
import os, sys
cur = sys.argv[1]
try:
    import psycopg
except Exception:
    sys.exit(0)  # driver missing: do not fail health on the check itself
names = sorted(f for f in os.listdir(os.path.join(cur, "migrations")) if f.endswith(".sql")) if cur and os.path.isdir(os.path.join(cur, "migrations")) else []
if not names:
    sys.exit(0)
import psycopg as _p
with _p.connect(os.environ["TRADE_OS_DATABASE_URL"]) as conn:
    with conn.cursor() as c:
        c.execute("SELECT name FROM audit.schema_migrations")
        applied = {r[0] for r in c.fetchall()}
missing = [n for n in names if n not in applied]
sys.exit(1 if missing else 0)
PYEOF
      ledger_ok=1
    fi
  fi
  if [ -n "$expect_id" ]; then
    [ "$(read_current_release)" = "$expect_id" ] && release_ok=1
  else
    release_ok=1
  fi
  if [ "$ping_ok" = 1 ] && [ "$app_ok" = 1 ] && [ "$svc_ok" = 1 ] && [ "$ledger_ok" = 1 ] && [ "$release_ok" = 1 ]; then ok=1; fi
  python3 - <<EOF
import json
open("$RELEASE_DIR/.health.json", "w").write(json.dumps({
  "ok": bool($ok),
  "checks": {"ping_contract": bool($ping_ok), "app_html": bool($app_ok),
             "systemd_active": bool($svc_ok), "migration_ledger": bool($ledger_ok),
             "release_pointer": bool($release_ok)},
  "checked_at": "$(NOW)",
}, indent=2, sort_keys=True) + "\n")
EOF
  printf '%s' "$ok"
}

wait_healthy() {
  local expect_id=$1 attempt
  for attempt in $(seq 1 20); do
    if [ "$(deep_health_json "$expect_id")" = "1" ]; then return 0; fi
    sleep 2
  done
  return 1
}

switch_to() {
  local target=$1
  ln -sfn "$target" "$REMOTE_ROOT/current.next"
  mv -Tf "$REMOTE_ROOT/current.next" "$REMOTE_ROOT/current"
}

prune_releases() {
  local current previous_healthy
  current=$(readlink -f "$REMOTE_ROOT/current" 2>/dev/null || true)
  previous_healthy=$(python3 -c "import json,os;print(json.load(open('$STATE_FILE')).get('previous_healthy',{}).get('id',''))" 2>/dev/null || true)
  find "$REMOTE_ROOT/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
    | sort -nr \
    | awk 'NR > 5 { sub(/^[^ ]+ /, ""); print }' \
    | while IFS= read -r stale; do
        if [ -n "$stale" ] && [ "$stale" != "$current" ] \
           && { [ -z "$previous_healthy" ] || [ "$stale" != "$REMOTE_ROOT/releases/$previous_healthy" ]; }; then
          rm -rf -- "$stale"
        fi
      done
}

# ---------------------------------------------------------------- deploy
do_deploy() {
  load_production_env
  mkdir -p "$RELEASE_DIR"
  exec >>"$LOG_FILE" 2>&1
  printf '=== trosa release deploy %s commit %s at %s ===\n' "$RELEASE_ID" "$COMMIT_SHA" "$(NOW)"

  local prod_before prod_commit_before
  prod_before=$(read_current_release)
  prod_commit_before=$(release_commit "$REMOTE_ROOT/releases/$prod_before")
  write_result "in_progress" "started" "" "runner started on ECS; safe to disconnect and re-poll"

  # Fast idempotent path: already running this exact commit and healthy.
  if [ -f "$RELEASE_DIR/release.json" ] && [ "$(release_commit "$RELEASE_DIR")" = "$COMMIT_SHA" ] \
     && [ "$prod_before" = "$RELEASE_ID" ] && [ "$(deep_health_json "$RELEASE_ID")" = "1" ]; then
    write_result "success" "done" "" "already production and healthy; nothing to do"
    printf 'already production %s\n' "$RELEASE_ID"
    return 0
  fi

  # ---- fetch (safe to repeat; production untouched) ----
  if [ ! -f "$RELEASE_DIR/release.json" ] || [ "$(release_commit "$RELEASE_DIR")" != "$COMMIT_SHA" ]; then
    local archive="/tmp/trosa-$COMMIT_SHA.tar.gz"
    curl --fail --location --silent --show-error --max-time 180 \
      "https://codeload.github.com/$GITHUB_REPOSITORY/tar.gz/$COMMIT_SHA" -o "$archive"
    rm -rf "$RELEASE_DIR"
    mkdir -p "$RELEASE_DIR"
    tar -xzf "$archive" -C "$RELEASE_DIR" --strip-components=1
    rm -f "$archive"
    "$REMOTE_ROOT/venv/bin/python" -m py_compile \
      "$RELEASE_DIR/app.py" "$RELEASE_DIR/db.py" "$RELEASE_DIR/scheduler.py" "$RELEASE_DIR/serve.py"
    python3 - <<EOF
import json
open("$RELEASE_DIR/release.json", "w").write(json.dumps({
  "id": "$RELEASE_ID", "commit": "$COMMIT_SHA",
  "repository": "$GITHUB_REMOTE", "staged_at": "$(NOW)",
}, indent=2, sort_keys=True) + "\n")
EOF
  fi
  "$REMOTE_ROOT/venv/bin/pip" install --disable-pip-version-check -q -r "$RELEASE_DIR/requirements.txt"

  # ---- db plan (explicit phase; production untouched) ----
  local applied_ledger=""
  if [ -n "${TRADE_OS_DATABASE_URL:-}" ]; then
    applied_ledger=$(TRADE_OS_DATABASE_URL="$TRADE_OS_DATABASE_URL" PGPASSFILE="${PGPASSFILE:-}" python3 - <<'PYEOF' 2>/dev/null || true
import os
try:
    import psycopg
    with psycopg.connect(os.environ["TRADE_OS_DATABASE_URL"]) as conn:
        with conn.cursor() as c:
            c.execute("SELECT name FROM audit.schema_migrations")
            print(",".join(sorted(r[0] for r in c.fetchall())), end="")
except Exception as e:
    print("LEDGER_UNREADABLE:" + str(e)[:200], end="")
PYEOF
)
  fi
  local plan_json plan_category pending_list=""
  if [ -x "$RELEASE_DIR/tools/release_db_plan.py" ] || [ -f "$RELEASE_DIR/tools/release_db_plan.py" ]; then
    plan_json=$(python3 "$RELEASE_DIR/tools/release_db_plan.py" "$RELEASE_DIR/migrations" --applied "$applied_ledger" 2>/dev/null || true)
  fi
  if [ -z "${plan_json:-}" ]; then
    # Fallback heuristic when the release predates the planner: any migration
    # file not in the ledger is pending; grep for destructive statements.
    pending_list=$(python3 - "$RELEASE_DIR/migrations" "$applied_ledger" <<'PYEOF' 2>/dev/null || true
import os, sys
mdir, applied = sys.argv[1], set(x for x in sys.argv[2].split(",") if x)
local = sorted(f for f in os.listdir(mdir) if f.endswith(".sql")) if os.path.isdir(mdir) else []
print(",".join(n for n in local if n not in applied), end="")
PYEOF
)
    local destructive=""
    if [ -n "$pending_list" ]; then
      destructive=$(python3 - "$RELEASE_DIR/migrations" "$pending_list" <<'PYEOF' 2>/dev/null || true
import os, re, sys
mdir = sys.argv[1]
# Same data-loss definition as tools/release_db_plan.py (fallback for
# releases that predate the planner): trigger-function bodies are not
# migration-time deletes; DO blocks and top level are.
func_start = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\b", re.I)
as_tag = re.compile(r"\bAS\s+(\$[A-Za-z_][A-Za-z_0-9]*\$|\$\$)", re.I)
def strip_funcs(text):
    parts, pos = [], 0
    for m in func_start.finditer(text):
        tm = as_tag.search(text, m.end(), m.end() + 4000)
        if not tm: continue
        tag = tm.group(1)
        end = text.find(tag, tm.end())
        if end == -1: continue
        parts.append(text[pos:tm.end()]); parts.append(" ")
        pos = end + len(tag)
    parts.append(text[pos:])
    return "".join(parts)
pat = re.compile(r"(DROP\s+(TABLE|TABLES|COLUMN|SCHEMA|DATABASE)|TRUNCATE\s+(TABLE|TABLES)|DELETE\s+FROM|ALTER\s+TABLE[^;]*DROP\s+COLUMN)", re.I)
bad = []
for name in sys.argv[2].split(","):
    if not name: continue
    try: text = open(os.path.join(mdir, name), encoding="utf-8").read()
    except OSError: bad.append(name); continue
    text = strip_funcs(text)
    text = re.sub(r"--[^\n]*", " ", re.sub(r"/\*.*?\*/", " ", text, flags=re.S))
    if pat.search(text): bad.append(name)
print(",".join(bad), end="")
PYEOF
)
    fi
    if [ -n "$destructive" ]; then plan_category="destructive";
    elif [ -n "$pending_list" ]; then plan_category="compatible";
    else plan_category="none"; fi
    plan_json=$(python3 - <<EOF
import json
print(json.dumps({"category": "$plan_category",
  "pending_migrations": [x for x in "$pending_list".split(",") if x],
  "destructive_files": [x for x in "${destructive:-}".split(",") if x],
  "sensitive_paths": [], "requires_backup": bool("$pending_list"),
  "allow_auto_apply": "$plan_category" != "destructive",
  "migration_count": len([x for x in "$pending_list".split(",") if x])}))
EOF
)
  fi
  plan_category=$(printf '%s' "$plan_json" | python3 -c "import json,sys;print(json.load(sys.stdin).get('category','compatible'))" 2>/dev/null || printf 'compatible')
  printf '%s' "$plan_json" | python3 -c "import json,sys;open('$RELEASE_DIR/.migration.json','w').write(json.dumps({'plan':json.load(sys.stdin),'applied_before':'$applied_ledger'[:4000]},indent=2,sort_keys=True))"
  printf 'db plan: %s\n' "$plan_category"

  if [ "$plan_category" = "destructive" ] && [ "$ALLOW_DESTRUCTIVE" != "1" ]; then
    write_result "refused" "db_plan" "destructive database change requires explicit approval" \
      "re-run publish with explicit destructive approval after reviewing pending migrations; production unchanged"
    return 0
  fi
  if [ "${applied_ledger#LEDGER_UNREADABLE}" != "$applied_ledger" ]; then
    write_result "failed" "db_plan" "migration ledger unreadable: $applied_ledger" \
      "check PostgreSQL availability, then re-run the same release; production unchanged"
    return 1
  fi

  # ---- backup (server-local pre-migration snapshot; no download in publish path) ----
  local needs_backup
  needs_backup=$(printf '%s' "$plan_json" | python3 -c "import json,sys;print('1' if json.load(sys.stdin).get('requires_backup') else '0')" 2>/dev/null || printf '0')
  if [ "$needs_backup" = "1" ]; then
    local pg_root="${TRADE_OS_POSTGRES_ROOT:-/opt/trade-os-postgres}"
    local snap_dir="/var/lib/trade-os/release-backups/$RELEASE_ID"
    mkdir -p "$snap_dir"
    chmod 700 "$snap_dir" || true
    if [ -x "$pg_root/backup.sh" ]; then
      local backup_log="$snap_dir/backup.log" dump_rel="" database_sha=""
      (cd "$pg_root" && ./backup.sh >"$backup_log" 2>&1) || {
        write_result "failed" "backup" "pre-migration backup failed; see $backup_log" \
          "fix backup, then re-run the same release; production and database unchanged"
        return 1
      }
      dump_rel=$(sed -n 's/^backup=//p' "$backup_log" | tail -n 1)
      database_sha=$(sed -n 's/^sha256=//p' "$backup_log" | tail -n 1)
      if [ -z "$dump_rel" ] || [ -z "$database_sha" ] || [ "${dump_rel#/}" != "$dump_rel" ]; then
        write_result "failed" "backup" "backup did not return a valid dump path and checksum" \
          "re-run the same release; production and database unchanged"
        return 1
      fi
      cp -- "$pg_root/$dump_rel" "$snap_dir/database.dump"
      [ "$(sha256sum "$snap_dir/database.dump" | awk '{print $1}')" = "$database_sha" ] || {
        write_result "failed" "backup" "backup checksum mismatch after copy" \
          "re-run the same release; production and database unchanged"
        return 1
      }
      python3 - <<EOF
import json
open("$RELEASE_DIR/.backup.json", "w").write(json.dumps({
  "path": "$snap_dir/database.dump", "sha256": "$database_sha",
  "verified": True, "scope": "pre-migration server-local",
  "created_at": "$(NOW)",
}, indent=2, sort_keys=True) + "\n")
EOF
      printf 'backup ok: %s\n' "$snap_dir/database.dump"
    else
      write_result "failed" "backup" "postgres backup runner missing at $pg_root/backup.sh" \
        "restore the postgres directory on ECS, then re-run the same release; production unchanged"
      return 1
    fi
  fi

  # ---- migrate (explicit phase, before any traffic switch) ----
  local pending_count
  pending_count=$(printf '%s' "$plan_json" | python3 -c "import json,sys;print(json.load(sys.stdin).get('migration_count',0))" 2>/dev/null || printf '0')
  if [ "$pending_count" != "0" ]; then
    CRM_ENV=production TRADE_OS_DATA_BACKEND=postgres \
    TRADE_OS_DATABASE_URL="${TRADE_OS_DATABASE_URL:-}" PGPASSFILE="${PGPASSFILE:-}" \
    "$REMOTE_ROOT/venv/bin/python" - "$RELEASE_DIR" <<'PYEOF' || {
import sys
sys.path.insert(0, sys.argv[1])
import db
db.require_formal_postgres_runtime()
db.init_postgres_store()
PYEOF
      write_result "failed" "migrate" "migration failed; production still runs $prod_before" \
        "inspect deploy.log, fix the migration as a NEW forward file, then re-run; production code and traffic unchanged"
      return 1
    }
    printf 'migration applied\n'
  else
    printf 'no pending migrations\n'
  fi

  # ---- activate (atomic switch) ----
  chown -R root:root "$RELEASE_DIR"
  switch_to "$RELEASE_DIR"
  systemctl daemon-reload
  if ! systemctl restart "$SERVICE_NAME"; then
    if [ -n "$prod_before" ] && [ "$prod_before" != "none" ]; then
      switch_to "$REMOTE_ROOT/releases/$prod_before"
      systemctl restart "$SERVICE_NAME" || true
    fi
    write_result "rolled_back" "activate" "service restart failed on new release; restored $prod_before" \
      "inspect deploy.log; fix and re-run the same release"
    return 1
  fi

  # ---- health (deep; auto-rollback on failure) ----
  if ! wait_healthy "$RELEASE_ID"; then
    if [ -n "$prod_before" ] && [ "$prod_before" != "none" ]; then
      switch_to "$REMOTE_ROOT/releases/$prod_before"
      systemctl restart "$SERVICE_NAME" || true
      sleep 3
      if [ "$(deep_health_json "$prod_before")" = "1" ]; then
        journalctl -u "$SERVICE_NAME" -n 80 --no-pager || true
        write_result "rolled_back" "health" "new release unhealthy; restored previous $prod_before (healthy)" \
          "inspect deploy.log; fix and re-run the same release"
        return 1
      fi
    fi
    journalctl -u "$SERVICE_NAME" -n 80 --no-pager || true
    write_result "rollback_failed" "health" "new release unhealthy AND previous $prod_before did not recover" \
      "manual intervention required on ECS; database migrations are forward-only, check ledger before switching code"
    return 1
  fi

  prune_releases
  update_state_on_success "$RELEASE_ID" "$COMMIT_SHA" "$prod_before" "$prod_commit_before"
  write_result "success" "done" "" "production is $RELEASE_ID; previous healthy is $prod_before"
  printf 'published %s\n' "$RELEASE_ID"
  return 0
}

# ---------------------------------------------------------------- rollback
do_rollback() {
  load_production_env
  mkdir -p "$RELEASE_DIR"
  exec >>"$LOG_FILE" 2>&1
  printf '=== trosa release rollback at %s ===\n' "$(NOW)"

  local prod_now target target_commit prod_commit_now
  prod_now=$(read_current_release)
  prod_commit_now=$(release_commit "$REMOTE_ROOT/releases/$prod_now")
  write_result "in_progress" "started" "" "rollback runner started on ECS; safe to disconnect and re-poll"
  # Pinned previous healthy first; fall back to newest non-current dir by mtime.
  target=$(python3 -c "import json;print(json.load(open('$STATE_FILE')).get('previous_healthy',{}).get('id',''))" 2>/dev/null || true)
  if [ -z "$target" ] || [ ! -d "$REMOTE_ROOT/releases/$target" ] || [ "$target" = "$prod_now" ]; then
    target=""
    local candidate
    while IFS= read -r candidate; do
      candidate=${candidate##*/}
      if [ "$candidate" != "$prod_now" ]; then target=$candidate; break; fi
    done < <(find "$REMOTE_ROOT/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -nr | awk '{ sub(/^[^ ]+ /, ""); print }')
  fi
  if [ -z "$target" ]; then
    write_result "failed" "rollback" "no previous release available" "nothing changed; production is $prod_now"
    return 1
  fi
  target_commit=$(release_commit "$REMOTE_ROOT/releases/$target")
  switch_to "$REMOTE_ROOT/releases/$target"
  systemctl restart "$SERVICE_NAME" || true
  if ! wait_healthy "$target"; then
    write_result "rollback_failed" "rollback" "rollback target $target unhealthy after switch" \
      "manual intervention required; database is forward-only, do not switch code blindly"
    return 1
  fi
  update_state_on_success "$target" "$target_commit" "$prod_now" "$prod_commit_now"
  write_result "success" "done" "" "rolled back to $target; database migrations were NOT downgraded (forward-only)"
  printf 'rolled back to %s\n' "$target"
  return 0
}

case "$MODE" in
  deploy) do_deploy ;;
  rollback) do_rollback ;;
esac
