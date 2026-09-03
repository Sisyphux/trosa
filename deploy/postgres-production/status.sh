#!/usr/bin/env bash
# Read-only status and relationship gates for the production PostgreSQL target.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
source .env
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
password="$(tr -d '\r\n' < secrets/postgres_password)"
docker compose ps
docker compose exec -T -e "PGPASSWORD=${password}" postgres \
  pg_isready --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"

psql_query() {
  docker compose exec -T -e "PGPASSWORD=${password}" postgres \
    psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 \
    --tuples-only --no-align --command "$1" | tr -d '\r'
}

has_table() {
  [[ "$(psql_query "select to_regclass('$1') is not null")" == "t" ]]
}

if has_table audit.schema_migrations; then
  printf 'migrations=%s\n' "$(psql_query 'select count(*) from audit.schema_migrations')"
else
  printf '%s\n' 'migrations=pending'
fi

if has_table core.companies && has_table sela.prospects && has_table trosa.accounts && has_table core.contact_methods; then
  psql_query "select 'orphans=' || ((select count(*) from sela.prospects p left join core.companies c on c.id=p.company_id where c.id is null) + (select count(*) from trosa.accounts a left join core.companies c on c.id=a.company_id where c.id is null) + (select count(*) from core.contact_methods m left join core.companies c on c.id=m.company_id where m.company_id is null and m.person_id is null))"
else
  printf '%s\n' 'orphans=pending'
fi

if has_table sela.prospects && has_table trosa.accounts; then
  psql_query "select 'shared_active_companies=' || (select count(distinct p.company_id) from sela.prospects p join trosa.accounts a on a.company_id=p.company_id where a.deleted_at is null)"
else
  printf '%s\n' 'shared_active_companies=pending'
fi
