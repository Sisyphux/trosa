#!/usr/bin/env bash
# Create and verify a consistent logical PostgreSQL backup.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
if [[ ! -r .env || ! -r secrets/postgres_password ]]; then
  printf '%s\n' 'Missing .env or secrets/postgres_password' >&2
  exit 1
fi
source .env
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"

mkdir -p backups
chmod 700 backups
password="$(tr -d '\r\n' < secrets/postgres_password)"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump="backups/${POSTGRES_DB}-${stamp}.dump"
checksum="${dump}.sha256"

docker_dump="/backups/$(basename "$dump")"
docker compose exec -T -e "PGPASSWORD=${password}" postgres \
  pg_dump --format=custom --no-owner --no-acl --serializable-deferrable \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" > "$dump"
chmod 600 "$dump"
docker compose exec -T postgres pg_restore --list "$docker_dump" >/dev/null
sha256sum "$dump" > "$checksum"
chmod 600 "$checksum"
printf 'backup=%s\n' "$dump"
printf 'sha256=%s\n' "$(awk '{print $1}' "$checksum")"
