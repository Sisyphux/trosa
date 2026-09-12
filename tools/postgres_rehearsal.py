#!/usr/bin/env python3
"""Manage the isolated local PostgreSQL rehearsal database.

The rehearsal is intentionally self-contained: a native PostgreSQL cluster is
kept below ``.local/postgres-rehearsal`` and listens only on 127.0.0.1:55432.
No production DSN, ECS service, Docker volume, or application data directory
is ever used by this tool.

Typical workflow::

    python3 tools/postgres_rehearsal.py test

The command creates a fresh database, applies every migration, loads a small
deterministic fixture, verifies the schema contract, and runs the PostgreSQL
integration tests.  Individual ``start``, ``init``, ``migrate``, ``fixture``,
``verify``, ``env`` and ``stop`` commands are available for development.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
STATE_DIR = ROOT / ".local" / "postgres-rehearsal"
DATA_DIR = STATE_DIR / "data"
SOCKET_DIR = STATE_DIR / "socket"
LOG_PATH = STATE_DIR / "postgres.log"
DEFAULT_PORT = 55432
DEFAULT_DATABASE = "trosa_rehearsal"
FIXTURE_KEY = "trosa-postgres-rehearsal-v1"
FIXTURE_USER = "hamid"
_POSTGRES_BIN_DIRS = (
    Path("/opt/homebrew/opt/postgresql@17/bin"),
    Path("/usr/local/opt/postgresql@17/bin"),
    Path("/opt/homebrew/opt/postgresql/bin"),
    Path("/usr/local/opt/postgresql/bin"),
)


def _port() -> int:
    value = os.environ.get("TROSA_REHEARSAL_PORT", str(DEFAULT_PORT)).strip()
    try:
        port = int(value)
    except ValueError as exc:
        raise RuntimeError("TROSA_REHEARSAL_PORT must be an integer") from exc
    if not 1024 <= port <= 65535:
        raise RuntimeError("TROSA_REHEARSAL_PORT must be between 1024 and 65535")
    return port


def _database() -> str:
    value = os.environ.get("TROSA_REHEARSAL_DB", DEFAULT_DATABASE).strip().lower()
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", value):
        raise RuntimeError("TROSA_REHEARSAL_DB must be a simple local PostgreSQL database name")
    if value != DEFAULT_DATABASE:
        raise RuntimeError(
            f"rehearsal database is fixed to {DEFAULT_DATABASE!r}; "
            "the tool will not operate on an arbitrary database"
        )
    return value


def _user() -> str:
    value = os.environ.get("TROSA_REHEARSAL_USER", getpass.getuser()).strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError("TROSA_REHEARSAL_USER must match the local PostgreSQL role created by initdb")
    return value


def _require_tools() -> None:
    missing = [name for name in ("initdb", "pg_ctl", "pg_isready") if not _which(name)]
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(
            f"Missing PostgreSQL tools: {names}. Install PostgreSQL 17 locally "
            "(for example, `brew install postgresql@17`) and retry."
        )


def _which(name: str) -> str | None:
    directories = [Path(directory) for directory in os.environ.get("PATH", "").split(os.pathsep) if directory]
    directories.extend(_POSTGRES_BIN_DIRS)
    for directory in directories:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _tool(name: str) -> str:
    path = _which(name)
    if not path:
        raise RuntimeError(f"Missing PostgreSQL tool: {name}")
    return path


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "LC_ALL": "C"},
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise RuntimeError(f"{' '.join(command)} failed: {detail[-4000:]}")
    return result


def _pg_ctl_status() -> bool:
    if not (DATA_DIR / "PG_VERSION").is_file() or not _which("pg_ctl"):
        return False
    return _run([_tool("pg_ctl"), "-D", str(DATA_DIR), "status"], check=False).returncode == 0


def _ready(database: str = "postgres") -> bool:
    if not _which("pg_isready"):
        return False
    return _run(
        [_tool("pg_isready"), "-h", "127.0.0.1", "-p", str(_port()), "-d", database],
        check=False,
    ).returncode == 0


def _ensure_cluster() -> None:
    _require_tools()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SOCKET_DIR.mkdir(parents=True, exist_ok=True)
    if (DATA_DIR / "PG_VERSION").is_file():
        return
    if DATA_DIR.exists() and any(DATA_DIR.iterdir()):
        raise RuntimeError(
            f"PostgreSQL data directory is incomplete: {DATA_DIR}. "
            "Move it aside and run init again; the tool will not delete it automatically."
        )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _run([
        _tool("initdb"), "-D", str(DATA_DIR), "--no-locale", "--encoding=UTF8",
        "--auth-local=trust", "--auth-host=trust",
    ])


def start_server() -> None:
    _ensure_cluster()
    if _pg_ctl_status():
        if not _ready():
            raise RuntimeError("The rehearsal cluster is running but not accepting local connections")
        return
    # A different process must never be mistaken for this cluster.  Refuse to
    # start if the dedicated port is already occupied by an unrelated server.
    if _ready():
        raise RuntimeError(
            f"127.0.0.1:{_port()} is already in use by another PostgreSQL server; "
            "the rehearsal will not connect to it"
        )
    _run([
        _tool("pg_ctl"), "-D", str(DATA_DIR),
        "-o", f"-p {_port()} -h 127.0.0.1 -k {SOCKET_DIR}",
        "-l", str(LOG_PATH), "-w", "start",
    ])
    if not _ready():
        raise RuntimeError(f"Rehearsal PostgreSQL did not become ready; inspect {LOG_PATH}")


def stop_server() -> None:
    if not (DATA_DIR / "PG_VERSION").is_file() or not _pg_ctl_status():
        return
    _run([_tool("pg_ctl"), "-D", str(DATA_DIR), "-m", "fast", "-w", "stop"])


def _dsn(database: str | None = None) -> str:
    return (
        f"postgresql://{quote(_user(), safe='')}@127.0.0.1:{_port()}"
        f"/{quote(database or _database(), safe='')}"
    )


def _assert_local_dsn(dsn: str) -> None:
    parsed = urlparse(dsn)
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise RuntimeError("rehearsal requires a PostgreSQL URL")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("rehearsal refuses non-loopback PostgreSQL hosts")


def _connect(database: str | None = None):
    import psycopg

    dsn = _dsn(database)
    _assert_local_dsn(dsn)
    return psycopg.connect(dsn, autocommit=True)


def ensure_database() -> None:
    start_server()
    with _connect("postgres") as connection:
        row = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (_database(),)
        ).fetchone()
        if row is None:
            connection.execute(f'CREATE DATABASE "{_database()}"')


def reset_database() -> None:
    """Recreate exactly the local rehearsal database, never any other DB."""
    ensure_database()
    with _connect("postgres") as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{_database()}" WITH (FORCE)')
        connection.execute(f'CREATE DATABASE "{_database()}"')


def _configure_environment() -> str:
    dsn = _dsn()
    _assert_local_dsn(dsn)
    os.environ["TRADE_OS_DATA_BACKEND"] = "postgres"
    os.environ["TRADE_OS_DATABASE_URL"] = dsn
    os.environ["TROSA_REHEARSAL_DATABASE_URL"] = dsn
    os.environ["TROSA_REHEARSAL"] = "1"
    os.environ["TROSA_REHEARSAL_PORT"] = str(_port())
    os.environ["TROSA_REHEARSAL_DB"] = _database()
    return dsn


def migrate() -> dict[str, Any]:
    dsn = _configure_environment()
    ensure_database()
    from tools.unified_postgres_migration import apply_schema, verify_schema

    apply_schema(dsn)
    # The application startup path also exercises the migration ledger and
    # creates the built-in identity rows needed by the compatibility boundary.
    import db

    db.init_postgres_store()
    result = verify_schema(dsn)
    if not result["ok"]:
        raise RuntimeError(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def _fixture_customer(connection: Any) -> int | None:
    row = connection.execute(
        """SELECT legacy_customer_id
             FROM trosa.account_legacy_refs
            WHERE organization_id=trosa.compat_org_id()
              AND legacy_user_id=?
              AND legacy_payload->>'rehearsal_fixture'=?
            ORDER BY legacy_customer_id
            LIMIT 1""",
        (FIXTURE_USER, FIXTURE_KEY),
    ).fetchone()
    return int(row[0]) if row else None


def load_fixture() -> dict[str, int]:
    """Load an idempotent fixture through the same canonical write boundary as the app."""
    _configure_environment()
    ensure_database()
    from tools.unified_postgres_migration import verify_schema

    if not verify_schema(_dsn())["ok"]:
        migrate()
    import db
    import trosa_domain

    db.init_postgres_store()
    db.set_db_user(FIXTURE_USER)
    connection = db.get_db()
    try:
        customer_id = _fixture_customer(connection)
        if customer_id is None:
            customer_id = trosa_domain.create_customer(
                connection,
                values={
                    "name": "PostgreSQL Rehearsal Customer",
                    "company": "Rehearsal Acrylic Co",
                    "country": "US",
                    "level": "A",
                    "website": "https://rehearsal.example",
                    "profile": "deterministic local fixture",
                    "field": "acrylic sheet",
                    "industry": "manufacturing",
                    "company_size": "11-50",
                    "annual_revenue": "1000000",
                    "tags": "rehearsal",
                    "notes": "fixture note",
                    "system_notes": "fixture system note",
                    "import_source": "postgres-rehearsal",
                    "last_contact": "2026-09-10",
                    "next_follow_up": "2026-09-12",
                    "manual_next_follow": True,
                    "business_stage": "",
                    "business_role": "",
                    "customer_judgment": "",
                    "rehearsal_fixture": FIXTURE_KEY,
                },
            )
        else:
            # A repeated local test may have edited or completed the fixture.
            # Restore only this explicitly marked rehearsal aggregate so the
            # test remains deterministic without touching any other Customer.
            trosa_domain.update_customer(
                connection,
                customer_id=customer_id,
                values={
                    "name": "PostgreSQL Rehearsal Customer",
                    "company": "Rehearsal Acrylic Co",
                    "country": "US",
                    "level": "A",
                    "website": "https://rehearsal.example",
                    "profile": "deterministic local fixture",
                    "field": "acrylic sheet",
                    "industry": "manufacturing",
                    "company_size": "11-50",
                    "annual_revenue": "1000000",
                    "tags": "rehearsal",
                    "notes": "fixture note",
                    "system_notes": "fixture system note",
                    "import_source": "postgres-rehearsal",
                    "last_contact": "2026-09-10",
                    "next_follow_up": "2026-09-12",
                    "manual_next_follow": True,
                    "business_stage": "",
                    "business_role": "",
                    "customer_judgment": "",
                },
            )

        contact_row = connection.execute(
            """SELECT legacy_contact_id
                 FROM trosa.contact_legacy_refs
                WHERE organization_id=trosa.compat_org_id()
                  AND legacy_user_id=? AND legacy_customer_id=?
                  AND contact_method_id IN (
                      SELECT id FROM core.contact_methods
                       WHERE normalized_value=?
                  )
                ORDER BY legacy_contact_id LIMIT 1""",
            (FIXTURE_USER, customer_id, "buyer@rehearsal.example"),
        ).fetchone()
        if contact_row:
            contact_id = int(contact_row[0])
        else:
            contact_id = trosa_domain.create_contact(
                connection,
                customer_id=customer_id,
                values={
                    "name": "Rehearsal Buyer",
                    "title": "Purchasing Manager",
                    "email": "buyer@rehearsal.example",
                    "phone": "+1-555-0100",
                    "preferred_channel": "email",
                    "contact_type": "person",
                    "is_primary": True,
                    "notes": "fixture contact",
                },
                created_at="2026-09-11 09:00:00",
            )

        interaction_id = trosa_domain.record_external_interaction(
            connection,
            customer_id=customer_id,
            contact_id=contact_id,
            content="Fixture customer replied to the rehearsal message",
            occurred_on="2026-09-11",
            direction="inbound",
            source="postgres-rehearsal",
            source_reference=f"{FIXTURE_KEY}:interaction",
            activity_type="customer_reply",
            result="received",
            next_plan="send sample quotation",
            is_reported=True,
        )
        task_id = trosa_domain.merge_open_task(
            connection,
            customer_id=customer_id,
            title="Send sample quotation",
            content="Send the acrylic sheet sample quotation",
            reason="rehearsal fixture",
            due_on="2026-09-12",
            source_interaction_id=interaction_id,
            now="2026-09-11 09:00:00",
        )
        inbox_id = trosa_domain.create_inbox_item(
            connection,
            item_type="rehearsal_review",
            title="Review rehearsal customer",
            content="Deterministic local PostgreSQL fixture",
            customer_id=customer_id,
            dedupe_key=f"{FIXTURE_KEY}:inbox",
            status="open",
            created_at="2026-09-11 09:00:00",
        )
        trosa_domain.set_customer_stage(
            connection, customer_ids=[customer_id], stage="成交"
        )
        trosa_domain.set_customer_judgment(
            connection, customer_id=customer_id, judgment="fixture-qualified"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "customer_id": int(customer_id),
        "contact_id": int(contact_id),
        "interaction_id": int(interaction_id),
        "task_id": int(task_id),
        "inbox_id": int(inbox_id),
    }


def verify() -> dict[str, Any]:
    _configure_environment()
    ensure_database()
    from tools.unified_postgres_migration import verify_schema

    result = verify_schema(_dsn())
    if not result["ok"]:
        raise RuntimeError(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def run_integration_tests() -> int:
    env = os.environ.copy()
    _configure_environment()
    env.update({
        "TRADE_OS_DATA_BACKEND": "postgres",
        "TRADE_OS_DATABASE_URL": _dsn(),
        "TROSA_REHEARSAL_DATABASE_URL": _dsn(),
        "TROSA_REHEARSAL": "1",
        "TROSA_REHEARSAL_PORT": str(_port()),
        "TROSA_REHEARSAL_DB": _database(),
    })
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests",
         "-p", "test_postgres_rehearsal.py", "-v"],
        cwd=ROOT,
        env=env,
    )
    return result.returncode


def status() -> dict[str, Any]:
    running = _pg_ctl_status()
    database_exists = False
    if running and _ready():
        try:
            with _connect("postgres") as connection:
                database_exists = bool(connection.execute(
                    "SELECT 1 FROM pg_database WHERE datname=%s", (_database(),)
                ).fetchone())
        except Exception:
            database_exists = False
    return {
        "running": running,
        "ready": bool(running and _ready()),
        "database_exists": database_exists,
        "data_dir": str(DATA_DIR),
        "port": _port(),
        "database": _database(),
        "role": _user(),
        "dsn": _dsn(),
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "command",
        choices=("start", "stop", "status", "init", "reset", "migrate", "fixture", "verify", "test", "env"),
        help="rehearsal lifecycle action",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "start":
        start_server()
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif args.command == "stop":
        stop_server()
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif args.command == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif args.command == "init":
        ensure_database()
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif args.command == "reset":
        reset_database()
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif args.command == "migrate":
        print(json.dumps(migrate(), ensure_ascii=False, indent=2))
    elif args.command == "fixture":
        print(json.dumps(load_fixture(), ensure_ascii=False, indent=2))
    elif args.command == "verify":
        print(json.dumps(verify(), ensure_ascii=False, indent=2))
    elif args.command == "test":
        reset_database()
        migrate()
        fixture = load_fixture()
        verified = verify()
        print(json.dumps({"fixture": fixture, "schema": verified}, ensure_ascii=False, indent=2))
        return run_integration_tests()
    elif args.command == "env":
        print(f'export TRADE_OS_DATA_BACKEND=postgres')
        print(f'export TRADE_OS_DATABASE_URL={_dsn()!r}')
        print(f'export TROSA_REHEARSAL_DATABASE_URL={_dsn()!r}')
        print('export TROSA_REHEARSAL=1')
        print(f'export TROSA_REHEARSAL_PORT={_port()}')
        print(f'export TROSA_REHEARSAL_DB={_database()}')
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"postgres rehearsal: {exc}", file=sys.stderr)
        raise SystemExit(2)
