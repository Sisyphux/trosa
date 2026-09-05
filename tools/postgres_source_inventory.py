"""Closed-set discovery for a Trosa SQLite source snapshot.

The production installation can contain invited or deactivated members that
are not part of the default in-code user list.  PostgreSQL migration must
therefore derive the source database set from ``system.db`` and fail closed
when a snapshot is missing a registered member database or contains an
unregistered ``*.db`` file.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any


_SAFE_USER_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _user_value(value: Any) -> str:
    return str(value or "").strip()


def discover_trosa_db_names(data_dir: Path) -> tuple[list[str], list[str]]:
    """Return the complete ordered DB set and any closed-set violations."""
    data_dir = Path(data_dir)
    names = ["system.db"]
    errors: list[str] = []
    system_path = data_dir / "system.db"
    if not system_path.is_file():
        return names, [f"missing Trosa source: {system_path}"]

    connection = sqlite3.connect(f"file:{system_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "select 1 from sqlite_master where type='table' and name='users'"
        ).fetchone()
        if table is None:
            return names, [f"Trosa source has no users table: {system_path}"]
        columns = {
            row[1]
            for row in connection.execute("pragma table_info(users)").fetchall()
        }
        identity_columns = [name for name in ("username", "id") if name in columns]
        if not identity_columns:
            return names, [f"Trosa users table has no username/id column: {system_path}"]
        select_sql = "select " + ", ".join(identity_columns) + " from users"
        # PostgreSQL compatibility routing normalizes owner keys to lower
        # case.  Treat ``Amy.db`` and ``amy.db`` as the same source owner so
        # the importer cannot silently merge two SQLite stores into one user.
        seen_users: set[str] = set()
        for row in connection.execute(select_sql).fetchall():
            values = [row[index] for index in range(len(identity_columns))]
            user = next((_user_value(value) for value in values if _user_value(value)), "")
            if not user:
                errors.append("Trosa system.db contains a user without username/id")
                continue
            if not _SAFE_USER_FILE.fullmatch(user):
                errors.append(f"Trosa user is not a safe database filename: {user!r}")
                continue
            user_key = user.casefold()
            if user_key in seen_users:
                errors.append(f"Trosa system.db contains duplicate user: {user}")
                continue
            seen_users.add(user_key)
            names.append(f"{user}.db")
        if not seen_users:
            errors.append(f"Trosa system.db contains no usable users: {system_path}")
    except sqlite3.Error as exc:
        errors.append(f"Trosa system.db users table unreadable: {exc}")
    finally:
        connection.close()

    expected = set(names)
    present = {path.name for path in data_dir.glob("*.db") if path.is_file()}
    for name in names:
        if not (data_dir / name).is_file():
            errors.append(f"missing Trosa source: {data_dir / name}")
    for name in sorted(present - expected):
        errors.append(f"unregistered Trosa source database: {data_dir / name}")
    return names, errors
