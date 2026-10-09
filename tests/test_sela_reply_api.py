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
                'integration_token:sela:hamid',
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
        db.cancel_safety_backup()
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
        outreach = body.get('outreach') or {}
        prospect = {
            'source_id': body['candidate_id'],
            'company': body.get('company'),
            'country': body.get('country'),
            'website': body.get('website'),
            'business_type': body.get('business_type'),
            'source_run': body.get('source_run'),
            'contact': body.get('contact') or {},
            'outreach_status': outreach.get('status'),
            'subject': outreach.get('subject'),
            'email_draft': outreach.get('content'),
            'sent_at': outreach.get('sent_at'),
            'gmail_message_id': body.get('gmail_message_id'),
            'gmail_thread_id': body.get('gmail_thread_id'),
        }
        return self.client.post(
            '/api/integrations/sela/prospects',
            json={'prospect': prospect, 'idempotency_key': body['idempotency_key']},
            headers=self.headers(body['idempotency_key']),
        )

    def hamid_db(self):
        db.set_db_user('hamid')
        return db.get_db()

    def test_reply_writes_inbound_timeline_and_dated_task_idempotently(self):
        outbound = outbound_payload()
        conn = self.hamid_db()
        cursor = conn.execute(
            '''INSERT INTO customers(name, company, website, status, customer_type, import_source)
               VALUES (?, ?, ?, ?, ?, ?)''',
            ('Acrílicos S.A.', 'Acrílicos S.A.', 'https://acrilicos.com/',
             '未建联', 'new', 'research'),
        )
        conn.execute(
            '''INSERT INTO contacts(customer_id, name, email)
               VALUES (?, ?, ?)''',
            (cursor.lastrowid, 'Ana Silva', 'ana@acrilicos.com'),
        )
        conn.commit()
        conn.close()
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
                '''SELECT content, result, follow_date, direction, activity_type, source, related_task_id
                   FROM follow_up_logs WHERE customer_id=? ORDER BY id DESC LIMIT 1''',
                (trosa_id,),
            ).fetchone()
            # The mail body never becomes the record: it is summarised on entry
            # (no model in tests -> deterministic one-line fact with the subject).
            self.assertNotIn('Please contact me next month.', activity['content'])
            self.assertNotIn('正文', activity['content'])
            self.assertEqual(activity['content'], '客户回复了邮件：Acrylic sheet supply')
            self.assertIn('事件：INTERESTED', activity['result'])
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

    def test_automatic_outbound_reply_is_a_canonical_trosa_timeline_fact(self):
        outbound = outbound_payload('candidate-auto-reply')
        first_outbound = self.post_outbound(outbound)
        self.assertEqual(first_outbound.status_code, 200, first_outbound.get_data(as_text=True))
        trosa_id = first_outbound.get_json()['trosa_id']
        reply = {
            'candidate_id': outbound['candidate_id'],
            'trosa_id': trosa_id,
            'reply': {
                'message_id': 'gmail-inbound-auto-1',
                'thread_id': 'gmail-thread-auto',
                'from': 'Ana Silva <ana@acrilicos.com>',
                'subject': 'Re: Acrylic sheet supply',
                'received_at': '2026-09-09T09:00:00+08:00',
                'body': 'Please send your catalogue.',
            },
            'action': {
                'name': 'CATALOGUE_SENT',
                'route': 'AUTO_CATALOGUE',
                'intent': 'INTERESTED',
                'event': 'INTERESTED',
                'outbound': {
                    'message_id': 'gmail-outbound-auto-1',
                    'thread_id': 'gmail-thread-auto',
                    'sent_at': '2026-09-09T09:01:00+08:00',
                    'subject': 'Our acrylic sheet catalogue',
                    'body': 'Thank you. Here is our current acrylic sheet catalogue.',
                    'in_reply_to': '<gmail-inbound-auto-1@example.com>',
                },
            },
            'idempotency_key': 'sela-reply:auto-outbound-1',
        }
        first = self.client.post(
            '/api/integrations/sela/reply',
            json=reply,
            headers=self.headers(reply['idempotency_key']),
        )
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertEqual(first.get_json()['status'], 'SYNCED')

        second = self.client.post(
            '/api/integrations/sela/reply',
            json=reply,
            headers=self.headers(reply['idempotency_key']),
        )
        self.assertEqual(second.status_code, 200)

        # A normal Sela refresh submits the current Prospect projection after
        # the reply write. It must not replay a stale initial-send timestamp
        # over the canonical reply outcome.
        refresh = {
            'source_id': outbound['candidate_id'],
            'company': outbound['company'],
            'website': outbound['website'],
            'country': outbound['country'],
            'business_type': outbound['business_type'],
            'contact': outbound['contact'],
            'outreach_status': 'INTERESTED',
            'subject': outbound['outreach']['subject'],
            'email_draft': outbound['outreach']['content'],
            'sent_at': outbound['outreach']['sent_at'],
            'gmail_message_id': 'gmail-outbound-auto-1',
        }
        refreshed = self.client.post(
            '/api/integrations/sela/prospects',
            json={'prospect': refresh, 'idempotency_key': 'sela-v2:candidate-auto-reply:refresh'},
            headers=self.headers('sela-v2:candidate-auto-reply:refresh'),
        )
        self.assertEqual(refreshed.status_code, 200, refreshed.get_data(as_text=True))

        conn = self.hamid_db()
        try:
            rows = conn.execute(
                '''SELECT content, follow_date, direction, activity_type, source
                   FROM follow_up_logs WHERE customer_id=? ORDER BY id ASC''',
                (trosa_id,),
            ).fetchall()
            outbound_rows = [row for row in rows if row['direction'] == 'outbound']
            self.assertEqual(len(outbound_rows), 1)
            self.assertIn('[Sela Outbound Reply ID: gmail-outbound-auto-1]', outbound_rows[0]['content'])
            self.assertIn('Here is our current acrylic sheet catalogue.', outbound_rows[0]['content'])
            self.assertEqual(outbound_rows[0]['follow_date'], '2026-09-09')
            self.assertEqual(outbound_rows[0]['activity_type'], 'email')
            self.assertEqual(outbound_rows[0]['source'], 'sela_reply_engine')
            outreach = conn.execute(
                'SELECT reply_status, reply_content, reply_date, external_updated_at FROM outreach_emails'
            ).fetchone()
            self.assertEqual(outreach['reply_status'], 'replied')
            self.assertEqual(outreach['reply_content'], 'Please send your catalogue.')
            self.assertEqual(outreach['reply_date'], '2026-09-09')
            self.assertEqual(outreach['external_updated_at'], '2026-09-09T09:00:00+08:00')
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


    def test_sela_minimal_reply_payload_is_accepted(self):
        # This is the exact minimal payload sela's agent_session_sync_replies
        # builds: flat reply + action.event only, no intent/route/next_task.
        outbound = outbound_payload('candidate-minimal-reply')
        first = self.post_outbound(outbound)
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        payload = {
            'candidate_id': outbound['candidate_id'],
            'reply': {
                'from': 'Ana Silva <ana@acrilicos.com>',
                'subject': 'Re: Acrylic sheet supply',
                'body': 'Thanks, let us talk next week.',
                'received_at': 'Mon, 17 Aug 2026 09:00:00 +0800',
                'message_id': 'gmail-minimal-1',
            },
            'action': {'event': 'REPLIED'},
            'idempotency_key': 'sela:gmail-reply:gmail-minimal-1',
        }
        response = self.client.post(
            '/api/integrations/sela/reply',
            json=payload,
            headers=self.headers(payload['idempotency_key']),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(body['status'], 'SYNCED')
        self.assertIsNotNone(body['activity_id'])

        # The handoff makes the prospect read as engaged, so sela's send
        # policy (relationship_stage in engaged_lead/qualified_opportunity)
        # refuses any further outreach.
        listed = self.client.get(
            '/api/integrations/sela/prospects?limit=100', headers=self.headers()
        )
        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        rows = {row['id']: row for row in listed.get_json()['prospects']}
        view = rows[outbound['candidate_id']]
        self.assertEqual(view['lifecycle_stage'], 'engaged_lead')
        self.assertTrue(view['customer_linked'])

    def _post_bounce(self, candidate_id, message_id='gmail-bounce-1'):
        payload = {
            'candidate_id': candidate_id,
            'reply': {
                'from': 'Mail Delivery Subsystem <mailer-daemon@googlemail.com>',
                'subject': 'Delivery Status Notification (Failure)',
                'body': 'Delivery to ana@acrilicos.com failed permanently.',
                'received_at': '2026-09-10T09:00:00+08:00',
                'message_id': message_id,
            },
            'action': {'event': 'BOUNCED'},
            'idempotency_key': f'sela:gmail-bounce:{message_id}',
        }
        response = self.client.post(
            '/api/integrations/sela/reply',
            json=payload,
            headers=self.headers(payload['idempotency_key']),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['status'], 'SYNCED')
        return response

    def test_reply_body_is_summarised_by_the_model_before_it_is_recorded(self):
        outbound = outbound_payload('candidate-summary')
        first = self.post_outbound(outbound)
        trosa_id = first.get_json()['trosa_id']
        self.module.quick_chat = lambda prompt, *a, **k: '客户想先看规格书，并询问最小起订量。'
        body = ('Tue, 25 Aug 2026 09:11:31 -0400 · info@acme.com Good day, please share the specs. '
                'Notice of Confidentiality: The information contained in this communication is intended solely for you.')
        reply = {
            'candidate_id': outbound['candidate_id'], 'trosa_id': trosa_id,
            'reply': {'message_id': 'gmail-reply-sum', 'from': 'info@acme.com', 'subject': 'Re: Quote',
                      'received_at': 'Tue, 25 Aug 2026 09:11:31 -0400', 'body': body},
            'action': {'name': 'REPLIED', 'route': 'REVIEW', 'intent': 'UNKNOWN', 'event': 'REPLIED'},
            'idempotency_key': 'sela-reply:summary-1',
        }
        response = self.client.post('/api/integrations/sela/reply', json=reply,
                                    headers=self.headers(reply['idempotency_key']))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = self.hamid_db()
        try:
            content = conn.execute(
                'SELECT content FROM follow_up_logs WHERE customer_id=? ORDER BY id DESC LIMIT 1',
                (trosa_id,)).fetchone()['content']
        finally:
            conn.close()
        self.assertEqual(content, '客户回复了邮件：客户想先看规格书，并询问最小起订量。')
        self.assertNotIn('Confidentiality', content)

    def test_bounce_is_not_an_inbound_customer_reply(self):
        # Regression (E2): a bounce must not be written to the timeline as an
        # inbound customer reply.
        outbound = outbound_payload('candidate-bounce')
        first = self.post_outbound(outbound)
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        trosa_id = first.get_json()['trosa_id']
        self._post_bounce(outbound['candidate_id'])

        conn = self.hamid_db()
        try:
            outreach = conn.execute(
                'SELECT reply_status FROM outreach_emails'
            ).fetchone()
            self.assertEqual(outreach['reply_status'], 'bounced')
            log = conn.execute(
                '''SELECT direction, activity_type, content FROM follow_up_logs
                   WHERE customer_id=? AND source='sela_reply_engine'
                   ORDER BY id DESC LIMIT 1''',
                (trosa_id,),
            ).fetchone()
            self.assertNotEqual(log['direction'], 'inbound')
            self.assertNotEqual(log['activity_type'], 'customer_reply')
            self.assertNotIn('客户通过 Gmail 回复', log['content'])
        finally:
            conn.close()

    def test_bounce_does_not_engage_the_prospect(self):
        # Regression (E1): a bounce's timestamp/body must not feed the inbound
        # lifecycle signals, so the prospect stays a cold prospect.
        outbound = outbound_payload('candidate-bounce')
        first = self.post_outbound(outbound)
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self._post_bounce(outbound['candidate_id'])

        listed = self.client.get(
            '/api/integrations/sela/prospects?limit=100', headers=self.headers()
        )
        rows = {row['id']: row for row in listed.get_json()['prospects']}
        view = rows[outbound['candidate_id']]
        self.assertEqual(view['outreach_status'], 'BOUNCED')
        self.assertEqual(view['lifecycle_stage'], 'cold_prospect')
        self.assertFalse(view['customer_linked'])


    def test_flat_reply_payload_is_rejected(self):
        # Regression (B): sela's old flat payload is not the reply contract;
        # Trosa requires nested reply + action objects.
        outbound = outbound_payload('candidate-flat')
        first = self.post_outbound(outbound)
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        flat = {
            'candidate_id': outbound['candidate_id'],
            'message_id': 'gmail-flat-1',
            'from': 'Ana Silva <ana@acrilicos.com>',
            'subject': 'Re: Acrylic sheet supply',
            'body': 'Old flat shape.',
            'received_at': 'Mon, 17 Aug 2026 09:00:00 +0800',
            'idempotency_key': 'sela:gmail-reply:gmail-flat-1',
        }
        response = self.client.post(
            '/api/integrations/sela/reply',
            json=flat,
            headers=self.headers(flat['idempotency_key']),
        )
        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
