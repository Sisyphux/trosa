"""Apply the deterministic Customer-boundary fixes derived from SQLite.

This is the execution counterpart of ``tools/sqlite_boundary_audit.py``.  It
re-derives the deterministic fix list from the legacy SQLite facts (never from
guessing), applies exactly those fixes, and follows the one deterministic
dependency: a timeline event that references a fixed task through
``related_task_id`` and shares the task's old binding follows the task to its
corrected customer.

Every change is journaled with the full before payload so it can be reviewed
and reverted.  Rows in the manual-review list are never touched here.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.sqlite_boundary_audit import ROW_KINDS, _sqlite_customer_names, collect_report

CANONICAL_TABLES = {
    ref_table: (canonical, payload_column)
    for _sqlite_table, ref_table, canonical, payload_column in ROW_KINDS
}


def plan_event_followups(event_rows: list[dict], fixed_tasks: dict) -> list[dict]:
    """Events that followed a now-corrected task must follow the correction.

    ``event_rows``: ``[{'owner', 'legacy_id', 'row_id', 'account_id',
    'related_task_id', 'bound', 'payload'}, ...]`` (timeline events with a
    ``related_task_id``).
    ``fixed_tasks``: ``{(user, 'reminders', legacy_id): {'old': int, 'new': int}}``.

    An event is updated only when its current binding equals the task's old
    binding — the precise inverse of the chain rule that bound it before.
    """
    updates: list[dict] = []
    for event in event_rows:
        task_key = (event['owner'], 'reminders', int(event['related_task_id']))
        fix = fixed_tasks.get(task_key)
        if fix is None:
            continue
        if event['bound'] is None or int(event['bound']) != int(fix['old']):
            continue
        updates.append({
            'table': 'follow_up_logs',
            'legacy_id': int(event['legacy_id']),
            'row_id': event['row_id'],
            'owner': event['owner'],
            'customer_id': int(fix['new']),
            'old_customer_id': int(event['bound']),
            'before_payload': event['payload'],
        })
    return updates


def apply_fixes(connection, fixes: list[dict], event_followups: list[dict],
                journal_path: Path) -> int:
    """Write the corrections and journal every before/after pair."""
    journal: list[dict] = []
    for fix in fixes:
        if fix['table'] == 'contacts':
            journal.append({**fix, 'kind': 'contact_binding', 'applied': False,
                            'note': 'contact repairs require explicit human sign-off'})
            continue
        canonical, payload_column = CANONICAL_TABLES[fix['table']]
        before = connection.execute(
            f'SELECT {payload_column} AS payload FROM {canonical} WHERE id=%s::uuid',
            (fix['row_id'],),
        ).fetchone()
        connection.execute(
            f'''UPDATE {canonical}
                   SET {payload_column}=coalesce({payload_column}, '{{}}'::jsonb)
                        || jsonb_build_object('customer_id', %s::bigint)
                 WHERE id=%s::uuid''',
            (int(fix['sqlite_customer_id']), fix['row_id']),
        )
        journal.append({
            'kind': 'binding_correction',
            'user': fix['user'], 'table': fix['table'],
            'legacy_id': fix['legacy_id'], 'row_id': fix['row_id'],
            'before_payload': before['payload'] if before else None,
            'sqlite_customer_id': int(fix['sqlite_customer_id']),
            'previous_customer_id': fix['current_pg_customer_id'],
        })
    for event in event_followups:
        before = connection.execute(
            '''SELECT payload AS payload FROM trosa.timeline_events WHERE id=%s::uuid''',
            (event['row_id'],),
        ).fetchone()
        connection.execute(
            '''UPDATE trosa.timeline_events
                   SET payload=coalesce(payload, '{}'::jsonb)
                        || jsonb_build_object('customer_id', %s::bigint)
                 WHERE id=%s::uuid''',
            (int(event['customer_id']), event['row_id']),
        )
        journal.append({
            'kind': 'event_follows_corrected_task',
            'table': 'follow_up_logs',
            'legacy_id': event['legacy_id'], 'row_id': event['row_id'],
            'before_payload': before['payload'] if before else None,
            'customer_id': int(event['customer_id']),
            'previous_customer_id': event['old_customer_id'],
        })
    if journal:
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_text(
            json.dumps(journal, ensure_ascii=False, indent=2), encoding='utf-8'
        )
    return len(journal)


def audit(connection, sqlite_dir: Path, *, apply: bool, journal_path: Path,
          json_output: bool) -> int:
    report = collect_report(connection, sqlite_dir)
    fixes = report['deterministic_fixes']

    fixed_tasks = {
        (fix['user'], 'reminders', fix['legacy_id']):
            {'old': int(fix['current_pg_customer_id']),
             'new': int(fix['sqlite_customer_id'])}
        for fix in fixes if fix['table'] == 'reminders' and 'row_id' in fix
    }
    event_rows = connection.execute(
        '''SELECT r.legacy_user_id AS owner, r.legacy_id, e.id AS row_id,
                  e.account_id,
                  trosa.compat_legacy_bigint(e.payload->>'related_task_id') AS related_task_id,
                  trosa.compat_legacy_bigint(e.payload->>'customer_id') AS bound,
                  e.payload
             FROM trosa.timeline_events e
             JOIN trosa.legacy_row_refs r ON r.target_id=e.id
              AND r.table_name='follow_up_logs'
            WHERE trosa.compat_legacy_bigint(e.payload->>'related_task_id') IS NOT NULL'''
    ).fetchall()
    event_rows = [
        {**row, 'payload': row['payload']} for row in event_rows
        if row['owner'] in {key[0] for key in fixed_tasks}
    ]
    followups = plan_event_followups(
        [dict(row) for row in event_rows], fixed_tasks,
    ) if fixed_tasks else []

    applied = 0
    if apply:
        connection.execute('BEGIN')
        try:
            applied = apply_fixes(connection, fixes, followups, journal_path)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    summary = {
        'generated_at': _dt.datetime.now(_dt.timezone.utc).isoformat(),
        'mode': 'repair' if apply else 'plan',
        'deterministic_fixes': fixes,
        'event_followups': followups,
        'applied': applied if apply else 0,
        'planned': len(fixes) + len(followups),
        'untouched_manual_review_count': report['manual_review_count'],
    }
    if json_output:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"确定修复: {len(fixes)} 条；连带事件: {len(followups)} 条"
              + (f"；已应用: {applied} 条，日志 {journal_path}" if apply else "（计划模式，未修改）"))
        for fix in fixes:
            print(f"  [{fix['user']}] {fix['table']} legacy_id={fix['legacy_id']} "
                  f"{fix['current_pg_customer_id']} -> {fix['sqlite_customer_id']} "
                  f"({fix['sqlite_customer_name']})")
        for event in followups:
            print(f"  [连带] [{event['owner']}] follow_up_logs legacy_id={event['legacy_id']} "
                  f"{event['old_customer_id']} -> {event['customer_id']}")
    return 0 if apply else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url', default='')
    parser.add_argument('--sqlite-dir', default='/var/lib/trade-os',
                        help='历史 SQLite 数据库目录（默认 /var/lib/trade-os）')
    parser.add_argument('--apply', action='store_true',
                        help='应用确定性修复（默认只打印计划）')
    parser.add_argument('--journal-path', default='',
                        help='变更日志路径（默认 trosa_sqlite_repair_<时间>.json）')
    parser.add_argument('--json', action='store_true', help='JSON 输出')
    args = parser.parse_args()

    from tools.customer_boundary_audit import _dsn, _connect

    connection = _connect(_dsn(args.database_url))
    try:
        journal_name = (
            args.journal_path
            or f"trosa_sqlite_repair_{_dt.datetime.now().strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        return audit(connection, Path(args.sqlite_dir), apply=args.apply,
                     journal_path=Path(journal_name), json_output=args.json)
    finally:
        connection.close()


if __name__ == '__main__':
    raise SystemExit(main())
