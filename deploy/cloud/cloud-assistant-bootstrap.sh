#!/usr/bin/env bash
# Run once through Cloud Assistant as root, never through SSH.  It creates the
# non-login account used by regular Trosa Cloud Assistant commands and exposes
# exactly one sudo entrypoint for the existing release runner.
set -euo pipefail

# ``tradeos`` must traverse this root-owned directory to execute the one
# fixed read-only program below; 0711 permits traversal but not listing or
# writing its contents.
install -d -m 0711 -o root -g root /usr/local/lib/trosa
planner_commit="${TROSA_DB_PLAN_COMMIT:-}"
if [[ ! "$planner_commit" =~ ^[0-9a-fA-F]{40}$ ]]; then
  printf '%s\n' 'TROSA_DB_PLAN_COMMIT must be an exact 40-character public Trosa commit.' >&2
  exit 2
fi
planner_tmp="/tmp/trosa-release-db-plan-${planner_commit}.py"
curl --fail --location --silent --show-error --max-time 60 \
  "https://raw.githubusercontent.com/Sisyphux/trosa/${planner_commit}/tools/release_db_plan.py" \
  -o "$planner_tmp"
install -m 0555 -o root -g root "$planner_tmp" /usr/local/lib/trosa/release_db_plan.py
rm -f "$planner_tmp"
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
if [[ "$mode" == publish ]]; then mode=deploy; fi
runner_file="/tmp/trosa-release-remote-${release_id}.sh"
runner_url="https://raw.githubusercontent.com/Sisyphux/trosa/${commit}/deploy/cloud/release-remote.sh"
curl --fail --location --silent --show-error --max-time 60 "$runner_url" -o "$runner_file"
chmod 0700 "$runner_file"
setsid nohup bash "$runner_file" "$remote_root" "$service_name" "$release_id" "$commit" "$github_remote" "$mode" "$allow_destructive" \
  >"/tmp/trosa-release-${release_id}.launch.log" 2>&1 </dev/null &
printf 'launched %s mode=%s\n' "$release_id" "$mode"
RUNNER

# A zero-argument, read-only inspection path.  It deliberately has no way to
# select a database, SQL statement, release, or file: trosa-operator can only
# ask the service account to inspect the currently active Trosa release.
install -m 0755 -o root -g root /dev/stdin /usr/local/lib/trosa/db-plan-readonly-service <<'SERVICE'
#!/opt/trade-os/venv/bin/python
"""Emit a secret-free database plan for the active Trosa release.

This program runs as the formal ``tradeos`` account.  It reads no operator
input, executes one fixed SELECT against the migration ledger, and delegates
classification to the active release's release_db_plan.py.
"""
import json
import os
import shlex
import subprocess
import sys


def emit_error(code):
    # Never serialize exception text: libpq errors can include a DSN path or
    # other deployment details that are not part of this read-only contract.
    print(json.dumps({"ok": False, "ledger_readable": False, "error": code},
                     sort_keys=True, separators=(",", ":")))
    raise SystemExit(1)


if len(sys.argv) != 1:
    emit_error("invalid_invocation")

remote_root = "/opt/trade-os"
python_bin = f"{remote_root}/venv/bin/python"
current = os.path.realpath(f"{remote_root}/current")
migrations = os.path.join(current, "migrations")
planner = "/usr/local/lib/trosa/release_db_plan.py"
if not (os.path.isfile(python_bin) and os.path.isdir(migrations) and os.path.isfile(planner)):
    emit_error("formal_runtime_unavailable")

try:
    raw_env = subprocess.check_output(
        ["systemctl", "show", "trade-os", "--property=Environment", "--value"],
        text=True,
    ).strip()
    service_env = dict(
        item.split("=", 1) for item in shlex.split(raw_env) if "=" in item
    )
    database_url = service_env["TRADE_OS_DATABASE_URL"]
    pgpassfile = service_env["PGPASSFILE"]
    if not os.path.isfile(pgpassfile) or not os.access(pgpassfile, os.R_OK):
        emit_error("service_database_credentials_unavailable")
    os.environ["PGPASSFILE"] = pgpassfile
    import psycopg
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT name FROM audit.schema_migrations ORDER BY name")
            applied = {row[0] for row in cursor.fetchall()}
except SystemExit:
    raise
except Exception:
    emit_error("migration_ledger_unreadable")

local_names = {
    name for name in os.listdir(migrations)
    if name.endswith(".sql") and os.path.isfile(os.path.join(migrations, name))
}
if not all(isinstance(name, str) and name.endswith(".sql") and os.path.basename(name) == name
           for name in applied):
    emit_error("migration_ledger_invalid")

# Only release-local filenames enter the planner.  This preserves the same
# boundary as release-remote.sh and prevents any non-filename/error value from
# being interpreted as a pending migration.
applied_local = sorted(applied & local_names)
try:
    planned = subprocess.run(
        [python_bin, planner, migrations, "--applied", ",".join(applied_local)],
        check=True, capture_output=True, text=True,
    )
    plan = json.loads(planned.stdout)
except Exception:
    emit_error("db_plan_failed")

print(json.dumps({
    "ok": True,
    "ledger_readable": True,
    "applied_migration_count": len(applied),
    "applied_migration_first": min(applied) if applied else None,
    "applied_migration_last": max(applied) if applied else None,
    "pending_migrations": plan.get("pending_migrations", []),
    "category": plan.get("category"),
    "destructive_files": plan.get("destructive_files", []),
}, sort_keys=True, separators=(",", ":")))
SERVICE

install -m 0750 -o root -g root /dev/stdin /usr/local/lib/trosa/db-plan-readonly <<'READONLY'
#!/usr/bin/env bash
set -euo pipefail
[[ "$#" == 0 ]] || { echo 'db-plan-readonly accepts no arguments' >&2; exit 2; }
exec runuser -u tradeos -- /usr/local/lib/trosa/db-plan-readonly-service
READONLY

install -m 0440 -o root -g root /dev/stdin /etc/sudoers.d/trosa-cloud-assistant <<'SUDOERS'
Defaults:trosa-operator !requiretty
trosa-operator ALL=(root) NOPASSWD: /usr/local/lib/trosa/release-runner
trosa-operator ALL=(root) NOPASSWD: /usr/local/lib/trosa/db-plan-readonly ""
SUDOERS
visudo -cf /etc/sudoers.d/trosa-cloud-assistant
printf 'TROSA_CLOUD_ASSISTANT_BOOTSTRAP_OK user=trosa-operator\n'
