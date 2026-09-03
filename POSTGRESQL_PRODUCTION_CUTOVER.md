# Trade OS PostgreSQL production cutover

The existing `trade-os.service` and `/var/lib/trade-os` SQLite store remain
the active production path until the final approval point. The target database
is a separate Compose project in `deploy/postgres-production/`, bound only to
`127.0.0.1:5432`.

## Prepare without switching traffic

1. Create a complete application-consistent SQLite snapshot. The source set
   is `system.db` plus every `<username>.db` registered in `system.db`,
   including inactive members. A missing or unregistered database is a hard
   failure.
2. Start the production PostgreSQL target with a new password and volume.
3. Apply and verify migrations 0001–0006.
4. Import only from the immutable Trosa snapshot and a point-in-time sela
   snapshot. Keep the import report, source hashes, and schema hashes.
5. Run `deploy/postgres-production/backup.sh`, copy the dump to an independent
   host/object store, and run `restore-check.sh` against that dump.
6. Run the Trosa PostgreSQL rehearsal service and the local sela PostgreSQL
   process through the same target. Check health, list/detail pages, write
   paths, Agent Gateway idempotency, Undo, Activity, Run, Home, and Gmail
   state without sending a test email.

## Final approval gate

Immediately before the switch, verify:

- the SQLite source service is healthy and the final source snapshot hash is
  recorded;
- PostgreSQL is healthy, all six migrations are present, and the import report
  has no unresolved issues;
- all core foreign-key/orphan/domain checks pass;
- the independent dump and restore check pass;
- the Sela SSH tunnel is up and its `PGPASSFILE` has mode 600;
- a rollback target (the current release and the final SQLite snapshot) is
  recorded.

Only after the user confirms at this point may the operator stop the old
writer, set `TRADE_OS_DATA_BACKEND=postgres` and
`TRADE_OS_DATABASE_URL` for Trosa, activate the sela PostgreSQL LaunchAgent,
and start the service. The switch must be followed by health and functional
checks before any old source is retired.

## Rollback

If the post-switch checks fail, stop the PostgreSQL-backed writer, restore the
previous Trosa release and SQLite environment, and start the old service. The
PostgreSQL target is not deleted; its dump, import report, and audit history
remain available for diagnosis. Never run the SQLite and PostgreSQL writers at
the same time.
