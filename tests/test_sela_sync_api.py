import copy
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db


TOKEN = 'test-sela-integration-token'


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_sela_sync_api_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


def payload(candidate_id='candidate-1', website='https://acrilicos.com/'):
    return {
        'candidate_id': candidate_id,
        'company': 'Acrílicos S.A.',
        'country': 'Brazil',
        'website': website,
        'business_type': 'Acrylic sheet fabricator',
        'source_run': 'run-2026-08-24',
        'source_note': f'[Sela Candidate ID: {candidate_id}]\nPublic evidence verified.',
        'contact': {
            'name': 'Ana Silva',
            'title': 'Purchasing',
            'email': 'INFO@acrilicos.com',
            'is_primary': 1,
        },
        'outreach': {
            'status': 'SENT',
            'sent_at': '2026-08-24 10:00:00',
            'subject': 'Acrylic sheet supply',
            'content': 'Hello Ana, we can support your acrylic sheet sourcing.',
            'updated_at': '2026-08-24 10:00:00',
        },
        'idempotency_key': f'sela:{candidate_id}:event-1',
    }


class SelaSyncApiTest(unittest.TestCase):
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
            (
                'integration_token:prospecting_lab:hamid',
                json.dumps({
                    'token_sha256': hashlib.sha256(TOKEN.encode('utf-8')).hexdigest(),
                    'enabled': True,
                    'user': 'hamid',
                }),
            ),
        )
        conn.commit()
        conn.close()
        self.module = load_app()
        self.client = self.module.app.test_client()

    def tearDown(self):
        db.set_db_user(None)
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    def request_headers(self, key=None):
        headers = {'Authorization': f'Bearer {TOKEN}'}
        if key:
            headers['X-Idempotency-Key'] = key
        return headers

    def hamid_db(self):
        db.set_db_user('hamid')
        return db.get_db()

    def post_sync(self, body, key=None):
        return self.client.post(
            '/api/integrations/sela/sync',
            json=body,
            headers=self.request_headers(key or body.get('idempotency_key')),
        )

    def test_health_requires_token_and_reports_ready_schema(self):
        self.assertEqual(self.client.get('/api/integrations/sela/health').status_code, 401)
        response = self.client.get(
            '/api/integrations/sela/health', headers=self.request_headers()
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['sync_api'], 'sela-v1')
        self.assertEqual(body['schema_version'], 1)
        self.assertTrue(body['data_version'])

    def test_sync_is_atomic_and_replaying_same_event_is_safe(self):
        body = payload()
        first = self.post_sync(body)
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        first_body = first.get_json()
        self.assertEqual(first_body['status'], 'SYNCED')
        self.assertTrue(first_body['created'])

        second = self.post_sync(body)
        self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
        self.assertEqual(second.get_json(), first_body)

        conn = self.hamid_db()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM contacts').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM outreach_emails').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM external_analysis_notes').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM integration_sync_receipts').fetchone()[0], 1)
        finally:
            conn.close()

        changed = copy.deepcopy(body)
        changed['outreach']['content'] = 'A different event body.'
        conflict = self.post_sync(changed)
        self.assertEqual(conflict.status_code, 409)

    def test_exact_domain_does_not_collide_with_longer_domain(self):
        conn = self.hamid_db()
        conn.execute(
            '''INSERT INTO customers
               (name, company, website, status, customer_type, import_source)
               VALUES (?, ?, ?, ?, ?, ?)''',
            ('Total Acrílicos', 'Total Acrílicos', 'https://www.totalacrilicos.com.br/',
             '跟进中', 'existing', 'manual'),
        )
        conn.commit()
        conn.close()

        response = self.post_sync(payload('candidate-domain'))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['status'], 'SYNCED')
        self.assertTrue(response.get_json()['created'])

        conn = self.hamid_db()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0], 2)
        finally:
            conn.close()

    def test_existing_exact_identity_is_linked_without_duplicate_customer(self):
        conn = self.hamid_db()
        cursor = conn.execute(
            '''INSERT INTO customers
               (name, company, website, status, customer_type, import_source)
               VALUES (?, ?, ?, ?, ?, ?)''',
            ('Acrílicos S.A.', 'Acrílicos S.A.', 'https://www.acrilicos.com/',
             '未建联', 'new', 'research'),
        )
        existing_id = cursor.lastrowid
        conn.commit()
        conn.close()

        response = self.post_sync(payload('candidate-existing'))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        self.assertEqual(body['status'], 'SYNCED')
        self.assertFalse(body['created'])
        self.assertEqual(body['trosa_id'], existing_id)

        conn = self.hamid_db()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM outreach_emails').fetchone()[0], 1)
        finally:
            conn.close()

    def test_exclusion_snapshot_supports_conditional_get(self):
        response = self.post_sync(payload('candidate-etag'))
        self.assertEqual(response.status_code, 200)

        first = self.client.get(
            '/api/integrations/sela/exclusions', headers=self.request_headers()
        )
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.get_json()['records'])
        etag = first.headers.get('ETag')
        self.assertTrue(etag)

        second = self.client.get(
            '/api/integrations/sela/exclusions',
            headers={**self.request_headers(), 'If-None-Match': etag},
        )
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.get_data(), b'')


if __name__ == '__main__':
    unittest.main()
