import base64
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db
import gmail_sync
import scheduler


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self.payload


class GmailSyncTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        self.env = {
            'GMAIL_CLIENT_ID': 'gmail-client',
            'GMAIL_CLIENT_SECRET': 'gmail-secret',
            'GMAIL_REDIRECT_URI': 'https://crm.example.test/api/integrations/gmail/oauth/callback',
            'GMAIL_TOKEN_ENCRYPTION_KEY': 'gmail-test-encryption-secret-at-least-32-chars',
            'GMAIL_SYNC_ENABLED': 'true',
            'GMAIL_INITIAL_MAX_MESSAGES': '20',
            'GMAIL_AI_SUMMARIZE': 'false',
        }
        self.environment = mock.patch.dict(os.environ, self.env, clear=False)
        self.environment.start()
        db.DB_DIR = self.tempdir.name
        db.init_all_dbs()
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company) VALUES ('Mina', 'Buyer Co.')")
            self.customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("""INSERT INTO contacts (customer_id, name, email, is_primary)
                            VALUES (?, 'Mina Buyer', 'buyer@example.com', 1)""", (self.customer_id,))
            self.contact_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        gmail_sync._save_record('hamid', {
            'refresh_token': gmail_sync._encrypt_refresh_token('refresh-token'),
            'email': 'owner@example.com', 'status': 'connected',
        })

    def tearDown(self):
        db.DB_DIR = self.original_db_dir
        self.environment.stop()
        self.tempdir.cleanup()

    @staticmethod
    def _raw_message(message_id='gmail-message-1', email='buyer@example.com'):
        text = base64.urlsafe_b64encode('Please confirm the quotation details.'.encode()).decode().rstrip('=')
        return {
            'id': message_id, 'threadId': 'thread-1', 'historyId': '100',
            'internalDate': '1780000000000', 'snippet': 'Please confirm the quotation details.',
            'payload': {
                'mimeType': 'multipart/alternative',
                'headers': [
                    {'name': 'From', 'value': 'Buyer <' + email + '>'},
                    {'name': 'To', 'value': 'Owner <owner@example.com>'},
                    {'name': 'Subject', 'value': 'Quotation details'},
                ],
                'parts': [{'mimeType': 'text/plain', 'body': {'data': text}}],
            },
        }

    def _gmail_request(self, method, url, **kwargs):
        if url == gmail_sync.GOOGLE_TOKEN_URL:
            return FakeResponse({'access_token': 'access-token'})
        if url.endswith('/profile'):
            return FakeResponse({'emailAddress': 'owner@example.com', 'historyId': '100'})
        if url.endswith('/messages'):
            return FakeResponse({'messages': [{'id': 'gmail-message-1'}]})
        if url.endswith('/history'):
            return FakeResponse({'historyId': '101', 'history': [
                {'messagesAdded': [{'message': {'id': 'gmail-message-1'}}]},
            ]})
        if url.endswith('/messages/gmail-message-1'):
            return FakeResponse(self._raw_message())
        raise AssertionError('Unexpected Gmail request: ' + method + ' ' + url)

    def test_initial_sync_exact_match_creates_one_auditable_timeline_item(self):
        with mock.patch.object(gmail_sync.requests, 'request', side_effect=self._gmail_request):
            first = gmail_sync.sync_gmail_user('hamid')
            second = gmail_sync.sync_gmail_user('hamid')

        self.assertEqual(first['status'], 'completed')
        self.assertEqual(first['matched'], 1)
        self.assertEqual(second['duplicate'], 1)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            activity = conn.execute('''SELECT customer_id, contact_id, source, activity_type, direction
                                       FROM follow_up_logs''').fetchone()
            self.assertEqual(tuple(activity), (self.customer_id, self.contact_id, 'gmail', 'email', 'inbound'))
            self.assertEqual(conn.execute("SELECT channel FROM communication_sources").fetchone()[0], 'gmail')
            self.assertEqual(conn.execute("SELECT source_fingerprint FROM communication_source_items").fetchone()[0],
                             'gmail:owner@example.com:gmail-message-1')
            self.assertEqual(conn.execute("SELECT match_status FROM gmail_message_states").fetchone()[0], 'matched')
        finally:
            conn.close()

    def test_unmatched_message_stays_in_inbox_without_creating_customer_or_task(self):
        message = gmail_sync.normalize_gmail_message(self._raw_message('unmatched-1', 'new@example.com'), 'owner@example.com')
        result = gmail_sync._store_message('hamid', 'owner@example.com', message, '新联系人来信')
        self.assertEqual(result['state'], 'unmatched')
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 0)
            inbox = conn.execute("SELECT item_type, customer_id, status FROM inbox_items").fetchone()
            self.assertEqual(tuple(inbox), ('gmail_capture', None, 'open'))
            self.assertEqual(conn.execute("SELECT match_status FROM gmail_message_states").fetchone()[0], 'unmatched')
        finally:
            conn.close()

    def test_same_email_on_multiple_customers_requires_inbox_confirmation(self):
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company) VALUES ('Other', 'Other Buyer Co.')")
            other_customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("""INSERT INTO contacts (customer_id, name, email)
                            VALUES (?, 'Other Buyer', 'shared@example.com')""", (other_customer_id,))
            conn.execute("""INSERT INTO contacts (customer_id, name, email)
                            VALUES (?, 'Original Buyer', 'shared@example.com')""", (self.customer_id,))
            conn.commit()
        finally:
            conn.close()
        message = gmail_sync.normalize_gmail_message(self._raw_message('ambiguous-1', 'shared@example.com'),
                                                     'owner@example.com')
        result = gmail_sync._store_message('hamid', 'owner@example.com', message, '冲突邮箱来信')
        self.assertEqual(result['state'], 'ambiguous')
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM follow_up_logs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT customer_id FROM inbox_items").fetchone()[0], None)
            self.assertEqual(conn.execute("SELECT match_status FROM gmail_message_states").fetchone()[0], 'ambiguous')
        finally:
            conn.close()

    def test_confirming_unmatched_gmail_capture_attaches_original_source(self):
        message = gmail_sync.normalize_gmail_message(self._raw_message('unmatched-confirm-1', 'new@example.com'), 'owner@example.com')
        stored = gmail_sync._store_message('hamid', 'owner@example.com', message, '新联系人来信')
        spec = importlib.util.spec_from_file_location('crm_gmail_capture_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        client = module.app.test_client()
        self.assertEqual(client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)
        response = client.post(f'/api/customers/{self.customer_id}/follow_history', json={
            'activity_content': '客户来信询问报价细节', 'activity_type': 'email', 'direction': 'inbound',
            'follow_date': '2026-05-28', 'inbox_item_id': stored['inbox_item_id'], 'source': 'gmail',
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            state = conn.execute('SELECT match_status, customer_id, activity_id FROM gmail_message_states').fetchone()
            self.assertEqual(tuple(state[:2]), ('matched', self.customer_id))
            self.assertTrue(state[2])
            self.assertEqual(conn.execute("SELECT channel FROM communication_sources").fetchone()[0], 'gmail')
            self.assertEqual(conn.execute("SELECT status FROM inbox_items WHERE id=?", (stored['inbox_item_id'],)).fetchone()[0],
                             'resolved')
        finally:
            conn.close()

    def test_scheduler_registers_optional_gmail_incremental_worker_when_configured(self):
        fake_scheduler = mock.Mock()
        fake_scheduler.running = False
        previous_scheduler = scheduler.scheduler
        try:
            with mock.patch.object(scheduler, 'BackgroundScheduler', return_value=fake_scheduler):
                scheduler.scheduler = None
                scheduler.start_scheduler()
            job_ids = [call.kwargs['id'] for call in fake_scheduler.add_job.call_args_list]
            self.assertIn('gmail_sync_worker', job_ids)
            gmail_job = next(call for call in fake_scheduler.add_job.call_args_list
                             if call.kwargs['id'] == 'gmail_sync_worker')
            self.assertEqual(gmail_job.kwargs['seconds'], 300)
        finally:
            scheduler.scheduler = previous_scheduler
