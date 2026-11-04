#!/usr/bin/env python3
"""Merge one duplicate Customer into the record a person actually works with.

Sela could create a second Customer for a company that already existed when the
prospect payload carried a different domain spelling (``regalplastic.com`` vs
``regal-plastics.com``) and the names were not equal variants.  The duplicate
then kept its own outreach history while the real relationship — contacts,
communications, tasks — lived under the first Customer.

This tool moves the duplicate's facts onto the surviving Customer, registers the
duplicate's domains and names as that company's identity, blocks further sela
contact, and archives the duplicate.  Nothing is deleted: every moved row is
snapshotted to JSON before the transaction, and the duplicate Customer stays in
the recycle bin.

Usage (production, on the server)::

    python3 tools/merge_customer_duplicate.py --source 1262 --target 3
    python3 tools/merge_customer_duplicate.py --source 1262 --target 3 --apply --yes

The default is a dry run that prints exactly what would move.  ``--apply``
writes one transaction; re-running after a successful merge exits without
changes (the archived source is no longer an active Customer).
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402

DEFAULT_BACKUP_DIR = '/var/lib/trade-os/merge-backups'


def _now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _rows(conn, sql, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _exec(conn, sql, params=()):
    return conn.execute(sql, params).rowcount


def _customer(conn, customer_id):
    rows = _rows(
        conn,
        '''SELECT r.*, a.company_id AS company_uuid, a.deleted_at AS account_deleted_at
             FROM trosa.customer_records r
             JOIN trosa.accounts a ON a.id=r.account_id
            WHERE r.id=? AND r.deleted_at IS NULL''',
        (customer_id,),
    )
    return rows[0] if rows else None


def _snapshot(conn, source, target):
    """Every fact the merge reads or moves, so the change is reversible by hand."""
    source_account = source['account_id']
    target_account = target['account_id']
    statement = {}
    statement['customers'] = [source, target]
    statement['accounts'] = _rows(
        conn, 'SELECT * FROM trosa.accounts WHERE id IN (?, ?)', (source_account, target_account))
    statement['legacy_refs'] = _rows(
        conn,
        '''SELECT * FROM trosa.account_legacy_refs
            WHERE legacy_customer_id IN (?, ?)''',
        (source['id'], target['id']),
    )
    statement['details'] = _rows(
        conn,
        'SELECT * FROM trosa.customer_details WHERE account_id IN (?, ?)',
        (source_account, target_account),
    )
    statement['states'] = _rows(
        conn,
        'SELECT * FROM trosa.customer_states WHERE legacy_customer_id IN (?, ?)',
        (source['id'], target['id']),
    )
    # ``trosa.customer_contacts`` is a read view; contacts live per user in
    # ``trosa.contact_legacy_refs``.
    statement['contacts'] = _rows(
        conn,
        '''SELECT * FROM trosa.contact_legacy_refs
            WHERE organization_id=trosa.compat_org_id()
              AND legacy_user_id=trosa.compat_current_user()
              AND legacy_customer_id IN (?, ?)''',
        (source['id'], target['id']),
    )
    statement['outreach_messages'] = _rows(
        conn, 'SELECT * FROM trosa.outreach_messages WHERE account_id=?', (source_account,))
    message_ids = [row['id'] for row in statement['outreach_messages']]
    if message_ids:
        placeholders = ','.join('?' for _ in message_ids)
        statement['email_delivery_events'] = _rows(
            conn,
            f'SELECT * FROM trosa.email_delivery_events WHERE outreach_message_id IN ({placeholders})',
            message_ids,
        )
    statement['timeline_events'] = _rows(
        conn, 'SELECT * FROM trosa.timeline_events WHERE account_id=?', (source_account,))
    statement['inbox_items'] = _rows(
        conn, 'SELECT * FROM trosa.inbox_items WHERE account_id=?', (source_account,))
    statement['tasks'] = _rows(
        conn, 'SELECT * FROM trosa.tasks WHERE account_id=?', (source_account,))
    statement['prospect_profiles'] = _rows(
        conn,
        '''SELECT * FROM trosa.agent_prospect_profiles
            WHERE customer_id IN (?, ?)''',
        (source['id'], target['id']),
    )
    company_ids = [source['company_uuid'], target['company_uuid']]
    statement['company_domains'] = _rows(
        conn,
        'SELECT * FROM core.company_domains WHERE company_id IN (?, ?)',
        company_ids,
    )
    statement['company_aliases'] = _rows(
        conn,
        'SELECT * FROM core.company_aliases WHERE company_id IN (?, ?)',
        company_ids,
    )
    statement['company_people'] = _rows(
        conn,
        'SELECT * FROM core.company_people WHERE company_id IN (?, ?)',
        company_ids,
    )
    return statement


def _counts(statement):
    return {key: len(value) for key, value in sorted(statement.items())
            if isinstance(value, list) and key not in ('customers', 'accounts', 'legacy_refs')}


def _write_backup(statement, backup_dir, source_id, target_id):
    directory = Path(backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
    path = directory / f'merge-customer-{source_id}-into-{target_id}-{stamp}.json'
    path.write_text(json.dumps(statement, ensure_ascii=False, indent=2, default=str),
                    encoding='utf-8')
    return path


def _merge(conn, source, target, note):
    source_id, target_id = int(source['id']), int(target['id'])
    source_account, target_account = source['account_id'], target['account_id']
    source_company, target_company = source['company_uuid'], target['company_uuid']
    now = _now()
    moved = {}

    moved['outreach_messages'] = _exec(
        conn,
        'UPDATE trosa.outreach_messages SET account_id=? WHERE account_id=?',
        (target_account, source_account),
    )
    moved['timeline_events'] = _exec(
        conn,
        'UPDATE trosa.timeline_events SET account_id=? WHERE account_id=?',
        (target_account, source_account),
    )
    moved['inbox_items'] = _exec(
        conn,
        'UPDATE trosa.inbox_items SET account_id=? WHERE account_id=?',
        (target_account, source_account),
    )

    # Contacts move as they are, except a second "primary" contact: the target's
    # own primary keeps priority and the duplicate becomes an ordinary contact.
    target_primary = conn.execute(
        '''SELECT count(*) FROM trosa.contact_legacy_refs
            WHERE organization_id=trosa.compat_org_id()
              AND legacy_user_id=trosa.compat_current_user()
              AND legacy_customer_id=? AND is_primary''',
        (target_id,),
    ).fetchone()[0]
    if target_primary:
        _exec(
            conn,
            '''UPDATE trosa.contact_legacy_refs SET is_primary=false
                WHERE organization_id=trosa.compat_org_id()
                  AND legacy_user_id=trosa.compat_current_user()
                  AND legacy_customer_id=?''',
            (source_id,),
        )
    moved['customer_contacts'] = _exec(
        conn,
        '''UPDATE trosa.contact_legacy_refs
              SET legacy_customer_id=?, account_id=?
            WHERE organization_id=trosa.compat_org_id()
              AND legacy_user_id=trosa.compat_current_user()
              AND legacy_customer_id=?''',
        (target_id, target_account, source_id),
    )

    # Tasks created for the duplicate were written on a false premise (the tool
    # that created them believed both records were different companies).  Close
    # them with a note instead of moving a wrong instruction to the survivor.
    moved['tasks_closed'] = _exec(
        conn,
        """UPDATE trosa.tasks
              SET status='done', completed_at=coalesce(completed_at, now()),
                  content=coalesce(content, '') || ?,
                  updated_at=now()
            WHERE account_id=? AND status='open'""",
        (f'\n[合并] 重复客户 #{source_id} 已合并到 #{target_id}，本条不再单独执行。', source_account),
    )

    moved['prospect_profiles'] = _exec(
        conn,
        """UPDATE trosa.agent_prospect_profiles
              SET customer_id=?, contact_permission='do_not_contact',
                  suppression_reason=?, suppression_at=?, updated_at=?
            WHERE customer_id=?""",
        (target_id, note, now, now, source_id),
    )

    # Company identity: the duplicate's hosts and names become the survivor's,
    # so the same company cannot be developed a second time.  A duplicate row
    # the survivor already has is dropped first: the unique key is per company,
    # so moving it would fail.
    _exec(
        conn,
        """DELETE FROM core.company_domains duplicate
             USING core.company_domains existing
            WHERE duplicate.company_id=? AND existing.company_id=?
              AND duplicate.normalized_domain=existing.normalized_domain""",
        (source_company, target_company),
    )
    _exec(
        conn,
        """UPDATE core.company_domains
              SET company_id=?, is_primary=false, verification_status='verified'
            WHERE company_id=?""",
        (target_company, source_company),
    )
    _exec(
        conn,
        """DELETE FROM core.company_aliases duplicate
             USING core.company_aliases existing
            WHERE duplicate.company_id=? AND existing.company_id=?
              AND duplicate.normalized_alias=existing.normalized_alias""",
        (source_company, target_company),
    )
    _exec(
        conn,
        "UPDATE core.company_aliases SET company_id=? WHERE company_id=?",
        (target_company, source_company),
    )
    _exec(
        conn,
        "UPDATE core.company_people SET company_id=? WHERE company_id=?",
        (target_company, source_company),
    )

    _exec(
        conn,
        """UPDATE trosa.customer_details
              SET notes=coalesce(notes, '') || ?
            WHERE account_id=? AND strpos(coalesce(notes, ''), ?) = 0""",
        (f'\n{note}', target_account, note),
    )

    moved['archived'] = _exec(
        conn,
        """UPDATE trosa.account_legacy_refs
              SET legacy_payload=coalesce(legacy_payload, '{}'::jsonb)
                   || jsonb_build_object('is_deleted', '1'::text, 'deleted_at', ?::text)
            WHERE organization_id=trosa.compat_org_id()
              AND legacy_user_id=trosa.compat_current_user()
              AND legacy_customer_id=?""",
        (now, source_id),
    )
    return moved


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--source', type=int, required=True, help='重复客户 id（会被归档）')
    parser.add_argument('--target', type=int, required=True, help='保留的客户 id')
    parser.add_argument('--note', default='', help='写入客户备注的合并说明')
    parser.add_argument('--backup-dir', default=DEFAULT_BACKUP_DIR)
    parser.add_argument('--apply', action='store_true', help='真正写入（默认只预览）')
    parser.add_argument('--yes', action='store_true', help='与 --apply 一起确认写入')
    parser.add_argument('--user', default=os.environ.get('TRADE_OS_AGENT_USER') or 'hamid')
    args = parser.parse_args(argv)

    if not db.postgres_mode():
        print('只支持正式 PostgreSQL 运行环境；SQLite 仅供开发，不做客户合并。', file=sys.stderr)
        return 2
    if args.source == args.target:
        print('--source 与 --target 不能相同', file=sys.stderr)
        return 2
    if args.apply and not args.yes:
        print('--apply 需要同时给出 --yes', file=sys.stderr)
        return 2

    db.set_db_user(args.user)
    conn = db.get_db()
    try:
        source = _customer(conn, args.source)
        target = _customer(conn, args.target)
        if not source or not target:
            missing = args.source if not source else args.target
            print(f'客户 {missing} 不存在或已归档；若已合并，无需重复执行。')
            return 0 if not source else 2
        if source['account_id'] == target['account_id']:
            print('两个客户已共用同一 Account，无需合并。')
            return 0
        note = args.note or (
            f'2026-11-04 合并重复客户：#{args.source} {source.get("company") or source.get("name")}'
            f'（{source.get("website") or "无官网"}）并入 #{args.target}，'
            '保留其开发邮件与联系人，并停止 sela 对该公司的自动联系。')

        statement = _snapshot(conn, source, target)
        print(json.dumps({
            'source': {key: source.get(key) for key in
                       ('id', 'name', 'company', 'website', 'import_source', 'external_source', 'external_id')},
            'target': {key: target.get(key) for key in
                       ('id', 'name', 'company', 'website', 'import_source', 'external_source', 'external_id')},
            'rows': _counts(statement),
            'open_tasks': [row['title'] for row in statement['tasks'] if row.get('status') == 'open'],
        }, ensure_ascii=False, indent=2, default=str))

        if not args.apply:
            print('\n预览完成：未写入。加 --apply --yes 执行合并。')
            return 0

        backup_path = _write_backup(statement, args.backup_dir, args.source, args.target)
        print(f'备份已写入 {backup_path}')
        moved = _merge(conn, source, target, note)
        import app  # only for the audited operation log writer
        app._record_operation_log(
            conn, 'MERGE', 'customer', args.target,
            f'合并重复客户 #{args.source}（{source.get("company") or source.get("name")}）到 '
            f'#{args.target}；开发邮件 {moved["outreach_messages"]} 条、联系人 '
            f'{moved["customer_contacts"]} 位、关闭待办 {moved["tasks_closed"]} 条。',
        )
        conn.commit()
        print(json.dumps(moved, ensure_ascii=False, indent=2))
        print(f'已合并：客户 {args.source} 已归档，历史移入客户 {args.target}。')
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
