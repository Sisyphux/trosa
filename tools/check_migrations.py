#!/usr/bin/env python3
"""Migration directory integrity gate for parallel development.

The runtime applies every ``migrations/NNNN_*.sql`` in filename order and
records its SHA-256 in ``audit.schema_migrations``.  Two agents working in
parallel can otherwise claim the same next number (git sees different file
names and merges them silently), or leave a gap that hides a migration from
review.  This gate makes those states impossible to merge unnoticed:

* every file must match ``NNNN_lower_snake_case.sql``;
* numbers must be unique (no two tasks claiming the same slot);
* numbers must be contiguous from 0001 with no gaps;
* the runtime registry must equal the directory contents (checked by
  ``tests/test_migration_integrity.py``).

Usage:
    python3 tools/check_migrations.py [--dir <repo-root>]

Exit codes: 0 clean, 1 violations, 2 usage error.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9][a-z0-9_]*\.sql$")


def migration_files(directory: str) -> list[str]:
    """Sorted ``.sql`` names in the migrations directory."""
    try:
        entries = os.listdir(directory)
    except OSError:
        return []
    return sorted(
        name for name in entries
        if name.endswith(".sql") and os.path.isfile(os.path.join(directory, name))
    )


def check_directory(directory: str) -> list[str]:
    """Return human-readable problems; an empty list means the directory is clean."""
    directory = os.path.abspath(directory)
    if not os.path.isdir(directory):
        return [f"migration directory not found: {directory}"]

    names = migration_files(directory)
    if not names:
        return [f"no migration files found in {directory}"]

    problems: list[str] = []
    by_number: dict[int, str] = {}
    for name in names:
        match = MIGRATION_NAME.match(name)
        if not match:
            problems.append(
                f"invalid migration filename: {name} (expected NNNN_snake_name.sql)"
            )
            continue
        number = int(match.group(1))
        if number in by_number:
            problems.append(
                f"duplicate migration number {match.group(1)}: "
                f"{by_number[number]} and {name} (rename the later one to the next free number)"
            )
        else:
            by_number[number] = name

    if by_number:
        highest = max(by_number)
        missing = [f"{n:04d}" for n in range(1, highest + 1) if n not in by_number]
        if missing:
            problems.append("missing migration number(s): " + ", ".join(missing))
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        default=None,
        help="repository root (default: the repository containing this file)",
    )
    args = parser.parse_args(argv[1:])
    root = os.path.abspath(args.dir) if args.dir else os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    directory = os.path.join(root, "migrations")
    problems = check_directory(directory)
    if problems:
        print("MIGRATION_CHECK_FAILED")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    names = migration_files(directory)
    print(f"MIGRATION_CHECK_OK count={len(names)} latest={names[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
