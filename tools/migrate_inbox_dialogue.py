#!/usr/bin/env python3
"""Migrate open legacy Sela Inbox requests into dialogue threads (contract §6.1).

Each open ``trosa.inbox_items`` row of type ``sela_agent_request`` becomes an
``inbox_threads`` row that reuses the legacy row's canonical UUID.  The legacy
integer id is recorded through ``trosa.legacy_row_refs`` so the old interfaces
keep resolving, and the old row/content is written verbatim as the first
``sela`` message.

The script is idempotent: a request that already has a thread is skipped, so it
can be re-run without duplicating anything.

Safety: this is a rehearsal/one-off tool.  It only runs with ``--apply`` and
``TRADE_OS_INBOX_DIALOGUE_MIGRATION=1`` set, and it must be pointed at an
isolated rehearsal database.  Never run it against production.
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db  # noqa: E402
import inbox_dialogue as dialogue  # noqa: E402

SELA_AGENT_REQUEST_TYPE = 'sela_agent_request'


def _legacy_scope(conn):
    where, params = dialogue._scope('ref')
    return where, params


def _legacy_rows(conn):
    """Return open legacy Sela requests as dicts (legacy_id + fields)."""
    where, params = _legacy_scope(conn)
    table = dialogue._t('inbox_items')
    ref_table = dialogue._t('legacy_row_refs')
    if dialogue.is_postgres():
        sql = (
            'SELECT item.id::text AS canonical_id, ref.legacy_id AS legacy_id, '
            'item.title, item.content, item.evidence, item.created_at::text AS created_at, '
            'item.legacy_payload '
            f'FROM {table} item '
            f'JOIN {ref_table} ref ON ref.target_id=item.id '
            f"WHERE item.item_type=? AND item.status='open' AND {where} "
            'ORDER BY ref.legacy_id'
        )
    else:
        sql = (
            'SELECT CAST(id AS TEXT) AS canonical_id, id AS legacy_id, title, content, '
            'evidence, created_at, NULL AS legacy_payload '
            f"FROM {table} WHERE item_type=? AND status='open' AND {where} ORDER BY id"
        )
    rows = conn.execute(sql, [SELA_AGENT_REQUEST_TYPE] + list(params)).fetchall()
    return [dict(row) for row in rows]


def _subject_for(row):
    payload = row.get('legacy_payload')
    if isinstance(payload, str) and payload.strip():
        try:
            payload = json.loads(payload)
        except Exception:
            payload = None
    if isinstance(payload, dict):
        source_id = str(payload.get('source_id') or '').strip()
        if source_id:
            return ('prospect:' + source_id)[:200]
    return None


def _refs_hints(row):
    evidence = row.get('evidence')
    if isinstance(evidence, str) and evidence.strip():
        try:
            evidence = json.loads(evidence)
        except Exception:
            evidence = None
    if not isinstance(evidence, dict):
        return [], None
    refs = []
    files = evidence.get('files') or evidence.get('attachments') or []
    if isinstance(files, list):
        for item in files[:50]:
            ref = (item.get('file_object_id') or item.get('id')
                   if isinstance(item, dict) else item)
            if ref:
                refs.append({'type': 'file', 'id': str(ref)[:200]})
    hints = evidence.get('hints') if isinstance(evidence.get('hints'), dict) else None
    return refs, hints


def run(apply=False, user=None, limit=None, expect=None):
    if not apply:
        return {'applied': False, 'reason': 'dry-run'}
    if os.environ.get('TRADE_OS_INBOX_DIALOGUE_MIGRATION') != '1':
        return {'applied': False, 'reason': 'TRADE_OS_INBOX_DIALOGUE_MIGRATION not set'}

    db.set_db_user(user or os.environ.get('TRADE_OS_INBOX_MIGRATION_USER') or 'hamid')
    conn = db.get_db()
    try:
        before = dialogue._counts(conn)
        legacy = _legacy_rows(conn)
        if limit:
            legacy = legacy[:limit]
        created, skipped, mapping = 0, 0, []
        conn.execute('BEGIN IMMEDIATE')
        for row in legacy:
            legacy_id = int(row['legacy_id'])
            known_thread = dialogue.resolve_legacy_thread_id(conn, legacy_id)
            already = bool(known_thread and dialogue._thread_row(conn, known_thread))
            refs, hints = _refs_hints(row)
            thread_id = dialogue.ensure_thread_for_legacy_item(
                conn, legacy_id=legacy_id,
                title=str(row.get('title') or ''),
                content=str(row.get('content') or ''),
                subject=_subject_for(row), refs=refs, hints=hints,
                created_at=row.get('created_at') or None,
            )
            if already:
                skipped += 1
            else:
                created += 1
            mapping.append({'legacy_id': legacy_id, 'thread_id': thread_id,
                            'created': not already})
        conn.commit()
        after = dialogue._counts(conn)
    finally:
        conn.close()

    result = {
        'applied': True,
        'user': db.get_current_user(),
        'legacy_open_requests': len(legacy),
        'threads_created': created,
        'threads_skipped': skipped,
        'before': before,
        'after': after,
        'mapping': mapping,
    }
    if expect is not None and len(legacy) != expect:
        result['expectation_failed'] = f'expected {expect} legacy requests, saw {len(legacy)}'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true',
                        help='actually write; without it this is a dry run')
    parser.add_argument('--user', default=None, help='scope user (default hamid)')
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--expect', type=int, default=None,
                        help='assert the number of legacy requests seen')
    args = parser.parse_args(argv)
    result = run(apply=args.apply, user=args.user, limit=args.limit,
                 expect=args.expect)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if result.get('reason') == 'TRADE_OS_INBOX_DIALOGUE_MIGRATION not set':
        print('Refusing to migrate: set TRADE_OS_INBOX_DIALOGUE_MIGRATION=1 on an '
              'isolated rehearsal database only.', file=sys.stderr)
        return 2
    if result.get('expectation_failed'):
        return 3
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
