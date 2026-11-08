"""Follow-up suggestion API (SQLite development boundary).  Synthetic data only."""

import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import followup_store as store  # noqa: E402
from followup_candidates import Candidate  # noqa: E402


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_followup_api_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_a, **_k: None
    return module


class FollowupApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = db.DB_DIR
        self.old_demo = os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.DB_DIR = self.tmp.name
        db.init_all_dbs()
        self.module = load_app()
        self.today = self.module._calendar_today()
        db.set_db_user('hamid')
        conn = db.get_db()
        conn.execute("INSERT INTO customers (name, company) VALUES ('测试客户甲', '甲公司')")
        cid = conn.execute('SELECT id FROM customers').fetchone()[0]
        conn.execute("INSERT INTO follow_up_logs (customer_id, content, follow_date, direction) "
                     "VALUES (?, '客户问了报价,说下周回复', '2026-10-20', 'inbound')", (cid,))
        self.event = conn.execute('SELECT id FROM follow_up_logs').fetchone()[0]
        self.cid = cid
        conn.commit()
        conn.close()
        self.suggestion = self._add('R1', self.event)
        self.client = self.module.app.test_client()
        with self.client.session_transaction() as s:
            s['user'] = 'hamid'

    def tearDown(self):
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.old_dir
        if self.old_demo is not None:
            os.environ['CRM_SEED_DEMO_DATA'] = self.old_demo
        self.tmp.cleanup()

    def _add(self, rule, event):
        db.set_db_user('hamid')
        conn = db.get_db()
        cand = Candidate(account_id=str(self.cid), rule=rule, certainty='certain',
                         evidence_event_id=str(event), evidence_event_ids=(str(event),),
                         facts={}, reason='客户 10 月 20 日来信,还没有回复', owner='hamid',
                         priority=1, dedupe_key=f'hamid:{self.cid}:{rule}:{event}')
        store.upsert_candidates(conn, [cand], self.today)
        conn.commit()
        sid = store.list_by_status(conn, 'new')[0]['id']
        conn.close()
        return sid

    def hint(self):
        response = self.client.get(f'/api/customers/{self.cid}/summary')
        self.assertEqual(response.status_code, 200)
        return response.get_json()['follow_hint']

    def test_customer_summary_carries_the_one_line(self):
        hint = self.hint()
        self.assertIn('还没有回复', hint['reason'])
        self.assertEqual(hint['date'], self.today.isoformat())

    def test_a_newer_record_clears_it_without_a_click(self):
        self.assertIsNotNone(self.hint())
        conn = db.get_db()
        conn.execute("INSERT INTO follow_up_logs (customer_id, content, follow_date, direction) "
                     "VALUES (?, '已回复客户', '2026-10-29', 'outbound')", (self.cid,))
        conn.commit()
        conn.close()
        self.assertIsNone(self.hint())

    def test_a_deleted_newer_record_does_not_clear_it(self):
        conn = db.get_db()
        conn.execute("INSERT INTO follow_up_logs (customer_id, content, follow_date, direction, is_deleted) "
                     "VALUES (?, '写错了', '2026-10-29', 'outbound', 1)", (self.cid,))
        conn.commit()
        conn.close()
        self.assertIsNotNone(self.hint())

    def test_answered_or_expired_suggestions_show_nothing(self):
        conn = db.get_db()
        store.transition(conn, self.suggestion, 'expired')
        conn.commit()
        conn.close()
        self.assertIsNone(self.hint())

    def test_no_button_routes_remain(self):
        self.assertEqual(self.client.get('/api/followups/today').status_code, 404)
        for path in ('handled', 'dismiss', 'snooze'):
            self.assertIn(self.client.post(f'/api/followups/{self.suggestion}/{path}').status_code, (404, 405))

    def test_other_member_does_not_see_it(self):
        other = self.module.app.test_client()
        with other.session_transaction() as s:
            s['user'] = 'amy'
        self.assertEqual(other.get(f'/api/customers/{self.cid}/summary').status_code, 404)

    def test_reading_creates_no_task(self):
        before = db.get_db().execute('SELECT count(*) FROM reminders').fetchone()[0]
        self.hint()
        after = db.get_db().execute('SELECT count(*) FROM reminders').fetchone()[0]
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
