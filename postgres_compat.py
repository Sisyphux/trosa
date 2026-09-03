"""Small DB-API compatibility layer for the non-production PostgreSQL run.

The web application still speaks the legacy SQLite-shaped repository API
(``?`` parameters, integer compatibility ids, and ``sqlite3.Row`` access).
This module keeps that API stable while routing every statement to the shared
PostgreSQL schemas.  It is deliberately opt-in; production remains on the
existing SQLite factory until an explicit cutover configuration is set.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable

import psycopg
from psycopg import errors
from psycopg.rows import tuple_row


_PRAGMA_TABLE_INFO = re.compile(
    r"^\s*PRAGMA\s+table_info\s*\(\s*([\w]+)\s*\)\s*;?\s*$",
    re.IGNORECASE,
)
_PRAGMA = re.compile(r"^\s*PRAGMA\b", re.IGNORECASE)
_INSERT_OR_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)
_SQLITE_DATETIME = re.compile(r"datetime\s*\(\s*'now'(?:\s*,\s*'localtime')?\s*\)", re.IGNORECASE)
_SQLITE_DATE = re.compile(r"date\s*\(\s*'now'(?:\s*,\s*'localtime')?\s*\)", re.IGNORECASE)
_INSERT_TABLE = re.compile(r"^\s*INSERT\s+(?:INTO\s+)?(?:OR\s+\w+\s+)?([\w.]+)", re.IGNORECASE)
_PSYCOPG_PLACEHOLDER_OR_PERCENT = re.compile(r"%(?![%sbt])", re.IGNORECASE)
_COMPAT_VIEW_INSERT = re.compile(
    r"^\s*INSERT\s+INTO\s+(?:trade_os_compat\.)?"
    r"(users|email_verifications|email_verification_jobs|email_domain_probes|email_logs|"
    r"gmail_message_states|communication_sources|communication_source_items|email_delivery_events|"
    r"import_batches|imported_activity_rows|import_unmatched_customers|weekly_reports)\b",
    re.IGNORECASE,
)


class CompatRow(dict):
    """A mapping that also preserves SQLite's integer-index access."""

    def __init__(self, columns: list[str], values: Iterable[Any]):
        super().__init__(zip(columns, values))
        self._columns = columns

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return super().__getitem__(self._columns[key])
        return super().__getitem__(key)


class _SyntheticCursor:
    """Cursor used for SQLite PRAGMA compatibility statements."""

    def __init__(self, rows: list[tuple[Any, ...]], description: list[tuple[Any, ...]]):
        self._rows = rows
        self.description = description
        self.rowcount = len(rows)
        self.lastrowid = None

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def fetchmany(self, size=None):
        size = size or 1
        rows, self._rows = self._rows[:size], self._rows[size:]
        return rows

    def __iter__(self):
        return iter(self.fetchall())

    def close(self):
        self._rows = []


def _translate_sql(sql: str) -> str:
    text = str(sql)
    if _PRAGMA_TABLE_INFO.match(text):
        return text
    if _PRAGMA.match(text):
        return text
    # PostgreSQL supports the conflict action, but not SQLite's INSERT OR
    # IGNORE spelling.
    text = _INSERT_OR_IGNORE.sub("INSERT INTO", text)
    if re.search(r"\bON\s+CONFLICT\b", text, re.IGNORECASE) is None and _INSERT_OR_IGNORE.search(str(sql)):
        text = text.rstrip().rstrip(';') + " ON CONFLICT DO NOTHING"
    text = _SQLITE_DATETIME.sub("CURRENT_TIMESTAMP", text)
    text = _SQLITE_DATE.sub("CURRENT_DATE", text)
    text = re.sub(r"\bBEGIN\s+IMMEDIATE\b", "BEGIN", text, flags=re.IGNORECASE)
    # All runtime values are bound parameters in the application.  The
    # legacy question-mark placeholder therefore has an unambiguous mapping.
    text = text.replace("?", "%s")
    # PostgreSQL checks ON CONFLICT against the target relation before an
    # INSTEAD OF trigger runs.  The legacy API uses conflict clauses on these
    # SQLite-shaped views, while the trigger itself performs the canonical
    # upsert.  Remove only that view-local clause; ordinary PostgreSQL table
    # statements keep their original conflict semantics.
    if _COMPAT_VIEW_INSERT.match(text):
        text = re.sub(r"\s+ON\s+CONFLICT\b[\s\S]*?(?=;?\s*$)", "", text, flags=re.IGNORECASE)
    # psycopg treats every percent sign as part of its pyformat parameter
    # grammar.  The legacy SQL contains SQLite LIKE literals such as
    # ``NOT LIKE 'outreach_%'``; escape only percent signs that are not a
    # placeholder or an already escaped ``%%`` sequence.
    text = _PSYCOPG_PLACEHOLDER_OR_PERCENT.sub("%%", text)
    return text


