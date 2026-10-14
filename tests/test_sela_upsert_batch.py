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

import db

TOKEN = 'test-sela-upsert-batch-token'


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_sela_upsert_batch_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


def prospect(index):
    sid = f'batch-{index:04d}'
    return {
        'source_id': sid, 'company': f'Batch Co {index:04d}', 'country': 'Brazil',
        'website': f'https://{sid}.example/', 'business_type': 'Acrylic sheet fabricator',
        'status': 'READY TO CONTACT', 'research_status': 'VERIFIED', 'confidence': 'HIGH',
        'reason': 'Public evidence.', 'source_urls': [f'https://{sid}.example/about'],
        'contact': {'name': f'Person {index}', 'email': f'buyer{index}@{sid}.example', 'is_primary': 1},
        'email_source_url': f'https://{sid}.example/contact', 'email_type': 'person',
    }


class SelaUpsertBatchTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.init_all_dbs()
        conn = db.get_system_db()
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, datetime('now', 'localtime'))",
            ('integration_token:sela:hamid', json.dumps({
                'token_sha256': hashlib.sha256(TOKEN.encode('utf-8')).hexdigest(),
                'enabled': True, 'user': 'hamid',
            })),
        )
        conn.commit()
        conn.close()
        self.module = load_app()
        self.client = self.module.app.test_client()

    def tearDown(self):
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.original_db_dir
        self.tempdir.cleanup()

    def post(self, index):
        key = f'upsert-batch:{index}'
        return self.client.post(
            '/api/integrations/sela/prospects',
            json={'prospect': prospect(index), 'idempotency_key': key},
            headers={'Authorization': f'Bearer {TOKEN}', 'X-Idempotency-Key': key},
        )

    def counted_post(self, index):
        statements = []
        original = db.get_db

        def counting_get_db():
            conn = original()
            conn.set_trace_callback(statements.append)
            return conn

        db.get_db = counting_get_db
        try:
            response = self.post(index)
        finally:
            db.get_db = original
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return len(statements)

    def test_statements_per_upsert_do_not_grow_with_customer_count(self):
        for index in range(3):
            self.assertEqual(self.post(index).status_code, 200)
        small = self.counted_post(100)
        for index in range(3, 40):
            self.assertEqual(self.post(index).status_code, 200)
        large = self.counted_post(101)
        self.assertEqual(small, large)

    def test_duplicate_email_still_matches_existing_customer(self):
        for index in range(5):
            self.assertEqual(self.post(index).status_code, 200)
        body = prospect(200)
        body['contact'] = {'name': 'Same Buyer', 'email': 'BUYER3@batch-0003.example'}
        key = 'upsert-batch:200'
        response = self.client.post(
            '/api/integrations/sela/prospects',
            json={'prospect': body, 'idempotency_key': key},
            headers={'Authorization': f'Bearer {TOKEN}', 'X-Idempotency-Key': key},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertNotEqual(response.get_json().get('status'), 'CREATED')


if __name__ == '__main__':
    unittest.main()
