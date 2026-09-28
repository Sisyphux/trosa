"""Sela service identity must be able to read the same business facts the UI shows.

Covers the read-surface completion: Today/upcoming reminders and the global
communication feed (with the inbound = real customer reply filter) are readable
by the Sela service credential, and the feed's direction/since/limit semantics
match the canonical Customer timeline.
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


SERVICE_TOKEN = 'trosa_sela_read_surface_token'


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_sela_read_surface_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


class SelaReadSurfaceTest(unittest.TestCase):
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
                'token_sha256': hashlib.sha256(SERVICE_TOKEN.encode('utf-8')).hexdigest(),
                'enabled': True,
                'user': 'hamid',
            })),
        )
        conn.commit()
        conn.close()
        self.module = load_app()
        self.session = self.module.app.test_client()
        self.assertEqual(self.session.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)
        self.service = self.module.app.test_client()
        self.service_headers = {'Authorization': f'Bearer {SERVICE_TOKEN}'}

    def tearDown(self):
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    def _create_customer(self, name):
        created = self.session.post('/api/customers', json={'name': name, 'company': f'{name} GmbH'})
        self.assertEqual(created.status_code, 201, created.get_json())
        return created.get_json()['id']

    def _record(self, customer_id, *, direction, content, follow_date, activity_type='follow_up'):
        saved = self.session.post(f'/api/customers/{customer_id}/follow_history', json={
            'activity_content': content,
            'activity_type': activity_type,
            'direction': direction,
            'follow_date': follow_date,
            'source': 'sela-read-surface-test',
        })
        self.assertEqual(saved.status_code, 200, saved.get_json())
        return saved.get_json()

    def test_service_identity_can_read_today_upcoming_and_global_feed(self):
        for path in (
            '/api/reminders/today',
            '/api/reminders/upcoming',
            '/api/follow-history',
            '/api/customers',
            '/api/inbox',
        ):
            response = self.service.get(path, headers=self.service_headers)
            self.assertEqual(response.status_code, 200, f'{path} -> {response.status_code}')

    def test_service_identity_still_cannot_read_admin_or_write_feed(self):
        self.assertEqual(
            self.service.get('/api/agent-gateway/tokens', headers=self.service_headers).status_code,
            401,
        )
        self.assertEqual(
            self.service.post('/api/integrations/sela/token', headers=self.service_headers).status_code,
            401,
        )
        # The communication feed stays read-only for every caller.
        self.assertEqual(
            self.service.post('/api/follow-history', headers=self.service_headers).status_code,
            405,
        )

    def test_inbound_filter_returns_only_real_customer_replies(self):
        stilform = self._create_customer('Stilform')
        other = self._create_customer('Middle Ocean Sign')
        self._record(stilform, direction='inbound', content='Yes please send the quote',
                     follow_date='2026-09-10', activity_type='customer_reply')
        self._record(stilform, direction='outbound', content='Following up on the quote',
                     follow_date='2026-09-05')
        self._record(other, direction='inbound', content='We received the samples',
                     follow_date='2026-09-12', activity_type='customer_reply')

        response = self.service.get(
            '/api/follow-history?direction=inbound&limit=50',
            headers=self.service_headers,
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        rows = response.get_json()
        self.assertEqual({row['direction'] for row in rows}, {'inbound'})
        self.assertEqual({row['customer_id'] for row in rows}, {stilform, other})
        # customer_name is attached so an external reader does not need a
        # second lookup per reply.
        self.assertTrue(all(row.get('customer_name') for row in rows))

        outbound = self.service.get('/api/follow-history?direction=outbound', headers=self.service_headers)
        self.assertEqual(outbound.status_code, 200)
        self.assertEqual([row['customer_id'] for row in outbound.get_json()], [stilform])

    def test_since_and_customer_filters_and_validation(self):
        customer_id = self._create_customer('Acrimet')
        self._record(customer_id, direction='inbound', content='Old reply', follow_date='2026-08-01')
        self._record(customer_id, direction='inbound', content='New reply', follow_date='2026-09-20')

        recent = self.service.get(
            '/api/follow-history?direction=inbound&since=2026-09-01',
            headers=self.service_headers,
        )
        self.assertEqual(recent.status_code, 200)
        contents = [row['content'] for row in recent.get_json()]
        self.assertEqual(contents, ['New reply'])

        single = self.service.get(
            f'/api/follow-history?customer_id={customer_id}',
            headers=self.service_headers,
        )
        self.assertEqual(single.status_code, 200)
        self.assertEqual({row['customer_id'] for row in single.get_json()}, {customer_id})

        self.assertEqual(
            self.service.get('/api/follow-history?direction=sideways', headers=self.service_headers).status_code,
            400,
        )
        self.assertEqual(
            self.service.get('/api/follow-history?since=09-01-2026', headers=self.service_headers).status_code,
            400,
        )
        self.assertEqual(
            self.service.get('/api/follow-history?customer_id=abc', headers=self.service_headers).status_code,
            400,
        )

    def test_bounded_limit_caps_oversized_requests(self):
        customer_id = self._create_customer('Bentleigh Group')
        for index in range(3):
            self._record(customer_id, direction='outbound', content=f'Message {index}',
                         follow_date=f'2026-09-0{index + 1}')
        response = self.service.get('/api/follow-history?limit=9999', headers=self.service_headers)
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(response.get_json()), 200)


if __name__ == '__main__':
    unittest.main()
