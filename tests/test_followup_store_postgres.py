"""PostgreSQL rehearsal for follow-up suggestions (run via tools/postgres_rehearsal.py test).

Proves the migration created the table and that one member cannot read or
change another member's suggestions.  Skipped outside the loopback rehearsal DB.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_inbox_dialogue_postgres import _dsn, _loopback  # noqa: E402


class FollowupStorePostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = _dsn()
        if not dsn:
            raise unittest.SkipTest('run tools/postgres_rehearsal.py test')
        if not _loopback(dsn):
            raise unittest.SkipTest('rehearsal tests only accept a loopback DSN')
        os.environ['TRADE_OS_DATA_BACKEND'] = 'postgres'
        os.environ['TRADE_OS_DATABASE_URL'] = dsn
        import db
        db.init_postgres_store()

    @classmethod
    def tearDownClass(cls):
        for name in ('TRADE_OS_DATA_BACKEND', 'TRADE_OS_DATABASE_URL'):
            os.environ.pop(name, None)

    def tearDown(self):
        import db
        db.set_db_user(None)

    def test_members_only_see_their_own_rows(self):
        import db
        import followup_store as store
        from followup_candidates import Candidate

        db.set_db_user('hamid')
        conn = db.get_db()
        try:
            account = str(conn.execute('SELECT id FROM trosa.accounts LIMIT 1').fetchone()[0])
            conn.execute('DELETE FROM trosa.followup_suggestions')
            key = 'hamid:%s:R1:rehearsal' % account
            cand = Candidate(account_id=account, rule='R1', certainty='certain',
                             evidence_event_id=None, evidence_event_ids=(), facts={'days': 3},
                             reason='客户 3 天前来信', owner='hamid', priority=1, dedupe_key=key)
            counts = store.upsert_candidates(conn, [cand], date(2026, 11, 2))
            again = store.upsert_candidates(conn, [cand], date(2026, 11, 2))
            conn.commit()
            self.assertEqual((counts['inserted'], again['inserted']), (1, 0))
            row = store.list_by_status(conn, 'new')[0]
            self.assertEqual(row['facts'], {'days': 3})
            self.assertEqual(row['account_id'], account)
        finally:
            conn.close()

        conn = db.get_db()
        try:
            event = conn.execute('SELECT id, account_id FROM trosa.timeline_events LIMIT 1').fetchone()
            if event:
                event_id, event_account = str(event[0]), str(event[1])
                conn.execute('UPDATE trosa.followup_suggestions SET account_id=?, evidence_event_id=? WHERE id=?',
                             (event_account, event_id, row['id']))
                described = store.describe(conn, [store.get(conn, row['id'])])
                self.assertLessEqual(len(described), 1)
                for item in described:
                    self.assertTrue(item['customer_name'])
                    self.assertIsInstance(item['customer_id'], int)
            conn.rollback()
        finally:
            conn.close()

        db.set_db_user('amy')
        other = db.get_db()
        try:
            self.assertEqual(store.list_by_status(other, 'new'), [])
            self.assertIsNone(store.get(other, row['id']))
            store.transition(other, row['id'], 'dismissed')
            other.commit()
        finally:
            other.close()

        db.set_db_user('hamid')
        conn = db.get_db()
        try:
            self.assertEqual(store.get(conn, row['id'])['status'], 'new')
            store.transition(conn, row['id'], 'snoozed', snooze_until='2026-11-05')
            self.assertEqual(store.list_visible(conn, '2026-11-04'), [])
            self.assertEqual(len(store.list_visible(conn, '2026-11-05')), 1)
            conn.execute('DELETE FROM trosa.followup_suggestions')
            conn.commit()
        finally:
            conn.close()

    def test_daily_job_runs_on_the_real_schema_and_is_idempotent(self):
        """The real PostgreSQL loader (not a stub) against the rehearsal fixture."""
        import db
        import followup_job
        import followup_store as store

        today = date.today()
        db.set_db_user('hamid')
        conn = db.get_db()
        try:
            conn.execute('DELETE FROM trosa.followup_suggestions')
            account = conn.execute(
                'SELECT r.account_id FROM trosa.customer_records r WHERE r.deleted_at IS NULL '
                'AND NOT EXISTS (SELECT 1 FROM trosa.timeline_events e WHERE e.account_id=r.account_id) '
                'AND NOT EXISTS (SELECT 1 FROM trosa.tasks t WHERE t.account_id=r.account_id AND t.status=\'open\') '
                'LIMIT 1').fetchone()
            self.assertIsNotNone(account, 'the fixture needs a customer with no records')
            conn.execute(
                'INSERT INTO trosa.timeline_events (id, account_id, event_type, direction, content, occurred_at) '
                "VALUES (gen_random_uuid(), ?, '客户回复', 'inbound', "
                "'Please send the updated price list for the sample order.', now() - interval '5 days')",
                (str(account[0]),))
            first = followup_job.run_for_member(conn, 'hamid', today)
            second = followup_job.run_for_member(conn, 'hamid', today)
            rows = len(store.list_by_status(conn, 'new'))
            conn.rollback()
        finally:
            conn.close()
        print('followup daily job rehearsal:', first, second, 'rows', rows)
        self.assertTrue(first['ok'] and second['ok'])
        self.assertEqual(first['inserted'], 1)
        self.assertEqual(second['inserted'], 0)
        self.assertEqual(rows, 1)


if __name__ == '__main__':
    unittest.main()
