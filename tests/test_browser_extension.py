import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import db

ROOT = Path(__file__).resolve().parents[1]


class BrowserExtensionApiTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        db.DB_DIR = self.tempdir.name
        db.init_all_dbs()
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        conn.execute("INSERT INTO customers (name, company, website) VALUES ('Buyer', 'Gold Coast Plastics', '')")
        customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute("INSERT INTO contacts (customer_id, name, email, phone, whatsapp) VALUES (?, 'Mina', 'BUYER@EXAMPLE.COM', '', '+86 138 0000 0000')", (customer_id,))
        conn.commit()
        conn.close()
        spec = importlib.util.spec_from_file_location('crm_extension_api_test', ROOT / 'app.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.client = self.module.app.test_client()
        self.customer_id = customer_id
        self.assertEqual(self.client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)

    def tearDown(self):
        db.DB_DIR = self.original_db_dir
        self.tempdir.cleanup()

    def test_match_save_and_idempotent_duplicate(self):
        match = self.client.post('/api/extension/match', json={'email': ' buyer@example.com '})
        self.assertEqual(match.status_code, 200)
        self.assertEqual(match.get_json()['match_state'], 'unique')
        message = {'message_id': 'm-1', 'time': '2026-08-09 09:00', 'direction': 'inbound', 'text': 'Please confirm MOQ.', 'fingerprint': 'fixture-m-1'}
        payload = {
            'customer_id': self.customer_id, 'contact_id': match.get_json()['contacts'][0]['id'],
            'channel': 'netease', 'source_url': 'https://mail.163.com/thread/1',
            'conversation_identity': 'MOQ', 'messages': [message], 'content': message['text'],
            'result': '客户询问 MOQ', 'warnings': ['fixture warning'], 'adapter_version': 'netease-test',
        }
        saved = self.client.post('/api/extension/communications', json=payload)
        self.assertEqual(saved.status_code, 200, saved.get_json())
        duplicate = self.client.post('/api/extension/communications', json=payload)
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.get_json()['duplicate'])
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM follow_up_logs').fetchone()[0], 1)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM communication_source_items').fetchone()[0], 1)
        self.assertEqual(json.loads(conn.execute('SELECT raw_payload FROM communication_sources').fetchone()[0])[0]['text'], message['text'])
        conn.close()

    def test_domain_is_a_confirmable_candidate_not_an_auto_write(self):
        result = self.client.post('/api/extension/match', json={'email': 'sales@goldcoastplastics.com.au'})
        self.assertEqual(result.status_code, 200)
        payload = result.get_json()
        self.assertEqual(payload['match_state'], 'domain_candidate')
        self.assertEqual(payload['domain'], 'goldcoastplastics.com.au')
        self.assertEqual(payload['domain_candidates'][0]['customer']['company'], 'Gold Coast Plastics')

    def test_conflicting_exact_contact_and_company_domain_requires_confirmation(self):
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        conn.execute("INSERT INTO customers (name, company) VALUES ('Brad', '2M Graphics')")
        wrong_customer = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute("INSERT INTO contacts (customer_id, name, email) VALUES (?, 'Brad', 'sales@goldcoastplastics.com.au')", (wrong_customer,))
        conn.commit()
        conn.close()
        result = self.client.post('/api/extension/match', json={'email': 'sales@goldcoastplastics.com.au'})
        payload = result.get_json()
        self.assertEqual(payload['match_state'], 'identity_conflict')
        self.assertIn('不一致', payload['match_warning'])

    def test_display_name_is_only_a_low_confidence_candidate(self):
        result = self.client.post('/api/extension/match', json={'name': 'Mina'})
        self.assertEqual(result.status_code, 200)
        payload = result.get_json()
        self.assertEqual(payload['match_state'], 'name_candidate')
        self.assertEqual(payload['name_candidates'][0]['contact']['name'], 'Mina')
        self.assertEqual(payload['name_candidates'][0]['confidence'], 'low')

    def test_exact_match_explains_its_evidence(self):
        payload = self.client.post('/api/extension/match', json={'email': 'buyer@example.com'}).get_json()
        self.assertEqual(payload['match_state'], 'unique')
        self.assertIn('邮箱', payload['exact_reason'])
        self.assertEqual(len(payload['customers']), 1)
        self.assertEqual(payload['contacts'][0]['confidence'], 'high')

    def test_public_mailbox_domain_is_not_treated_as_a_company(self):
        payload = self.client.post('/api/extension/match', json={'email': 'someone@gmail.com'}).get_json()
        self.assertEqual(payload['match_state'], 'unmatched')
        self.assertEqual(payload['domain_candidates'], [])

    def test_save_rejects_contact_from_another_customer(self):
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        conn.execute("INSERT INTO customers (name, company) VALUES ('Other', 'Other Company')")
        other_customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute("INSERT INTO contacts (customer_id, name, email) VALUES (?, 'Other Contact', 'other@example.com')", (other_customer_id,))
        other_contact_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.commit()
        conn.close()
        response = self.client.post('/api/extension/communications', json={
            'customer_id': self.customer_id, 'contact_id': other_contact_id,
            'channel': 'netease', 'messages': [{'fingerprint': 'foreign-contact', 'text': 'hello'}],
            'content': 'hello',
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('不属于当前客户', response.get_json()['error'])

    def test_save_deduplicates_repeated_message_in_one_capture(self):
        message = {'message_id': 'same-message', 'time': '2026-08-10 10:00', 'direction': 'inbound',
                   'text': 'Same DOM message', 'fingerprint': 'same-dom-message'}
        response = self.client.post('/api/extension/communications', json={
            'customer_id': self.customer_id, 'channel': 'whatsapp', 'content': message['text'],
            'messages': [message, dict(message)],
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['new_message_count'], 1)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM communication_source_items').fetchone()[0], 1)
        conn.close()
