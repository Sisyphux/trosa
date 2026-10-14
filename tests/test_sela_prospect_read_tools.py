"""S9 read tools: read one prospect, read a few by ids, refuse unknown params.

The whole-worklist read is the slow path; S9 gives the Agent a direct way to
read a single prospect or a bounded set.  These tests pin the response shapes,
the ordering, the missing-id report and the "unknown parameter is an error,
never silently ignored" rule.
"""
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


TOKEN = 'test-sela-read-tools-token'


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_sela_read_tools_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


def prospect(source_id, company):
    return {
        'source_id': source_id,
        'company': company,
        'country': 'Germany',
        'website': f'https://{source_id}.example/',
        'status': 'READY TO CONTACT',
        'contact': {'name': 'Buyer', 'title': 'Purchasing', 'email': f'buyer@{source_id}.example'},
        'outreach_status': 'EMAIL_VERIFY',
    }


class SelaProspectReadToolsTest(unittest.TestCase):
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
                'enabled': True,
                'user': 'hamid',
            })),
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

    def upsert(self, source_id, company):
        response = self.client.post(
            '/api/integrations/sela/prospects',
            json={'prospect': prospect(source_id, company),
                  'idempotency_key': f'sela-v2:{source_id}:read-tools'},
            headers={**self.headers, 'X-Idempotency-Key': f'sela-v2:{source_id}:read-tools'},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def list_prospects(self):
        response = self.client.get('/api/integrations/sela/prospects?limit=100', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()['prospects']

    # ---- single prospect -------------------------------------------------
    def test_single_prospect_matches_the_list_view(self):
        self.upsert('alpha', 'Alpha GmbH')
        self.upsert('beta', 'Beta GmbH')

        single = self.client.get('/api/integrations/sela/prospects/beta', headers=self.headers)
        self.assertEqual(single.status_code, 200, single.get_json())
        body = single.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(body['prospect_api'], 'sela-v2')
        self.assertEqual(body['prospect']['id'], 'beta')
        listed = {row['id']: row for row in self.list_prospects()}
        self.assertEqual(body['prospect'], listed['beta'])

    def test_single_prospect_unknown_source_id_is_404(self):
        self.upsert('alpha', 'Alpha GmbH')
        response = self.client.get('/api/integrations/sela/prospects/nope', headers=self.headers)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.get_json()['success'])

    # ---- ids -------------------------------------------------------------
    def test_ids_returns_requested_prospects_in_request_order(self):
        for source_id in ('a-one', 'b-two', 'c-three'):
            self.upsert(source_id, source_id.upper())
        response = self.client.get(
            '/api/integrations/sela/prospects?ids=c-three,a-one', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        self.assertEqual([row['id'] for row in body['prospects']], ['c-three', 'a-one'])
        self.assertEqual(body['missing'], [])

    def test_ids_reports_unknown_ids_without_failing(self):
        self.upsert('known', 'Known GmbH')
        response = self.client.get(
            '/api/integrations/sela/prospects?ids=known,ghost', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        self.assertEqual([row['id'] for row in body['prospects']], ['known'])
        self.assertEqual(body['missing'], ['ghost'])

    def test_ids_allows_exactly_one_hundred(self):
        for index in range(100):
            self.upsert(f'prospect-{index}', f'Company {index}')
        ids = ','.join(f'prospect-{index}' for index in range(100))
        response = self.client.get(f'/api/integrations/sela/prospects?ids={ids}', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(len(response.get_json()['prospects']), 100)

    def test_ids_over_one_hundred_is_400(self):
        ids = ','.join(f'prospect-{index}' for index in range(101))
        response = self.client.get(f'/api/integrations/sela/prospects?ids={ids}', headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])

    def test_empty_id_token_is_400(self):
        response = self.client.get('/api/integrations/sela/prospects?ids=a,,b', headers=self.headers)
        self.assertEqual(response.status_code, 400)

    def test_ids_cannot_combine_with_paging(self):
        response = self.client.get('/api/integrations/sela/prospects?ids=a&after=5', headers=self.headers)
        self.assertEqual(response.status_code, 400)

    # ---- unknown parameters ---------------------------------------------
    def test_unknown_list_param_is_400_with_supported_list(self):
        response = self.client.get('/api/integrations/sela/prospects?bogus=1', headers=self.headers)
        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        self.assertFalse(body['success'])
        self.assertIn('ids', body['supported_params'])
        self.assertIn('after', body['supported_params'])

    def test_unknown_single_param_is_400(self):
        self.upsert('alpha', 'Alpha GmbH')
        response = self.client.get('/api/integrations/sela/prospects/alpha?bogus=1', headers=self.headers)
        self.assertEqual(response.status_code, 400)

    # ---- auth ------------------------------------------------------------
    def test_read_routes_require_the_service_token(self):
        self.upsert('alpha', 'Alpha GmbH')
        self.assertEqual(self.client.get('/api/integrations/sela/prospects/alpha').status_code, 401)
        self.assertEqual(self.client.get('/api/integrations/sela/prospects?ids=alpha').status_code, 401)


if __name__ == '__main__':
    unittest.main()
