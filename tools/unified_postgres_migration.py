#!/usr/bin/env python3
"""Preflight and schema gate for the unified Trade OS PostgreSQL cutover.

This tool is deliberately unable to mutate SQLite or sela JSON sources.  It
creates a hash-backed manifest before a maintenance window and can apply or
verify the canonical PostgreSQL schema when an explicit DSN is supplied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from tools.postgres_source_inventory import discover_trosa_db_names
except ModuleNotFoundError:  # direct ``python tools/...`` invocation
    from postgres_source_inventory import discover_trosa_db_names


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATHS = (
    ROOT / "migrations" / "0001_unified_trade_os.sql",
    ROOT / "migrations" / "0002_postgres_runtime.sql",
    ROOT / "migrations" / "0003_postgres_app_compat.sql",
    ROOT / "migrations" / "0004_postgres_runtime_surfaces.sql",
    ROOT / "migrations" / "0005_postgres_runtime_write_fixes.sql",
    ROOT / "migrations" / "0006_postgres_runtime_surface_writes.sql",
    ROOT / "migrations" / "0007_postgres_runtime_hardening.sql",
)
TARGET_SCHEMAS = ("identity", "core", "trosa", "sela", "audit", "trade_os_compat")
TARGET_TABLES = (
    "audit.schema_migrations",
    "identity.organizations",
    "identity.users",
    "core.companies",
    "core.contact_methods",
    "trosa.accounts",
    "trosa.timeline_events",
    "sela.prospects",
    "sela.prospect_events",
    "sela.run_activity_events",
    "audit.import_batches",
    "audit.legacy_records",
    "trosa.account_legacy_refs",
    "trosa.legacy_row_refs",
    "trosa.contact_legacy_refs",
    "trosa.weekly_reports",
    "trade_os_compat.app_settings",
    "trade_os_compat.customer_file_rows",
    "trade_os_compat.operation_log_rows",
    "trade_os_compat.agent_proposal_rows",
    "trade_os_compat.agent_action_rows",
    "trade_os_compat.undo_action_rows",
    "trade_os_compat.gmail_message_state_rows",
    "trade_os_compat.communication_source_rows",
    "trade_os_compat.communication_source_item_rows",
    "trosa.email_message_receipts",
    "trosa.email_delivery_events",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def schema_sha256() -> str:
    digest = hashlib.sha256()
    for path in SCHEMA_PATHS:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sqlite_inventory(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        tables = []
        for name, sql in rows:
            count = connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            tables.append({"name": name, "rows": count, "ddl": sql or ""})
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        return {"sha256": sha256(path), "bytes": path.stat().st_size,
                "integrity": integrity, "tables": tables}
    finally:
        connection.close()


def json_inventory(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        detail: dict[str, Any] = {"root_type": "list", "rows": len(payload)}
    elif isinstance(payload, dict):
        detail = {"root_type": "object", "keys": sorted(payload),
                  "rows_by_key": {key: len(value) for key, value in payload.items()
                                  if isinstance(value, (list, dict))}}
    else:
        detail = {"root_type": type(payload).__name__}
    return {"sha256": sha256(path), "bytes": path.stat().st_size, **detail}


def source_manifest(trosa_data_dir: Path, sela_data_dir: Path) -> dict[str, Any]:
    errors: list[str] = []
    trosa: dict[str, Any] = {}
    trosa_names, discovery_errors = discover_trosa_db_names(trosa_data_dir)
    errors.extend(discovery_errors)
    for name in trosa_names:
        path = trosa_data_dir / name
        if not path.is_file():
            if f"missing Trosa source: {path}" not in errors:
                errors.append(f"missing Trosa source: {path}")
            continue
        trosa[name] = sqlite_inventory(path)

    sela: dict[str, Any] = {}
    for name in ("candidates.json", "feedback_events.json", "search_memory.json"):
        path = sela_data_dir / name
        if not path.is_file():
            errors.append(f"missing sela source: {path}")
            continue
        sela[name] = json_inventory(path)
    for name in ("activity_events.sqlite3",):
        path = sela_data_dir / name
        if not path.is_file():
            errors.append(f"missing sela source: {path}")
            continue
        sela[name] = sqlite_inventory(path)

    candidates = sela.get("candidates.json", {}).get("rows")
    feedback = sela.get("feedback_events.json", {}).get("rows")
    activity_tables = sela.get("activity_events.sqlite3", {}).get("tables", [])
    activity = next((item["rows"] for item in activity_tables
                     if item["name"] == "activity_events"), None)
    checks = {
        "required_trosa_sources_present": not any("Trosa" in item for item in errors),
        "required_sela_sources_present": not any("sela" in item for item in errors),
        "candidates_readable": isinstance(candidates, int),
        "feedback_readable": isinstance(feedback, int),
        "activity_events_readable": isinstance(activity, int),
    }
    return {
        "generated_at": iso_now(),
        "schema_sha256": schema_sha256(),
        "trosa": trosa,
        "sela": sela,
        "expected_counts": {"sela_candidates": candidates, "sela_feedback_events": feedback,
                            "sela_activity_events": activity},
        "checks": checks,
        "errors": errors,
        "ready_for_dry_run": not errors and all(checks.values()),
    }


def postgres_connection(dsn: str):
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - installation is deployment-specific
        raise RuntimeError("psycopg is required; install Trosa requirements first") from exc
    return psycopg.connect(dsn, autocommit=False)


def apply_schema(dsn: str) -> None:
    with postgres_connection(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE SCHEMA IF NOT EXISTS audit")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS audit.schema_migrations (
                    name TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            connection.commit()
            applied = {
                name: digest for name, digest in cursor.execute(
                    "SELECT name, sha256 FROM audit.schema_migrations"
                )
            }
            connection.commit()
            for schema_path in SCHEMA_PATHS:
                name = schema_path.name
                digest = sha256(schema_path)
                if applied.get(name) == digest:
                    continue
                cursor.execute(schema_path.read_text(encoding="utf-8"))
                cursor.execute(
                    """
                    INSERT INTO audit.schema_migrations (name, sha256)
                    VALUES (%s, %s)
                    ON CONFLICT (name) DO UPDATE SET sha256=excluded.sha256,
                      applied_at=now()
                    """,
                    (name, digest),
                )
                connection.commit()


def verify_schema(dsn: str) -> dict[str, Any]:
    with postgres_connection(dsn) as connection:
        with connection.cursor() as cursor:
            schemas = {row[0] for row in cursor.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name = ANY(%s)", (list(TARGET_SCHEMAS),)
            )}
            tables = {f"{schema}.{name}" for schema, name in cursor.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_type = 'BASE TABLE' AND table_schema = ANY(%s)",
                (list(TARGET_SCHEMAS),)
            )}
            applied = {
                name: digest for name, digest in cursor.execute(
                    "SELECT name, sha256 FROM audit.schema_migrations"
                )
            }
    expected_migrations = {path.name: sha256(path) for path in SCHEMA_PATHS}
    migrations = {
        name: applied.get(name) == digest
        for name, digest in expected_migrations.items()
    }
    return {
        "schemas": {name: name in schemas for name in TARGET_SCHEMAS},
        "tables": {name: name in tables for name in TARGET_TABLES},
        "migrations": migrations,
        "ok": (
            set(TARGET_SCHEMAS).issubset(schemas)
            and set(TARGET_TABLES).issubset(tables)
            and all(migrations.values())
        ),
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--trosa-data-dir", type=Path, help="Directory containing system.db and user SQLite files")
    value.add_argument("--sela-data-dir", type=Path, help="Directory containing sela JSON/SQLite ledgers")
    value.add_argument("--write-manifest", type=Path, help="Write read-only source manifest to this path")
    value.add_argument("--database-url", help="Explicit PostgreSQL DSN; never read from a source ledger")
    value.add_argument("--apply-schema", action="store_true", help="Apply canonical schema to --database-url")
    value.add_argument("--verify-schema", action="store_true", help="Verify canonical schema in --database-url")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.apply_schema or args.verify_schema:
        if not args.database_url:
            parser().error("--database-url is required for PostgreSQL actions")
    if args.apply_schema:
        apply_schema(args.database_url)
    if args.verify_schema:
        print(json.dumps(verify_schema(args.database_url), ensure_ascii=False, indent=2))
    if args.write_manifest:
        if not args.trosa_data_dir or not args.sela_data_dir:
            parser().error("--trosa-data-dir and --sela-data-dir are required for --write-manifest")
        manifest = source_manifest(args.trosa_data_dir, args.sela_data_dir)
        args.write_manifest.parent.mkdir(parents=True, exist_ok=True)
        args.write_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0 if manifest["ready_for_dry_run"] else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
