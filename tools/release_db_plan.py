"""Trosa release database planner.

Single source of truth for answering, for any given release:

* are there database migrations the production PostgreSQL has not seen?
* are they safe to apply automatically, or do they need explicit approval?
* what must be backed up and verified before the service switches?

Classification (conservative by design):

* ``none``        — no new migration files vs. the deployed ledger. No DB
                    backup is required by the release itself; no migration
                    phase runs.
* ``compatible``  — only new forward ``.sql`` files without destructive
                    statements. The release runner takes a server-local
                    pre-migration logical dump, applies the migrations, then
                    verifies the schema contract. Fully automatic.
* ``destructive`` — any new migration (or tracked runtime file diff) matches
                    a destructive pattern (DROP/TRUNCATE/DELETE FROM/ALTER ..
                    DROP, ...). The release runner refuses to proceed unless
                    the operator passes an explicit allow flag AND a verified
                    backup exists. Data backfills (UPDATE/INSERT without a
                    destructive match) stay ``compatible``.

The same module is used by:

* ``deploy/cloud/release-remote.sh`` on ECS (explicit migration phase),
* ``deploy/cloud/trosa-release`` for the local pre-flight summary,
* ``tests/test_release_mechanism.py`` for regression coverage.

It never touches a database; it only inspects files. Anything that needs
live ledger state passes ``applied_names`` explicitly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys

# Mirrors the heuristic used by deploy/cloud/release-commit.sh and the server-
# side runner so the local gate and ECS agree on what "destructive" means.
# Scope is data loss: dropping tables/columns/schemas, truncating, or
# deleting rows at migration time. Dropping a CONSTRAINT or INDEX is routine
# index-replacement practice in this repo (the new unique index is created in
# the same migration) and stays "compatible" — which still takes a verified
# pre-migration backup before applying.
DESTRUCTIVE_PATTERN = re.compile(
    r"(DROP\s+(TABLE|TABLES|COLUMN|SCHEMA|DATABASE)"
    r"|TRUNCATE\s+(TABLE|TABLES)"
    r"|DELETE\s+FROM"
    r"|ALTER\s+TABLE[^;]*DROP\s+COLUMN)",
    re.IGNORECASE,
)

# Files whose staged diff makes a release "database sensitive" even when no
# new migration file exists (runtime migration code changed).
DB_SENSITIVE_PATH_PREFIXES = (
    "migrations/",
    "db.py",
    "postgres_compat.py",
    "postgres_schema_contract.py",
    "tools/unified_postgres_migration.py",
    "tools/unified_postgres_import.py",
    "deploy/postgres-production/",
)

MIGRATION_SUFFIX = ".sql"

RELEASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def is_db_sensitive_path(path: str) -> bool:
    """Whether a changed path can alter database behavior.

    ``migrations/`` only counts for ``.sql`` files: documentation such as
    ``migrations/README.md`` can describe destructive keywords without ever
    running at migration time, and must not force a backup or trip the
    destructive heuristic.
    """
    for prefix in DB_SENSITIVE_PATH_PREFIXES:
        if prefix == "migrations/":
            if path.startswith(prefix) and path.endswith(MIGRATION_SUFFIX):
                return True
            continue
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return True
    return False


def is_valid_release_id(release_id: str) -> bool:
    """Release ids must be safe to use as a directory name and symlink target."""
    return bool(RELEASE_ID_PATTERN.match(release_id or ""))


def is_destructive_sql(sql_text: str) -> bool:
    """True when the SQL contains a statement class we never auto-apply."""
    stripped = _strip_sql_comments_and_strings(sql_text)
    return bool(DESTRUCTIVE_PATTERN.search(stripped))


def _strip_function_bodies(sql_text: str) -> str:
    """Remove CREATE FUNCTION/PROCEDURE plpgsql bodies from the text.

    Trigger/writer functions routinely contain ``DELETE FROM ... WHERE id=``
    row-sync plumbing that only runs on later DML — it is not a
    migration-time destructive operation. ``DO`` blocks are deliberately
    kept: they execute during the migration itself, so a DROP/DELETE inside
    one must still count as destructive.
    """
    func_start = re.compile(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\b",
        re.IGNORECASE,
    )
    as_tag = re.compile(r"\bAS\s+(\$[A-Za-z_][A-Za-z_0-9]*\$|\$\$)", re.IGNORECASE)
    parts: list[str] = []
    pos = 0
    for match in func_start.finditer(sql_text):
        tag_match = as_tag.search(sql_text, match.end(), match.end() + 4000)
        if not tag_match:
            continue
        tag = tag_match.group(1)
        body_start = tag_match.end()
        body_end = sql_text.find(tag, body_start)
        if body_end == -1:
            continue
        parts.append(sql_text[pos:body_start])
        parts.append(" ")
        pos = body_end + len(tag)
    parts.append(sql_text[pos:])
    return "".join(parts)


def _strip_sql_comments_and_strings(sql_text: str) -> str:
    """Remove function bodies, comments and quoted literals so keywords inside
    prose, trigger plumbing or string data do not trigger a verdict. Only
    migration-time executable code (top level + DO blocks) is judged."""
    text = _strip_function_bodies(sql_text)
    # Remove /* ... */ blocks.
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    # Remove -- line comments.
    text = re.sub(r"--[^\n]*", " ", text)
    # Remove single-quoted string literals ('' escapes included).
    text = re.sub(r"'(?:[^']|'')*'", "''", text)
    # Remove double-quoted identifiers.
    text = re.sub(r'"(?:[^"]|"")*"', '""', text)
    return text


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def list_migration_files(migrations_dir: str) -> list[str]:
    """Sorted migration file names present in a release directory."""
    try:
        entries = os.listdir(migrations_dir)
    except OSError:
        return []
    return sorted(
        name for name in entries
        if name.endswith(MIGRATION_SUFFIX)
        and os.path.isfile(os.path.join(migrations_dir, name))
    )


def validate_applied_migration_names(applied_names: set[str] | None) -> set[str]:
    """Return a ledger value only when it is a list of migration filenames.

    The planner is deliberately file-only.  A database/transport error is not
    a migration name and must never be allowed to turn the whole history into
    a pending plan (for example, ``LEDGER_UNREADABLE: ...``).  The release
    runner treats a failed ledger query as a terminal error before it invokes
    this planner; this validation is a second boundary for every caller.
    """
    if applied_names is None:
        raise ValueError("applied migration ledger is required")
    if not isinstance(applied_names, set):
        raise ValueError("applied migration ledger must be a set of filenames")
    invalid = sorted(
        name for name in applied_names
        if not isinstance(name, str)
        or not name.endswith(MIGRATION_SUFFIX)
        or os.path.basename(name) != name
        or not name
    )
    if invalid:
        raise ValueError("invalid applied migration filename(s): " + ", ".join(map(str, invalid)))
    return set(applied_names)


def plan_release_db(
    migrations_dir: str,
    applied_names: set[str] | None = None,
    changed_paths: list[str] | None = None,
) -> dict:
    """Build the machine-readable database plan for one release.

    ``applied_names`` is the successfully read live
    ``audit.schema_migrations`` ledger from production. It is required and
    must contain only migration file names. ``changed_paths`` is the list of
    repo paths changed by this release vs. production.
    """
    applied = validate_applied_migration_names(applied_names)
    changed = list(changed_paths or [])
    local_files = list_migration_files(migrations_dir)

    pending = [name for name in local_files if name not in applied]
    destructive_files: list[str] = []
    for name in pending:
        try:
            with open(os.path.join(migrations_dir, name), "r", encoding="utf-8") as handle:
                contents = handle.read()
        except OSError:
            destructive_files.append(name)  # unreadable == unsafe
            continue
        if is_destructive_sql(contents):
            destructive_files.append(name)

    sensitive_paths = [path for path in changed if is_db_sensitive_path(path)]

    if destructive_files:
        category = "destructive"
    elif pending or sensitive_paths:
        category = "compatible" if pending else "sensitive_runtime"
    else:
        category = "none"

    return {
        "category": category,
        "pending_migrations": pending,
        "destructive_files": destructive_files,
        "sensitive_paths": sensitive_paths,
        "requires_backup": category in ("compatible", "destructive", "sensitive_runtime"),
        "allow_auto_apply": category in ("none", "compatible", "sensitive_runtime"),
        "migration_count": len(pending),
    }


def main(argv: list[str]) -> int:
    """CLI: release_db_plan.py <migrations_dir> [--applied a,b,c] [--changed p1,p2]
    or:    release_db_plan.py --check-files <repo_root> <file...> (exit 1 + names
           when any listed SQL file is destructive)."""
    if len(argv) > 1 and argv[1] == "--check-files":
        if len(argv) < 4:
            print("usage: release_db_plan.py --check-files <repo_root> <file...>",
                  file=sys.stderr)
            return 2
        root = argv[2]
        bad = []
        for rel in argv[3:]:
            path = os.path.join(root, rel)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    contents = handle.read()
            except OSError:
                bad.append(rel)
                continue
            if is_destructive_sql(contents):
                bad.append(rel)
        if bad:
            print("\n".join(bad))
            return 1
        return 0
    migrations_dir = argv[1] if len(argv) > 1 else "migrations"
    applied: set[str] = set()
    changed: list[str] = []
    index = 2
    while index < len(argv):
        if argv[index] == "--applied" and index + 1 < len(argv):
            applied = {item for item in argv[index + 1].split(",") if item}
            index += 2
        elif argv[index] == "--changed" and index + 1 < len(argv):
            changed = [item for item in argv[index + 1].split(",") if item]
            index += 2
        else:
            print(f"unknown argument: {argv[index]}", file=sys.stderr)
            return 2
    try:
        result = plan_release_db(migrations_dir, applied, changed)
    except ValueError as exc:
        print(f"invalid applied migration ledger: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
