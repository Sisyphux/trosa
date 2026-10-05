"""PostgreSQL rehearsal for the Inbox dialogue backend.

Skipped unless explicitly pointed at the isolated loopback rehearsal database
(``tools/postgres_rehearsal.py test``).  Covers the §6.1 migration rehearsal on
constructed legacy requests and the §4.4 concurrent confirmation arbitration.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _dsn() -> str:
    if os.environ.get('TROSA_REHEARSAL') != '1':
        return ''
    return os.environ.get('TROSA_REHEARSAL_DATABASE_URL', '').strip()


def _loopback(dsn: str) -> bool:
    parsed = urlparse(dsn)
    expected_port = int(os.environ.get('TROSA_REHEARSAL_PORT', '55432'))
    expected_database = os.environ.get('TROSA_REHEARSAL_DB', 'trosa_rehearsal')
    return (
        parsed.scheme in {'postgres', 'postgresql'}
        and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
        and (parsed.port or 5432) == expected_port
        and parsed.path.lstrip('/') == expected_database
    )


class InboxDialoguePostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = _dsn()
        if not dsn:
            raise unittest.SkipTest('run tools/postgres_rehearsal.py test')
        if not _loopback(dsn):
            raise unittest.SkipTest('rehearsal tests only accept a loopback DSN')
        try:
            import psycopg
            with psycopg.connect(dsn) as connection:
                connection.execute('SELECT 1')
        except Exception as exc:  # pragma: no cover - local service dependent
            raise unittest.SkipTest(f'local PostgreSQL is unavailable: {exc}')
        os.environ['TRADE_OS_DATA_BACKEND'] = 'postgres'
        os.environ['TRADE_OS_DATABASE_URL'] = dsn
        os.environ['TRADE_OS_INBOX_DIALOGUE_MIGRATION'] = '1'
        import db
        db.init_postgres_store()
        cls.dsn = dsn

    @classmethod
    def tearDownClass(cls):
        """Do not leak PostgreSQL mode into the rest of the suite."""
        for name in ('TRADE_OS_DATA_BACKEND', 'TRADE_OS_DATABASE_URL',
                     'TRADE_OS_INBOX_DIALOGUE_MIGRATION'):
            os.environ.pop(name, None)

    def setUp(self):
        import db
        db.set_db_user('hamid')

    def tearDown(self):
        import db
        db.set_db_user(None)

    def test_migration_rehearsal_is_repeatable(self):
        import db
        import trosa_domain
        from tools import migrate_inbox_dialogue as migration

        conn = db.get_db()
        legacy_ids = []
        try:
            for index in range(11):
                legacy_ids.append(trosa_domain.create_inbox_item(
                    conn, item_type='sela_agent_request',
                    title=f'历史请求 {index}',
                    content=f'第 {index} 条历史内容',
                    dedupe_key=f'sela:rehearsal:{index}',
                    status='open',
                    created_at=f'2026-09-{10 + index:02d} 09:00:00',
                ))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(len(set(legacy_ids)), 11)

        seeded = set(legacy_ids)
        first = migration.run(apply=True, user='hamid')
        self.assertTrue(first['applied'])
        self.assertGreaterEqual(first['legacy_open_requests'], 11)
        self.assertEqual(
            first['threads_created'] + first['threads_skipped'],
            first['legacy_open_requests'])
        created_ids = {m['legacy_id'] for m in first['mapping'] if m['created']}
        self.assertTrue(seeded.issubset(created_ids))
        self.assertEqual(
            first['before']['open'],
            first['after']['open'] - first['threads_created'])

        second = migration.run(apply=True, user='hamid')
        self.assertEqual(second['threads_created'], 0)
        self.assertEqual(second['threads_skipped'], second['legacy_open_requests'])
        self.assertEqual(second['after']['open'], first['after']['open'])
        self.assertEqual({m['legacy_id'] for m in second['mapping']},
                         {m['legacy_id'] for m in first['mapping']})

        # Each seeded thread reused the legacy canonical uuid and kept the content.
        conn = db.get_db()
        try:
            for entry in first['mapping']:
                if entry['legacy_id'] not in seeded:
                    continue
                row = conn.execute(
                    "SELECT content FROM trosa.inbox_items WHERE id=%s::uuid",
                    (entry['thread_id'],),
                ).fetchone()
                self.assertIsNotNone(row)
                thread = conn.execute(
                    'SELECT t.id, m.text FROM trosa.inbox_threads t '
                    'JOIN trosa.inbox_messages m ON m.thread_id=t.id AND m.seq=1 '
                    'WHERE t.id=%s::uuid',
                    (entry['thread_id'],),
                ).fetchone()
                self.assertEqual(str(thread[1]), str(row[0]))
        finally:
            conn.close()

    def test_concurrent_confirmation_consumption(self):
        import db
        import inbox_dialogue as dialogue

        conn = db.get_db()
        try:
            conn.execute('BEGIN IMMEDIATE')
            created = dialogue.create_thread(
                conn, payload={'title': '并发确认', 'text': '请确认合并身份',
                               'subject': 'prospect:concurrent'})
            thread_id = created['thread']['id']
            proposal = dialogue.append_message(
                conn, thread_id=thread_id, payload={'text': '提议合并身份'})
            proposal_id = proposal['thread']['messages'][-1]['id']
            confirmation = dialogue.reply_human(
                conn, thread_id=thread_id,
                payload={'text': '确认', 'seen_revision': proposal['thread']['revision']})
            confirmation_id = confirmation['message']['id']
            conn.execute(
                'CREATE TABLE IF NOT EXISTS trosa.dialogue_test_markers '
                '(id serial PRIMARY KEY, payload text)')
            conn.execute('DELETE FROM trosa.dialogue_test_markers')
            conn.commit()
        finally:
            conn.close()

        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def _run(key):
            thread_conn = db.get_db()
            try:
                thread_conn.execute('BEGIN IMMEDIATE')

                def handler(inner_conn, arguments, now):
                    inner_conn.execute(
                        'INSERT INTO trosa.dialogue_test_markers (payload) VALUES (%s)',
                        (key,))
                    return {'key': key}

                barrier.wait(timeout=10)
                try:
                    dialogue.irreversible_action(
                        thread_conn, thread_id=thread_id, action='merge_identity',
                        arguments={'op': 'merge'}, proposal_message_id=proposal_id,
                        confirmed_by_message_id=confirmation_id, handler=handler,
                        idempotency_key=key)
                    thread_conn.commit()
                    result = 'ok'
                except dialogue.DialogueError as error:
                    thread_conn.rollback()
                    result = error.code
                except Exception as error:  # pragma: no cover - diagnostic
                    thread_conn.rollback()
                    result = f'error:{error}'
            finally:
                thread_conn.close()
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=_run, args=(f'concurrent-{uuid.uuid4().hex}',))
                   for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(sorted(outcomes), ['confirmation_required', 'ok'])
        conn = db.get_db()
        try:
            markers = conn.execute(
                'SELECT count(*) FROM trosa.dialogue_test_markers').fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(int(markers), 1, 'the losing transaction must roll back')


if __name__ == '__main__':
    unittest.main()
