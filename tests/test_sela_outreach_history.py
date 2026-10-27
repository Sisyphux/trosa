"""Batched outreach history for Sela: one request instead of one ``/api/customers/<id>`` per customer."""
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

TOKEN = 'test-sela-outreach-history-token'


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_sela_outreach_history_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


class OutreachHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        self.original_demo = os.environ.get('CRM_SEED_DEMO_DATA')
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.init_all_dbs()
        conn = db.get_system_db()
        conn.execute(
            '''INSERT INTO app_settings (key, value, updated_at)
               VALUES (?, ?, datetime('now', 'localtime'))''',
            ('integration_token:sela:hamid', json.dumps({
                'token_sha256': hashlib.sha256(TOKEN.encode('utf-8')).hexdigest(),
                'enabled': True, 'user': 'hamid'})),
        )
        conn.commit()
        conn.close()
        self.module = load_app()
        self.client = self.module.app.test_client()
        self.headers = {'Authorization': f'Bearer {TOKEN}'}

    def tearDown(self):
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    def seed(self):
        db.set_db_user('hamid')
        conn = db.get_db()
        ids = []
        for name, country in (('Alpha Plastics', 'Brazil'), ('Beta Signs', 'France')):
            cursor = conn.execute(
                "INSERT INTO customers (name, company, country, industry) VALUES (?, ?, ?, 'PMMA')",
                (name, name, country))
            ids.append(int(cursor.lastrowid))
        conn.execute(
            "INSERT INTO contacts (customer_id, name, email, is_primary, contact_type) VALUES (?, 'A', 'a@alpha.test', 1, 'person')",
            (ids[0],))
        conn.execute(
            "INSERT INTO outreach_emails (customer_id, subject, sent_date, reply_status, reply_date) "
            "VALUES (?, 'Hello Alpha', '2026-10-20', 'replied', '2026-10-21')", (ids[0],))
        conn.execute(
            "INSERT INTO follow_up_logs (customer_id, follow_date, direction, activity_type, content) "
            "VALUES (?, '2026-10-21', 'inbound', 'customer_reply', 'send prices')", (ids[0],))
        conn.execute(
            "INSERT INTO follow_up_logs (customer_id, follow_date, direction, activity_type, content) "
            "VALUES (?, '2026-10-22', 'outbound', 'follow_up', 'we answered')", (ids[0],))
        conn.commit()
        conn.close()
        return ids

    def get(self, query):
        return self.client.get('/api/integrations/sela/outreach-history' + query, headers=self.headers)

    def test_returns_letters_and_inbound_replies_for_many_customers(self):
        alpha, beta = self.seed()
        response = self.get(f'?ids={alpha},{beta},999999')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        self.assertEqual(body['missing'], [999999])
        by_id = {row['id']: row for row in body['customers']}
        first = by_id[alpha]
        self.assertEqual((first['company'], first['country']), ('Alpha Plastics', 'Brazil'))
        self.assertEqual(first['contacts'], [{'is_primary': True, 'contact_type': 'person'}])
        self.assertEqual([m['content'] for m in first['outreach_emails']], ['Hello Alpha'])
        self.assertEqual(first['outreach_emails'][0]['delivery_status'], 'replied')
        self.assertEqual([m['content'] for m in first['follow_history']], ['send prices'])
        self.assertEqual(by_id[beta]['outreach_emails'], [])

    def test_matches_the_single_customer_detail(self):
        alpha, _ = self.seed()
        batch = self.get(f'?ids={alpha}').get_json()['customers'][0]
        db.set_db_user('hamid')
        self.assertEqual(self.client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)
        detail = self.client.get(f'/api/customers/{alpha}').get_json()
        self.assertEqual(batch['outreach_emails'], detail['outreach_emails'])
        self.assertEqual(batch['follow_history'],
                         [item for item in detail['follow_history'] if item['direction'] == 'inbound'])

    def test_bad_parameters_are_rejected(self):
        for query in ('', '?ids=', '?ids=abc', '?ids=0', '?ids=' + ','.join(str(i) for i in range(1, 202)),
                      '?ids=1&limit=5'):
            self.assertEqual(self.get(query).status_code, 400, query)

    def test_requires_authentication(self):
        self.assertEqual(self.client.get('/api/integrations/sela/outreach-history?ids=1').status_code, 401)


if __name__ == '__main__':
    unittest.main()
