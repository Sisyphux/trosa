"""Tests for tools/reconcile_prospect_read.py (read-only reconciliation).

The script normally talks to a live Trosa with the Sela service token; here a
Flask test client stands in for the transport so the reconciliation logic is
exercised without a network or a second process.
"""

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import db  # noqa: E402

TOKEN = 'test-reconcile-token'


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_reconcile_test_app', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


def load_reconciler():
    spec = importlib.util.spec_from_file_location('reconcile_prospect_read', ROOT / 'tools' / 'reconcile_prospect_read.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FlaskTransport:
    """Adapter that makes the reconciler's HttpTransport shape hit a test client."""

    def __init__(self, client, token):
        self.client = client
        self.token = token

    def get(self, path, params, headers=None):
        query = urlencode(sorted((k, v) for k, v in params.items() if v not in (None, '')))
        url = path + ('?' + query if query else '')
        request_headers = {'Authorization': 'Bearer ' + self.token}
        if headers:
            request_headers.update({str(k): str(v) for k, v in headers.items()})
        response = self.client.get(url, headers=request_headers)
        body = {} if response.status_code == 304 else (response.get_json() or {})
        return response.status_code, dict(response.headers), body


class ReconcileProspectReadTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        self.original_seed = os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.DB_DIR = self.tempdir.name
        db.init_all_dbs()
        conn = db.get_system_db()
        conn.execute(
            'INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)',
            ('integration_token:sela:hamid', json.dumps({
                'token_sha256': hashlib.sha256(TOKEN.encode()).hexdigest(),
                'enabled': True, 'user': 'hamid'}), "2026-01-01 00:00:00"),
        )
        conn.commit()
        conn.close()
        self.module = load_app()
        self.reconciler = load_reconciler()
        self.client = self.module.app.test_client()
        self.headers = {'Authorization': 'Bearer ' + TOKEN}

    def tearDown(self):
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.original_db_dir
        if self.original_seed is not None:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_seed
        self.tempdir.cleanup()

    def prospect(self, source_id, company=''):
        return {
            'source_id': source_id,
            'company': company or (source_id + ' GmbH'),
            'country': 'Germany',
            'website': 'https://%s.example/' % source_id,
            'status': 'READY TO CONTACT',
            'contact': {'name': 'Buyer', 'title': 'Purchasing', 'email': 'buyer@%s.example' % source_id},
            'outreach_status': 'EMAIL_VERIFY',
        }

    def upsert(self, source_id, company=''):
        return self.client.post(
            '/api/integrations/sela/prospects',
            json={'prospect': self.prospect(source_id, company), 'idempotency_key': 'sela-v2:%s:reconcile' % source_id},
            headers={**self.headers, 'X-Idempotency-Key': 'sela-v2:%s:reconcile' % source_id},
        ).get_json()

    def transport(self):
        return FlaskTransport(self.client, TOKEN)

    def test_reconciliation_passes_on_a_consistent_feed(self):
        self.upsert('alpha', 'Alpha GmbH')
        self.upsert('beta', 'Beta GmbH')

        ok, lines = self.reconciler.reconcile(
            self.transport(), [{'source_id': 'alpha', 'expect_zero_sends': True}])
        report = '\n'.join(lines)
        self.assertTrue(ok, report)
        self.assertTrue(any('feed total matches' in line for line in lines), report)
        self.assertTrue(any('no duplicate prospect ids' in line for line in lines), report)
        self.assertTrue(any('needs=email' in line and line.startswith('[PASS]') for line in lines), report)
        self.assertTrue(any('alpha: outreach=' in line and line.endswith('PASS') for line in lines), report)
        self.assertTrue(any('If-None-Match returns 304' in line for line in lines), report)

    def test_reconciliation_flags_a_wrong_expectation(self):
        self.upsert('alpha', 'Alpha GmbH')

        ok, lines = self.reconciler.reconcile(
            self.transport(), [{'source_id': 'alpha', 'expect_first_sent': '2026-08-13'}])
        self.assertFalse(ok)
        self.assertTrue(any(line.startswith('[FAIL]') for line in lines), '\n'.join(lines))

    def test_reconciliation_flags_a_missing_prospect(self):
        ok, lines = self.reconciler.reconcile(
            self.transport(), [{'source_id': 'ghost', 'expect_zero_sends': True}])
        self.assertFalse(ok)
        self.assertTrue(any('ghost: read failed' in line for line in lines), '\n'.join(lines))


if __name__ == '__main__':
    unittest.main()
