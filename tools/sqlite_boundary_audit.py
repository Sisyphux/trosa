"""Audit the PostgreSQL Customer boundary against the legacy SQLite facts.

The legacy per-user SQLite databases (``hamid.db``, ``amy.db``, ...) are the
historical source of truth: every ``follow_up_logs``, ``outreach_emails``,
``reminders``, and ``contacts`` row carries the customer it belongs to, and the
canonical PostgreSQL store was imported from them.  This tool re-derives the
expected customer attribution from SQLite and compares it with the current
PostgreSQL binding (``legacy_row_refs`` -> canonical row payload
``customer_id``) for the whole database, per user.

It repairs nothing.  It outputs two lists only:

* ``deterministic_fixes`` — the SQLite row answers the question exactly, so the
  PostgreSQL binding can be corrected with evidence (history rows are
  immutable at runtime, so a difference can only come from migration);
* ``manual_review`` — rows whose owner cannot be proven from SQLite (row never
  imported, binding missing, SQLite row without a customer, or the difference
  may be a legitimate runtime action such as an Inbox reassignment).

Rows created after the cutover (runtime writes) have no SQLite source and are
counted separately, never flagged.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SYSTEM_USERS = {'system', 'kelly', 'carl'}

# (sqlite table, legacy_row_refs.table_name, canonical table, payload column)
ROW_KINDS = (
    ('follow_up_logs', 'follow_up_logs', 'trosa.timeline_events', 'payload'),
    ('outreach_emails', 'outreach_emails', 'trosa.outreach_messages', 'legacy_payload'),
    ('reminders', 'reminders', 'trosa.tasks', 'legacy_payload'),
)


def _sqlite_rows(db_path: str, table: str) -> list[dict]:
    connection = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
        if not columns or 'customer_id' not in columns:
            return []
        rows = connection.execute(f'SELECT id, customer_id FROM {table}').fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _sqlite_customer_names(db_path: str) -> dict[int, str]:
    connection = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute('PRAGMA table_info(customers)')}
        if not columns:
            return {}
        rows = connection.execute('SELECT id, name, company FROM customers').fetchall()
        return {
            int(row['id']): str(row['company'] or row['name'] or f'#{row["id"]}')
            for row in rows
        }
    finally:
        connection.close()


def compare(sqlite_rows: list[dict], pg_rows: dict[int, dict]) -> dict[str, list]:
    """Classify one (user, table) against the legacy facts.

    ``sqlite_rows``: ``[{'id': int, 'customer_id': int|None}, ...]``
    ``pg_rows``: ``{legacy_id: {'bound': int|None, 'row_id': str}}``

    Buckets:
    ``ok`` — PostgreSQL binding equals the SQLite customer;
    ``fix`` — deterministic inconsistency, SQLite decides;
    ``manual_missing_ref`` — no PostgreSQL ref or no bound customer;
    ``manual_no_sqlite_customer`` — legacy row itself has no customer;
    ``pg_only`` — runtime rows with no SQLite counterpart.
    """
    buckets: dict[str, list] = {
        'ok': [], 'fix': [], 'manual_missing_ref': [],
        'manual_no_sqlite_customer': [], 'pg_only': [],
    }
    sqlite_ids: set[int] = set()
    for row in sqlite_rows:
        legacy_id = int(row['id'])
        sqlite_ids.add(legacy_id)
        pg = pg_rows.get(legacy_id)
        if pg is None or pg['bound'] is None:
            buckets['manual_missing_ref'].append(row)
        elif row['customer_id'] is None:
            buckets['manual_no_sqlite_customer'].append(row)
        elif int(pg['bound']) == int(row['customer_id']):
            buckets['ok'].append(row)
        else:
            buckets['fix'].append(row)
    for legacy_id in pg_rows:
        if legacy_id not in sqlite_ids:
            buckets['pg_only'].append({'id': legacy_id})
    return buckets


def _pg_state(connection, users: list[str]) -> dict:
    """Current PostgreSQL bindings keyed by (user, table, legacy_id)."""
    state: dict = {
        'rows': {},     # (user, table, legacy_id) -> {'bound': int|None, 'row_id': str}
        'contacts': {}, # (user, contact_id) -> {'customer': int, 'account_id': str}
        'inbox': {},    # (user, item_id) -> {'customer': int|None, 'account_id': str}
        'accounts': {}, # (user, customer_id) -> account_id
    }
    for _sqlite_table, ref_table, canonical, payload_column in ROW_KINDS:
        rows = connection.execute(
            f'''SELECT r.legacy_user_id, r.legacy_id,
                       trosa.compat_legacy_bigint(c.{payload_column}->>'customer_id') AS bound,
                       c.id AS row_id
                  FROM trosa.legacy_row_refs r
                  JOIN {canonical} c ON c.id=r.target_id
                 WHERE r.table_name=%s''',
            (ref_table,),
        ).fetchall()
        for row in rows:
            state['rows'][(row['legacy_user_id'], ref_table, int(row['legacy_id']))] = {
                'bound': int(row['bound']) if row['bound'] is not None else None,
                'row_id': str(row['row_id']),
            }
    for row in connection.execute(
        '''SELECT legacy_user_id, legacy_contact_id, legacy_customer_id, account_id
             FROM trosa.contact_legacy_refs'''
    ).fetchall():
        state['contacts'][(row['legacy_user_id'], int(row['legacy_contact_id']))] = {
            'customer': int(row['legacy_customer_id']),
            'account_id': str(row['account_id']),
        }
    for row in connection.execute(
        '''SELECT r.legacy_user_id, r.legacy_id, item.account_id,
                  (SELECT ar.legacy_customer_id
                     FROM trosa.account_legacy_refs ar
                    WHERE ar.account_id=item.account_id
                      AND ar.legacy_user_id=r.legacy_user_id
                      AND ar.organization_id=r.organization_id
                    LIMIT 1) AS legacy_customer
             FROM trosa.legacy_row_refs r
             JOIN trosa.inbox_items item ON item.id=r.target_id
            WHERE r.table_name='inbox_items' '''
    ).fetchall():
        state['inbox'][(row['legacy_user_id'], int(row['legacy_id']))] = {
            'customer': int(row['legacy_customer']) if row['legacy_customer'] is not None else None,
            'account_id': str(row['account_id']),
        }
    for user in users:
        for row in connection.execute(
            '''SELECT ar.legacy_customer_id, ar.account_id
                 FROM trosa.account_legacy_refs ar
                WHERE ar.legacy_user_id=%s''',
            (user,),
        ).fetchall():
            state['accounts'][(user, int(row['legacy_customer_id']))] = str(row['account_id'])
    return state


def audit(connection, sqlite_dir: Path, *, json_output: bool) -> int:
    users = [
        row['legacy_user_id'] for row in connection.execute(
            '''SELECT DISTINCT legacy_user_id FROM trosa.account_legacy_refs
                WHERE coalesce(legacy_user_id,'') <> '' ORDER BY 1'''
        ).fetchall()
        if row['legacy_user_id'] not in SYSTEM_USERS
    ]
    state = _pg_state(connection, users)

    report: dict = {
        'generated_at': _dt.datetime.now(_dt.timezone.utc).isoformat(),
        'sqlite_dir': str(sqlite_dir),
        'users': users,
        'ok_total': 0,
        'pg_only_runtime_rows': 0,
        'deterministic_fixes': [],
        'manual_review': [],
    }
    for user in users:
        db_path = sqlite_dir / f'{user}.db'
        if not db_path.is_file():
            continue
        names = _sqlite_customer_names(str(db_path))
        for sqlite_table, ref_table, _canonical, _payload in ROW_KINDS:
            sqlite_rows = _sqlite_rows(str(db_path), sqlite_table)
            pg_rows = {
                legacy_id: entry
                for (owner, table, legacy_id), entry in state['rows'].items()
                if owner == user and table == ref_table
            }
            buckets = compare(sqlite_rows, pg_rows)
            report['ok_total'] += len(buckets['ok'])
            report['pg_only_runtime_rows'] += len(buckets['pg_only'])
            for row in buckets['fix']:
                pg = pg_rows[int(row['id'])]
                report['deterministic_fixes'].append({
                    'user': user, 'table': ref_table, 'legacy_id': int(row['id']),
                    'sqlite_customer_id': int(row['customer_id']),
                    'sqlite_customer_name': names.get(int(row['customer_id']), ''),
                    'current_pg_customer_id': pg['bound'],
                    'row_id': pg['row_id'],
                })
            for row in buckets['manual_missing_ref']:
                report['manual_review'].append({
                    'user': user, 'table': ref_table, 'legacy_id': int(row['id']),
                    'reason': 'no_postgres_ref_or_binding',
                    'sqlite_customer_id': row.get('customer_id'),
                    'sqlite_customer_name': (
                        names.get(int(row['customer_id']), '')
                        if row.get('customer_id') is not None else ''
                    ),
                })
            for row in buckets['manual_no_sqlite_customer']:
                report['manual_review'].append({
                    'user': user, 'table': ref_table, 'legacy_id': int(row['id']),
                    'reason': 'sqlite_row_has_no_customer_id',
                    'sqlite_customer_id': None, 'sqlite_customer_name': '',
                })

        # Contacts: a legacy contact must keep its legacy customer and that
        # customer's account.
        for row in _sqlite_rows(str(db_path), 'contacts'):
            contact_id = int(row['id'])
            pg_contact = state['contacts'].get((user, contact_id))
            expected_customer = (
                int(row['customer_id']) if row['customer_id'] is not None else None
            )
            if pg_contact is None:
                report['manual_review'].append({
                    'user': user, 'table': 'contacts', 'legacy_id': contact_id,
                    'reason': 'contact_not_imported',
                    'sqlite_customer_id': expected_customer,
                    'sqlite_customer_name': (
                        names.get(expected_customer, '') if expected_customer else ''
                    ),
                })
            elif expected_customer is None:
                report['manual_review'].append({
                    'user': user, 'table': 'contacts', 'legacy_id': contact_id,
                    'reason': 'sqlite_contact_has_no_customer_id',
                    'sqlite_customer_id': None, 'sqlite_customer_name': '',
                })
            elif pg_contact['customer'] != expected_customer:
                report['deterministic_fixes'].append({
                    'user': user, 'table': 'contacts', 'legacy_id': contact_id,
                    'sqlite_customer_id': expected_customer,
                    'sqlite_customer_name': names.get(expected_customer, ''),
                    'current_pg_customer_id': pg_contact['customer'],
                })
            else:
                expected_account = state['accounts'].get((user, expected_customer))
                if expected_account and pg_contact['account_id'] != expected_account:
                    report['deterministic_fixes'].append({
                        'user': user, 'table': 'contacts', 'legacy_id': contact_id,
                        'sqlite_customer_id': expected_customer,
                        'sqlite_customer_name': names.get(expected_customer, ''),
                        'current_pg_customer_id': pg_contact['customer'],
                        'detail': 'contact_account_points_at_a_different_customer_account',
                    })

        # Inbox: runtime reassignments are legitimate, so any difference goes
        # to human review, never to an automatic fix.
        for row in _sqlite_rows(str(db_path), 'inbox_items'):
            item_id = int(row['id'])
            expected_customer = row.get('customer_id')
            if expected_customer is None:
                continue
            pg_inbox = state['inbox'].get((user, item_id))
            if pg_inbox is None:
                report['manual_review'].append({
                    'user': user, 'table': 'inbox_items', 'legacy_id': item_id,
                    'reason': 'inbox_item_not_imported',
                    'sqlite_customer_id': int(expected_customer),
                    'sqlite_customer_name': names.get(int(expected_customer), ''),
                })
            elif pg_inbox['customer'] is None or pg_inbox['customer'] != int(expected_customer):
                report['manual_review'].append({
                    'user': user, 'table': 'inbox_items', 'legacy_id': item_id,
                    'reason': 'inbox_customer_differs_from_sqlite',
                    'sqlite_customer_id': int(expected_customer),
                    'sqlite_customer_name': names.get(int(expected_customer), ''),
                    'current_pg_customer_id': pg_inbox['customer'],
                })

    report['deterministic_fix_count'] = len(report['deterministic_fixes'])
    report['manual_review_count'] = len(report['manual_review'])
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 1 if report['deterministic_fixes'] else 0


def _print_report(report: dict) -> None:
    print(f"SQLite 事实源: {report['sqlite_dir']}  用户: {', '.join(report['users'])}")
    print(f"完全一致的历史行: {report['ok_total']}")
    print(f"PostgreSQL 独有(切换后运行时新写入，无 SQLite 对照): {report['pg_only_runtime_rows']}")
    print(f"确定修复(以 SQLite 为准): {report['deterministic_fix_count']}")
    print(f"人工确认: {report['manual_review_count']}")
    if report['deterministic_fixes']:
        print('--- 确定修复 ---')
        for item in report['deterministic_fixes']:
            print(f"  [{item['user']}] {item['table']} legacy_id={item['legacy_id']} "
                  f"SQLite客户={item['sqlite_customer_id']}({item['sqlite_customer_name']}) "
                  f"当前PG客户={item['current_pg_customer_id']}"
                  + (f" [{item['detail']}]" if item.get('detail') else ''))
    if report['manual_review']:
        print('--- 人工确认 ---')
        for item in report['manual_review']:
            print(f"  [{item['user']}] {item['table']} legacy_id={item['legacy_id']} "
                  f"reason={item['reason']} sqlite_customer={item['sqlite_customer_id']} "
                  f"({item['sqlite_customer_name']})"
                  + (f" 当前PG客户={item['current_pg_customer_id']}"
                     if 'current_pg_customer_id' in item else ''))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url', default='')
    parser.add_argument('--sqlite-dir', default='/var/lib/trade-os',
                        help='历史 SQLite 数据库目录（默认 /var/lib/trade-os）')
    parser.add_argument('--json', action='store_true', help='输出完整 JSON 报告')
    args = parser.parse_args()

    from tools.customer_boundary_audit import _dsn, _connect

    connection = _connect(_dsn(args.database_url))
    try:
        return audit(connection, Path(args.sqlite_dir), json_output=args.json)
    finally:
        connection.close()


if __name__ == '__main__':
    raise SystemExit(main())
