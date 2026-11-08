"""Follow-up suggestion storage (SQLite mirror).  Synthetic data only."""

import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import followup_store as store  # noqa: E402
from followup_candidates import Candidate  # noqa: E402


def cand(account='a1', rule='R1', event='e1', priority=1, reason='客户 3 天前来信'):
    return Candidate(account_id=account, rule=rule, certainty='certain',
                     evidence_event_id=event, evidence_event_ids=(event,),
                     facts={'days': 3}, reason=reason, owner='hamid', priority=priority,
                     dedupe_key=f'hamid:{account}:{rule}:{event}')


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = db.DB_DIR
        self.old_backend = os.environ.pop('TRADE_OS_DATA_BACKEND', None)
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.DB_DIR = self.tmp.name
        db.init_all_dbs()
        db.set_db_user('hamid')
        self.conn = db.get_db()
        self.today = date(2026, 11, 2)

    def tearDown(self):
        self.conn.close()
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.old_dir
        if self.old_backend is not None:
            os.environ['TRADE_OS_DATA_BACKEND'] = self.old_backend
        self.tmp.cleanup()

    def test_upsert_is_idempotent(self):
        first = store.upsert_candidates(self.conn, [cand(), cand('a2', 'R2', 'e2')], self.today)
        again = store.upsert_candidates(self.conn, [cand(), cand('a2', 'R2', 'e2')], self.today)
        self.assertEqual(first['inserted'], 2)
        self.assertEqual((again['inserted'], again['refreshed']), (0, 2))
        self.assertEqual(len(store.list_by_status(self.conn, 'new')), 2)

    def test_refresh_updates_reason_but_not_answered_rows(self):
        store.upsert_candidates(self.conn, [cand()], self.today)
        store.upsert_candidates(self.conn, [cand(reason='客户 4 天前来信')], self.today)
        row = store.list_by_status(self.conn, 'new')[0]
        self.assertEqual(row['reason'], '客户 4 天前来信')
        store.transition(self.conn, row['id'], 'dismissed', feedback_reason='已在别处处理')
        counts = store.upsert_candidates(self.conn, [cand(reason='x')], self.today)
        self.assertEqual(counts['kept'], 1)
        self.assertEqual(store.get(self.conn, row['id'])['status'], 'dismissed')
        self.assertEqual(store.muted_evidence(self.conn), {('a1', 'e1')})

    def test_snooze_hides_until_the_day(self):
        store.upsert_candidates(self.conn, [cand()], self.today)
        rid = store.list_by_status(self.conn, 'new')[0]['id']
        store.transition(self.conn, rid, 'snoozed', snooze_until='2026-11-05')
        self.assertEqual(store.list_visible(self.conn, '2026-11-04'), [])
        self.assertEqual([r['id'] for r in store.list_visible(self.conn, '2026-11-05')], [rid])

    def test_visible_order_follows_priority(self):
        store.upsert_candidates(self.conn, [cand('a1', priority=3), cand('a2', 'R2', 'e2', priority=1)], self.today)
        self.assertEqual([r['account_id'] for r in store.list_visible(self.conn, self.today)], ['a2', 'a1'])

    def test_void_and_expire(self):
        store.upsert_candidates(self.conn, [cand(), cand('a2', 'R2', 'e2')], date(2026, 10, 28))
        self.assertEqual(store.stale_new_count(self.conn, self.today), 2)
        self.assertEqual(store.void_unsupported(self.conn, {'hamid:a1:R1:e1'}, self.today), 1)
        self.assertEqual(store.expire_old(self.conn, self.today), 1)
        self.assertEqual(store.stale_new_count(self.conn, self.today), 0)
        self.assertEqual(len(store.list_by_status(self.conn, 'voided')), 1)
        self.assertEqual(len(store.list_by_status(self.conn, 'expired')), 1)

    def test_other_member_cannot_see_rows(self):
        store.upsert_candidates(self.conn, [cand()], self.today)
        self.conn.commit()
        rid = store.list_by_status(self.conn, 'new')[0]['id']
        db.set_db_user('amy')
        other = db.get_db()
        try:
            self.assertEqual(store.list_by_status(other, 'new'), [])
            self.assertIsNone(store.get(other, rid))
            store.transition(other, rid, 'handled')
        finally:
            other.close()
        db.set_db_user('hamid')
        self.assertEqual(store.get(self.conn, rid)['status'], 'new')


if __name__ == '__main__':
    unittest.main()
