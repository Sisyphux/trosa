# Trade OS PostgreSQL migration rehearsal

This runbook creates an isolated, non-production PostgreSQL database for a
first migration rehearsal.  It does not alter the running Trosa SQLite files,
sela JSON/SQLite ledgers, or production application configuration.

The target schemas are `identity`, `core`, `trosa`, `sela`, and `audit`.
They live in one PostgreSQL database.  No synchronization service, dual write,
or production read/write cutover is part of this runbook.

## 0. Guardrails

- Use a new Docker volume named `trade_os_postgres_rehearsal`; never mount the
  production Trosa data directory into the container.
- Bind PostgreSQL only to `127.0.0.1:5433`.  Do not open port 5432 in an ECS
  security group or bind the rehearsal service to `0.0.0.0`.
- Copy only an existing consistent Trosa SQLite backup and read sela data in
  read-only mode.  Do not point the rehearsal at live writable source files.
- Set `TRADE_OS_DATABASE_URL` only in the shell executing the rehearsal tool.
  Do not add it to the production `.env` or service unit.
- A failed rehearsal is rolled back by deleting the *rehearsal Docker volume*.
  Source SQLite and JSON files are never rollback targets.

## 1. Local Docker deployment

From the Trosa repository:

```bash
cd /Users/luoxin/Desktop/Trosa/deploy/postgres-rehearsal
cp .env.example .env
mkdir -p secrets
chmod 700 secrets
openssl rand -base64 36 > secrets/postgres_password
chmod 600 secrets/postgres_password
docker compose up -d
docker compose ps
docker compose exec postgres pg_isready -U tradeos_rehearsal -d tradeos_rehearsal
```

