#!/usr/bin/env bash
# Create / refresh the read-only maintenance role on the production PostgreSQL.
#
# Run on the ECS host by the release role or an administrator (it changes
# production database roles; development sessions never run it).  Idempotent.
#
#   ./maint-readonly-role.sh            # uses secrets/postgres_password for the admin
#
# The role can SELECT business data of every member and nothing else:
#   * default_transaction_read_only=on, short statement_timeout
#   * no write / DDL / function-execute privileges
#   * no access to the ``identity`` schema's credential-bearing tables
# Its password goes to secrets/maint_readonly_password (mode 600); copy it to
# the maintainer's ~/.config/trosa-maint/readonly.json, NOT to workbench.env.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
DB_NAME="${TRADE_OS_DB_NAME:-trade_os}"
ADMIN_USER="${TRADE_OS_DB_ADMIN:-postgres}"
ROLE="trosa_maint_ro"
SECRET_FILE="secrets/maint_readonly_password"

mkdir -p secrets
chmod 700 secrets
if [ ! -s "$SECRET_FILE" ]; then
  umask 077
  openssl rand -base64 36 | tr -d '\n=+/' > "$SECRET_FILE"
fi
chmod 600 "$SECRET_FILE"
PASSWORD="$(cat "$SECRET_FILE")"

psql_admin() {
  docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$ADMIN_USER" -d "$DB_NAME" "$@"
}

psql_admin <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${ROLE}') THEN
    CREATE ROLE ${ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
END
\$\$;
ALTER ROLE ${ROLE} WITH LOGIN PASSWORD '${PASSWORD}' CONNECTION LIMIT 5;
ALTER ROLE ${ROLE} SET default_transaction_read_only = on;
ALTER ROLE ${ROLE} SET statement_timeout = '20s';
ALTER ROLE ${ROLE} SET idle_in_transaction_session_timeout = '30s';

REVOKE ALL ON DATABASE ${DB_NAME} FROM ${ROLE};
GRANT CONNECT ON DATABASE ${DB_NAME} TO ${ROLE};

-- Business schemas: read everything, including future tables from migrations.
DO \$\$
DECLARE s text;
BEGIN
  FOREACH s IN ARRAY ARRAY['trosa','core','audit','sela','trade_os_compat']
  LOOP
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = s) THEN
      EXECUTE format('GRANT USAGE ON SCHEMA %I TO ${ROLE}', s);
      EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO ${ROLE}', s);
      EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON TABLES TO ${ROLE}', s);
    END IF;
  END LOOP;
END
\$\$;

-- identity: member list only (no invitations / settings).
GRANT USAGE ON SCHEMA identity TO ${ROLE};
REVOKE ALL ON ALL TABLES IN SCHEMA identity FROM ${ROLE};
GRANT SELECT ON identity.users, identity.memberships, identity.organizations TO ${ROLE};
SQL

cat <<'MSG'

Read-only role ready: trosa_maint_ro
Password: secrets/maint_readonly_password (do not paste into chat or commit)
Next: put DSN into ~/.config/trosa-maint/readonly.json on the maintainer's Mac
(see docs/MAINTAINER_DATA_ACCESS.md). The role has no write path.
MSG
