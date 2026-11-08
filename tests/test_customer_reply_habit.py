"""Customer "一般什么时候回": a hand-written line, saved and read back as-is."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db
import postgres_schema_contract as contract
from tests.test_customer_timezone import load_app


class CustomerReplyHabitTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        self.original_demo = os.environ.get('CRM_SEED_DEMO_DATA')
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.init_all_dbs()
        self.module = load_app()
        self.client = self.module.app.test_client()
        with self.client.session_transaction() as session:
            session['user'] = 'hamid'

    def tearDown(self):
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    def make(self):
        response = self.client.post('/api/customers', json={'name': '习惯客户', 'company': '习惯客户'})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()['id']

    def test_empty_by_default_and_saved_as_written(self):
        cid = self.make()
        self.assertEqual(self.client.get(f'/api/customers/{cid}').get_json().get('reply_habit', ''), '')
        r = self.client.put(f'/api/customers/{cid}', json={'reply_habit': '  欧洲时间下午，\n周一最快 '})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.client.get(f'/api/customers/{cid}').get_json()['reply_habit'], '欧洲时间下午， 周一最快')
        summary = self.client.get(f'/api/customers/{cid}/summary').get_json()
        self.assertEqual(summary['reply_habit'], '欧洲时间下午， 周一最快')

    def test_other_edits_do_not_clear_it_and_blank_clears_it(self):
        cid = self.make()
        self.client.put(f'/api/customers/{cid}', json={'reply_habit': '晚上回'})
        self.client.put(f'/api/customers/{cid}', json={'field': '亚克力'})
        self.assertEqual(self.client.get(f'/api/customers/{cid}').get_json()['reply_habit'], '晚上回')
        self.client.put(f'/api/customers/{cid}', json={'reply_habit': ''})
        self.assertEqual(self.client.get(f'/api/customers/{cid}').get_json()['reply_habit'], '')

    def test_length_is_capped(self):
        cid = self.make()
        self.client.put(f'/api/customers/{cid}', json={'reply_habit': '好' * 500})
        self.assertEqual(len(self.client.get(f'/api/customers/{cid}').get_json()['reply_habit']), 200)

    def test_contract_and_migration_cover_the_column(self):
        text = (ROOT / 'migrations' / '0133_customer_reply_habit.sql').read_text()
        self.assertIn('ADD COLUMN IF NOT EXISTS reply_habit', text)
        self.assertIn('AS reply_habit', text)
        flat = repr(contract.__dict__)
        self.assertIn("trosa.customer_records', 'reply_habit'", flat.replace('"', "'"))
        self.assertIn("trosa.customer_details', 'reply_habit'", flat.replace('"', "'"))


if __name__ == '__main__':
    unittest.main()
