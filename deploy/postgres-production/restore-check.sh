#!/usr/bin/env bash
# Restore a dump into a temporary database in the same PostgreSQL instance,
# validate the core relationship gates, then remove only that temporary DB.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
if [[ "$#" -ne 1 || ! -r "$1" ]]; then
  printf 'Usage: %s BACKUP.dump\n' "$0" >&2
  exit 2
fi
source .env
: "${POSTGRES_USER:?POSTGRES_USER is required}"
password="$(tr -d '\r\n' < secrets/postgres_password)"
dump="$1"
check_db="tradeos_restore_check_$(date -u +%Y%m%d%H%M%S)"

cleanup() {
  docker compose exec -T -e "PGPASSWORD=${password}" postgres \
    psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 \
    --command "DROP DATABASE IF EXISTS \"$check_db\"" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker compose exec -T -e "PGPASSWORD=${password}" postgres \
  psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 \
  --command "CREATE DATABASE \"$check_db\""
docker compose exec -T -e "PGPASSWORD=${password}" postgres \
  pg_restore --no-owner --no-acl --username "$POSTGRES_USER" --dbname "$check_db" < "$dump"

docker compose exec -T -e "PGPASSWORD=${password}" postgres \
  psql --username "$POSTGRES_USER" --dbname "$check_db" --set ON_ERROR_STOP=1 --tuples-only --no-align \
  --command "select 'orphans=' || ((select count(*) from sela.prospects p left join core.companies c on c.id=p.company_id where c.id is null) + (select count(*) from trosa.accounts a left join core.companies c on c.id=a.company_id where c.id is null) + (select count(*) from core.contact_methods m left join core.companies c on c.id=m.company_id where m.company_id is null and m.person_id is null)); select 'schema_migrations=' || count(*) from audit.schema_migrations"
printf '%s\n' 'restore-check=passed'
