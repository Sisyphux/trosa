import hashlib
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

import sys

sys.path.insert(0, str(ROOT))

import db


SERVICE_TOKEN = 'trosa_sela_test_service_token'
LEGACY_TOKEN = 'legacy-prospecting-lab-token'


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_sela_service_auth_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


class SelaServiceAuthTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        self.original_demo = os.environ.get('CRM_SEED_DEMO_DATA')
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.init_all_dbs()
        conn = db.get_system_db()
        for key, token in (
            ('integration_token:sela:hamid', SERVICE_TOKEN),
            ('integration_token:prospecting_lab:hamid', LEGACY_TOKEN),
        ):
            conn.execute(
                '''INSERT INTO app_settings (key, value, updated_at)
                   VALUES (?, ?, datetime('now', 'localtime'))''',
                (key, json.dumps({
                    'token_sha256': hashlib.sha256(token.encode('utf-8')).hexdigest(),
                    'enabled': True,
                    'user': 'hamid',
                })),
            )
        conn.commit()
        conn.close()
        self.module = load_app()

    def tearDown(self):
        db.set_db_user(None)
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    def test_service_token_has_only_hamid_customer_operations_and_legacy_token_remains_compatible(self):
        service = self.module.app.test_client()
        service_headers = {'Authorization': f'Bearer {SERVICE_TOKEN}'}
        self.assertEqual(
            service.get('/api/integrations/sela/health', headers=service_headers).status_code,
            200,
        )
        self.assertEqual(service.get('/api/customers', headers=service_headers).status_code, 200)
        self.assertEqual(service.get('/api/inbox', headers=service_headers).status_code, 200)
        self.assertEqual(service.post('/api/customers', json={'company': 'Sela Customer'}, headers=service_headers).status_code, 201)
        self.assertEqual(service.get('/api/agent-gateway/tokens', headers=service_headers).status_code, 401)
        self.assertEqual(
            service.post('/api/integrations/sela/token', headers=service_headers).status_code,
            401,
        )

        legacy = self.module.app.test_client()
        legacy_headers = {'Authorization': f'Bearer {LEGACY_TOKEN}'}
        self.assertEqual(legacy.get('/api/customers', headers=legacy_headers).status_code, 200)
        self.assertEqual(
            legacy.get('/api/integrations/sela/health', headers=legacy_headers).status_code,
            200,
        )

    def test_issuance_stores_only_digest_and_replaces_previous_service_token(self):
        session = self.module.app.test_client()
        self.assertEqual(session.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)
        response = session.post('/api/integrations/sela/token')
        self.assertEqual(response.status_code, 200, response.get_json())
        token = response.get_json()['token']
        self.assertTrue(token.startswith('trosa_sela_'))

        conn = sqlite3.connect(os.path.join(db.DB_DIR, 'system.db'))
        try:
            stored = conn.execute(
                'SELECT value FROM app_settings WHERE key=?',
                ('integration_token:sela:hamid',),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertNotIn(token, stored)
        self.assertIn(hashlib.sha256(token.encode('utf-8')).hexdigest(), stored)

        service = self.module.app.test_client()
        self.assertEqual(
            service.get(
                '/api/integrations/sela/health',
                headers={'Authorization': f'Bearer {token}'},
            ).status_code,
            200,
        )


if __name__ == '__main__':
    unittest.main()