The official PostgreSQL image initializes the database from `POSTGRES_USER`,
`POSTGRES_DB`, and `POSTGRES_PASSWORD_FILE`; those variables affect only an
empty Docker volume.  The volume is intentionally separate from all source
data. [Official PostgreSQL Docker image](https://hub.docker.com/_/postgres)

Create a shell-only database URL.  Do not paste the password into source files;
if it contains URL-reserved characters, percent-encode it first.

```bash
REHEARSAL_PASSWORD="$(tr -d '\n' < secrets/postgres_password)"
export TRADE_OS_DATABASE_URL="postgresql://tradeos_rehearsal:${REHEARSAL_PASSWORD}@127.0.0.1:5433/tradeos_rehearsal"
```

## 2. ECS Docker deployment (optional)

Use this only as an isolated rehearsal service on the existing ECS host.  It
does not replace `trade-os`, `cloudflared`, or the running SQLite application.

1. Copy only `deploy/postgres-rehearsal/` to a separate directory such as
   `/opt/trade-os-postgres-rehearsal/`.
2. Create `.env` and `secrets/postgres_password` there with the same permissions
   as the local procedure.
3. Start only the rehearsal Compose project:

```bash
cd /opt/trade-os-postgres-rehearsal
docker compose up -d
docker compose ps
```

4. Keep it loopback-only.  If a local development machine needs access, use an
   SSH tunnel rather than changing firewall rules:

```bash
ssh -N -L 5433:127.0.0.1:5433 <configured-ecs-host>
```

5. In a separate local shell, set `TRADE_OS_DATABASE_URL` to the local tunnel
   URL from section 1.  Never set that variable for the production service.

## 3. Source snapshot for the rehearsal

### Trosa

Use an existing consistent snapshot from Trosa's `backups/` tree.  A snapshot
must contain `system.db`, `hamid.db`, `amy.db`, and `kelley.db`.  Copy that
snapshot to a dedicated rehearsal input directory, for example:

```text
<rehearsal-input>/trosa/system.db
<rehearsal-input>/trosa/hamid.db
<rehearsal-input>/trosa/amy.db
<rehearsal-input>/trosa/kelley.db
```

Do not copy a live SQLite file together with an uncoordinated `-wal` file.  The
Trosa backup process already produces a consistent SQLite snapshot.

### sela

Copy these files as a point-in-time input set; the rehearsal process opens them
read-only:

```text
<rehearsal-input>/sela/candidates.json
<rehearsal-input>/sela/feedback_events.json
<rehearsal-input>/sela/search_memory.json
<rehearsal-input>/sela/activity_events.sqlite3
```

Before copying, pause local sela writes or use a filesystem snapshot.  This is
for input consistency only; it does not change production read/write routing.

## 4. Preflight manifest and migration log

Install Trosa dependencies in the rehearsal checkout, then generate a
hash-backed manifest.  This is a read-only operation on the copied inputs.

```bash
cd /Users/luoxin/Desktop/Trosa
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python tools/unified_postgres_migration.py \
  --trosa-data-dir <rehearsal-input>/trosa \
  --sela-data-dir <rehearsal-input>/sela \
  --write-manifest <rehearsal-output>/source-manifest.json
```

The manifest records source SHA-256, SQLite integrity status, source table
counts, sela record counts, the canonical schema hash, and preflight errors.
Keep it immutable as the first migration log artifact.

## 5. Schema initialization

With `TRADE_OS_DATABASE_URL` exported in the current shell only:

```bash
.venv/bin/python tools/unified_postgres_migration.py \
  --database-url "$TRADE_OS_DATABASE_URL" \
  --apply-schema \
  --verify-schema | tee <rehearsal-output>/schema-verification.json
```

The command is safe to retry.  It initializes only the rehearsal PostgreSQL
database and verifies the five target schemas plus required tables.

## 6. Trosa import flow

The Trosa loader must process each copied source database in this order:

1. `system.db`: organization seed, users, membership roles and settings.
2. All user databases: raw source rows into `audit.import_batches` and
   `audit.legacy_records` with original database name, table name, legacy ID,
   row number and row payload.
3. Create or resolve `core.companies`, domains, people and contact methods.
4. Create `trosa.accounts`, then contacts, tasks, Timeline, messages, inbox,
   research, AI records and web-monitor history.
5. Load Agent, undo, integration and import history into `audit`.
6. Load `customer_files` as metadata only.  The binary remains in the copied
   rehearsal input until an object-storage rehearsal is separately approved.

Every record must retain `legacy_payload` or `audit.legacy_records` provenance.
Uncertain company matches go to `audit.migration_issues`; they are never
silently merged or dropped.

## 7. sela import flow

1. Store raw JSON rows and SQLite activity rows in the import batch audit.
2. Resolve Candidate company/domain/email references against `core` using
   verified domain first, then human-reviewed name/country candidates.
3. Write Candidate workflow state to `sela.prospects`, research to
   `sela.prospect_research` and sources to `sela.prospect_evidence`.
4. Write draft/provider identifiers to `sela.outreach_messages`.
5. Append every `feedback_events.json` record to `sela.prospect_events`; an
   unresolved Candidate reference remains valid and is reported, not dropped.
6. Append every `activity_events.sqlite3.activity_events` row to
   `sela.run_activity_events`.
7. Store `search_memory.json` as typed `sela.search_memory_entries` records,
   preserving each original payload.

No sela event is copied into a Trosa table.  Trosa Timeline can later display a
cross-module view by reference; its event source remains `sela`.

## 8. Acceptance report

Write one `migration-verification.json` using this shape:

```json
{
  "rehearsal_id": "2026-09-01T120000+0800",
  "source_manifest_sha256": "...",
  "schema_verification": {"ok": true},
  "sources": {
    "trosa": {"integrity": "ok", "tables": {}},
    "sela": {"candidates": 0, "feedback_events": 0, "activity_events": 0}
  },
  "target": {"core": {}, "trosa": {}, "sela": {}, "audit": {}},
  "reconciliation": {
    "exact_count_checks": [],
    "foreign_key_checks": [],
    "unresolved_entities": [],
    "unresolved_events": [],
    "attachment_hash_checks": []
  },
  "result": "passed|failed",
  "approved_by": "",
  "created_at": ""
}
```

The report passes only when all source integrity checks pass, all append-only
event counts reconcile exactly, every target schema check passes, and every
unresolved identity/event is explicitly listed for review.

## 9. Rollback

The rehearsal rollback is limited to the target container:

```bash
cd /Users/luoxin/Desktop/Trosa/deploy/postgres-rehearsal
docker compose down -v
```

This removes only the named rehearsal PostgreSQL volume.  Preserve the copied
input snapshot and all manifests/reports.  It does not alter Trosa production
SQLite, sela JSON/SQLite, production application configuration, or production
attachments.
