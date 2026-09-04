# Trade OS production PostgreSQL target

This directory is the production database target for the eventual cutover.
It is separate from `deploy/postgres-rehearsal/` and must never share its
Docker volume, database name, password, or port.

The service binds PostgreSQL to `127.0.0.1:5432` on the ECS host. Trosa uses
that loopback endpoint directly. The local sela service reaches the same
database only through the managed SSH tunnel template in the sela repository;
PostgreSQL is never exposed on the public interface.

Before starting it on ECS:

```bash
cp .env.example .env
mkdir -p secrets backups
chmod 700 secrets backups
openssl rand -base64 48 > secrets/postgres_password
chmod 600 secrets/postgres_password
docker compose up -d
./status.sh
```

Apply the seven canonical migrations and import only from verified, immutable
source snapshots. Do not point the importer at `/var/lib/trade-os` while the
SQLite service is running.

After import, create a logical backup and run a restore check:

```bash
./backup.sh
./restore-check.sh backups/<verified-dump>.dump
```

The dump must also be copied to an independent host or object store before a
production cutover. A local Docker volume alone is not a disaster-recovery
plan. The existing SQLite snapshot remains the application rollback source
until the PostgreSQL cutover has passed its observation window.
