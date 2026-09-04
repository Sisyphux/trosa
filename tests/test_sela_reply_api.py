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


TOKEN = 'test-sela-integration-token'


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_sela_reply_api_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


def outbound_payload(candidate_id='candidate-reply-1'):
    return {
        'candidate_id': candidate_id,
        'company': 'Acrílicos S.A.',
        'country': 'Brazil',
        'website': 'https://acrilicos.com/',
        'business_type': 'Acrylic sheet fabricator',
        'source_run': 'run-2026-08-24',
        'contact': {'name': 'Ana Silva', 'email': 'ana@acrilicos.com', 'is_primary': 1},
        'outreach': {
            'status': 'SENT',
            'sent_at': '2026-08-24 10:00:00',
            'subject': 'Acrylic sheet supply',
            'content': 'Hello Ana, we can support your acrylic sheet sourcing.',
            'updated_at': '2026-08-24 10:00:00',
        },
        'idempotency_key': f'sela:{candidate_id}:event-1',
    }


class SelaReplyApiTest(unittest.TestCase):
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

    def headers(self, key=None):
        result = {'Authorization': f'Bearer {TOKEN}'}
        if key:
            result['X-Idempotency-Key'] = key
        return result

    def post_outbound(self, body):
        return self.client.post(
            '/api/integrations/sela/sync',
            json=body,
            headers=self.headers(body['idempotency_key']),
        )

    def hamid_db(self):
        db.set_db_user('hamid')
        return db.get_db()

    def test_reply_writes_inbound_timeline_and_dated_task_idempotently(self):
        outbound = outbound_payload()
        first_outbound = self.post_outbound(outbound)
        self.assertEqual(first_outbound.status_code, 200, first_outbound.get_data(as_text=True))
        trosa_id = first_outbound.get_json()['trosa_id']
        reply = {
            'candidate_id': outbound['candidate_id'],
            'trosa_id': trosa_id,
            'reply': {
                'message_id': 'gmail-reply-1',
                'rfc_message_id': '<gmail-reply-1@example.com>',
                'thread_id': 'gmail-thread-1',
                'from': 'Ana Silva <ana@acrilicos.com>',
                'subject': 'Re: Acrylic sheet supply',
                'received_at': 'Mon, 17 Aug 2026 09:00:00 +0800',
                'body': 'Please contact me next month.',
            },
            'action': {
                'name': 'FOLLOW_UP_SCHEDULED',
                'route': 'SCHEDULE_FOLLOW_UP',
                'intent': 'FOLLOW_UP_REQUEST',
                'event': 'INTERESTED',
                'reason': '客户明确要求未来日期再次联系。',
                'next_task': {
                    'title': '按客户要求再次联系',
                    'due_date': '2026-09-17',
                    'reason': '客户要求未来日期再次联系',
                },
            },
            'idempotency_key': 'sela-reply:reply-1',
        }
        first = self.client.post(
            '/api/integrations/sela/reply',
            json=reply,
            headers=self.headers(reply['idempotency_key']),
        )
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        body = first.get_json()
        self.assertEqual(body['status'], 'SYNCED')
        self.assertIsNotNone(body['activity_id'])
        self.assertIsNotNone(body['task_id'])

        second = self.client.post(
            '/api/integrations/sela/reply',
            json=reply,
            headers=self.headers(reply['idempotency_key']),
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json(), body)

        conn = self.hamid_db()
        try:
            activity = conn.execute(
                '''SELECT content, follow_date, direction, activity_type, source, related_task_id
                   FROM follow_up_logs WHERE customer_id=? ORDER BY id DESC LIMIT 1''',
                (trosa_id,),
            ).fetchone()
            self.assertIn('Please contact me next month.', activity['content'])
            self.assertEqual(activity['follow_date'], '2026-08-17')
            self.assertEqual(activity['direction'], 'inbound')
            self.assertEqual(activity['activity_type'], 'customer_reply')
            self.assertEqual(activity['source'], 'sela_reply_engine')
            self.assertIsNone(activity['related_task_id'])
            self.assertEqual(
                conn.execute(
                    "SELECT source_activity_id FROM reminders WHERE id=?",
                    (body['task_id'],),
                ).fetchone()[0],
                body['activity_id'],
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM reminders WHERE customer_id=? AND is_done=0 AND remind_date=?",
                    (trosa_id, '2026-09-17'),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM integration_sync_receipts WHERE idempotency_key=?",
                    (reply['idempotency_key'],),
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()

    def test_reply_route_requires_exact_existing_sela_link(self):
        reply = {
            'candidate_id': 'missing-link',
            'trosa_id': 999,
            'reply': {'message_id': 'gmail-reply-2', 'subject': 'Re: hello', 'body': 'Hello'},
            'action': {'name': 'HUMAN_REVIEW', 'route': 'HUMAN_REVIEW', 'intent': 'GENERAL_REPLY'},
            'idempotency_key': 'sela-reply:missing-link',
        }
        response = self.client.post(
            '/api/integrations/sela/reply',
            json=reply,
            headers=self.headers(reply['idempotency_key']),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'REVIEW')
        self.assertEqual(response.get_json()['reason'], 'TROSA_CUSTOMER_NOT_FOUND')


if __name__ == '__main__':
    unittest.main()