class CompatCursor:
    def __init__(self, connection: "CompatConnection", raw):
        self.connection = connection
        self.raw = raw
        self._description: list[tuple[Any, ...]] = []
        self._synthetic: _SyntheticCursor | None = None
        self._lastrowid: int | None = None
        self._insert_table: str | None = None

    @property
    def description(self):
        if self._synthetic is not None:
            return self._synthetic.description
        return self.raw.description

    @property
    def rowcount(self):
        if self._synthetic is not None:
            return self._synthetic.rowcount
        return self.raw.rowcount

    @property
    def lastrowid(self):
        if self._lastrowid is not None:
            return self._lastrowid
        try:
            with self.connection.raw.cursor() as cur:
                cur.execute("SELECT current_setting('trade_os.lastrowid', true)")
                value = cur.fetchone()[0]
            if value not in (None, ""):
                return int(value)
        except (ValueError, TypeError, psycopg.Error):
            pass
        if self._insert_table:
            try:
                with self.connection.raw.cursor() as cur:
                    cur.execute(f"SELECT max(id) FROM {self._insert_table}")
                    value = cur.fetchone()[0]
                return int(value) if value is not None else None
            except (ValueError, TypeError, psycopg.Error):
                pass
        return None

    def _as_rows(self, values):
        columns = [item.name for item in (self.raw.description or ())]
        return [CompatRow(columns, row) for row in values]

    def execute(self, sql: str, params: Any = None):
        self._synthetic = None
        self._lastrowid = None
        self._insert_table = None
        pragma = _PRAGMA_TABLE_INFO.match(str(sql))
        if pragma:
            table = pragma.group(1)
            query = """
                SELECT ordinal_position - 1 AS cid, column_name AS name,
                       data_type AS type,
                       CASE WHEN is_nullable = 'NO' THEN 1 ELSE 0 END AS notnull,
                       column_default AS dflt_value, 0 AS pk
                  FROM information_schema.columns
                 WHERE table_schema = current_schema() AND table_name = %s
                 ORDER BY ordinal_position
            """
            with self.connection.raw.cursor() as cur:
                cur.execute(query, (table,))
                rows = [CompatRow(
                    ["cid", "name", "type", "notnull", "dflt_value", "pk"],
                    row,
                ) for row in cur.fetchall()]
            description = [
                ("cid", None, None, None, None, None, None),
                ("name", None, None, None, None, None, None),
                ("type", None, None, None, None, None, None),
                ("notnull", None, None, None, None, None, None),
                ("dflt_value", None, None, None, None, None, None),
                ("pk", None, None, None, None, None, None),
            ]
            self._synthetic = _SyntheticCursor(rows, description)
            return self
        if _PRAGMA.match(str(sql)):
            self._synthetic = _SyntheticCursor([], [])
            return self
        text = _translate_sql(sql)
        insert_match = _INSERT_TABLE.match(text)
        if insert_match:
            self._insert_table = insert_match.group(1)
            self.connection.raw.execute("SELECT set_config('trade_os.lastrowid', '', true)")
        # A SQLite caller may issue BEGIN after a SELECT has already opened a
        # psycopg transaction.  SQLite treats that as a harmless continuation;
        # preserve the active transaction instead of raising PostgreSQL's
        # "transaction already in progress" error.
        if re.match(r"^\s*BEGIN\b", text, re.IGNORECASE):
            if self.connection.in_transaction:
                return self
        try:
            self.raw.execute(text, params)
        except errors.UniqueViolation as exc:
            raise sqlite3.IntegrityError(str(exc)) from exc
        except errors.ForeignKeyViolation as exc:
            raise sqlite3.IntegrityError(str(exc)) from exc
        return self

    def executemany(self, sql: str, params_seq):
        self._synthetic = None
        self._lastrowid = None
        self._insert_table = None
        try:
            self.raw.executemany(_translate_sql(sql), params_seq)
        except errors.UniqueViolation as exc:
            raise sqlite3.IntegrityError(str(exc)) from exc
        except errors.ForeignKeyViolation as exc:
            raise sqlite3.IntegrityError(str(exc)) from exc
        return self

    def fetchone(self):
        if self._synthetic is not None:
            return self._synthetic.fetchone()
        row = self.raw.fetchone()
        return None if row is None else CompatRow([item.name for item in (self.raw.description or ())], row)

    def fetchall(self):
        if self._synthetic is not None:
            return self._synthetic.fetchall()
        return self._as_rows(self.raw.fetchall())

    def fetchmany(self, size=None):
        if self._synthetic is not None:
            return self._synthetic.fetchmany(size)
        return self._as_rows(self.raw.fetchmany(size))

    def __iter__(self):
        return iter(self.fetchall())

    def close(self):
        if self._synthetic is not None:
            self._synthetic.close()
        self.raw.close()


class CompatConnection:
    def __init__(self, raw):
        self.raw = raw
        self._row_factory = None

    @property
    def in_transaction(self):
        return self.raw.info.transaction_status != psycopg.pq.TransactionStatus.IDLE

    @property
    def row_factory(self):
        return self._row_factory

    @row_factory.setter
    def row_factory(self, value):
        # The application assigns sqlite3.Row.  CompatCursor supplies the
        # same mapping/index behavior and deliberately ignores that setting.
        self._row_factory = value

    def cursor(self, *args, **kwargs):
        return CompatCursor(self, self.raw.cursor(row_factory=tuple_row))

    def execute(self, sql: str, params: Any = None):
        cursor = self.cursor()
        cursor.execute(sql, params)
        return cursor

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                self.rollback()
            else:
                self.commit()
        finally:
            self.close()


def connect(user: str | None = None) -> CompatConnection:
    dsn = str(__import__("os").environ.get("TRADE_OS_DATABASE_URL") or "").strip()
    if not dsn:
        raise RuntimeError("TRADE_OS_DATABASE_URL is required for PostgreSQL mode")
    raw = psycopg.connect(dsn, row_factory=tuple_row)
    raw.execute("SELECT set_config('search_path', 'trade_os_compat,trosa,core,identity,audit,sela,public', false)")
    raw.execute("SELECT set_config('trade_os.user', %s, false)", (str(user or "hamid"),))
    raw.commit()
    return CompatConnection(raw)


def is_configured() -> bool:
    return bool(str(__import__("os").environ.get("TRADE_OS_DATABASE_URL") or "").strip())
