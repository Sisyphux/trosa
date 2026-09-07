# Trade OS production PostgreSQL target

This directory is the production PostgreSQL target after the cutover. It is
separate from `deploy/postgres-rehearsal/` and must never share its Docker
volume, database name, password, or port.

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

Apply the ordered migrations and import only from verified, immutable source
snapshots. The running Trosa service uses PostgreSQL through the systemd
drop-in at `/etc/systemd/system/trade-os.service.d/postgres.conf`; do not
point the importer at `/var/lib/trade-os` as if it were the production
database.

After import, create a logical backup and run a restore check:

```bash
./backup.sh
./restore-check.sh backups/<verified-dump>.dump
```

The dump must also be copied to an independent host or object store. A local
Docker volume alone is not a disaster-recovery plan. The workbench backup
wrapper packages this verified logical dump together with the file attachments
under `/var/lib/trade-os/uploads/customer_files`; a PostgreSQL dump alone does
not contain those files. SQLite files are retained only for isolated legacy
rehearsal or explicitly approved rollback work, not as the active data source.
