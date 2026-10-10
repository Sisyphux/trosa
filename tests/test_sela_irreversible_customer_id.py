"""``merge_identity`` / ``overwrite_email`` take a Trosa customer number, not a source id.

Production 2026-10-10 16:11: sela sent ``target_customer_id='agent-nguan-kee-malaysia'`` (a
Sela source id) and ``int()`` raised, so the whole irreversible action answered 500.  A
number that is not a positive integer must be a readable 400; a source id may be resolved to
the prospect's own ``customer_id`` but only for a cold prospect found by exact source id.
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

import db  # noqa: E402


TOKEN = 'test-sela-irreversible-customer-id-token'
PROPOSAL_TEXT = 'Sela 建议把这两个对象合并为同一家公司，并说明了判断依据。'


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_sela_irreversible_customer_id_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


def prospect(source_id, **overrides):
    body = {
        'source_id': source_id,
        'company': f'Irreversible Co {source_id}',
        'country': 'Brazil',
        'website': f'https://{source_id}.example/',
        'business_type': 'Acrylic sheet fabricator',
        'status': 'READY TO CONTACT',
        'confidence': 'HIGH',
        'reason': 'Public fabrication evidence is present.',
        'source_urls': [f'https://{source_id}.example/about'],
        'evidence': [{'type': 'website', 'text': 'Fabricates acrylic displays.',
                      'source_url': f'https://{source_id}.example/about'}],
        'contact': {}, 'email': '', 'outreach_status': '', 'subject': '',
        'email_draft': '', 'gmail_draft_id': '', 'gmail_thread_id': '', 'sent_at': '',
    }
    body.update(overrides)
    return body


class SelaIrreversibleCustomerIdTest(unittest.TestCase):
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
                'enabled': True, 'user': 'hamid'})),
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

    # -- helpers -----------------------------------------------------------
    def headers(self, key):
        return {'Authorization': f'Bearer {TOKEN}', 'X-Idempotency-Key': key}

    def hamid_db(self):
        db.set_db_user('hamid')
        return db.get_db()

    def human(self):
        client = self.module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        return client

    def create_prospect(self, source_id, **overrides):
        key = f'irr:{source_id}:create'
        response = self.client.post(
            '/api/integrations/sela/prospects',
            json={'prospect': prospect(source_id, **overrides), 'idempotency_key': key},
            headers=self.headers(key),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return int(response.get_json()['trosa_id'])

    def prepare(self, source_id, key):
        """A sela proposal followed by a genuine human confirmation (contract §4.4)."""
        create_key = f'{key}:create'
        create = self.client.post(
            '/api/integrations/sela/threads',
            json={'title': f'合并身份 {source_id}', 'text': PROPOSAL_TEXT,
                  'subject': f'prospect:{source_id}', 'idempotency_key': create_key},
            headers=self.headers(create_key),
        )
        self.assertEqual(create.status_code, 200, create.get_data(as_text=True))
        thread = create.get_json()['thread']
        thread_id = thread['id']
        if not create.get_json()['created']:
            append_key = f'{key}:proposal'
            append = self.client.post(
                f'/api/integrations/sela/threads/{thread_id}/messages',
                json={'text': PROPOSAL_TEXT, 'seen_revision': thread['revision'],
                      'idempotency_key': append_key},
                headers=self.headers(append_key),
            )
            self.assertEqual(append.status_code, 200, append.get_data(as_text=True))
            thread = append.get_json()['thread']
        proposal_id = thread['messages'][-1]['id']
        reply = self.human().post(
            f'/api/inbox/threads/{thread_id}/reply',
            json={'text': '确认', 'seen_revision': thread['revision']},
        )
        self.assertEqual(reply.status_code, 200, reply.get_data(as_text=True))
        return thread_id, proposal_id, reply.get_json()['message']['id']

    def irreversible(self, source_id, key, action, arguments):
        thread_id, proposal_id, confirm_id = self.prepare(source_id, key)
        response = self.client.post(
            f'/api/integrations/sela/threads/{thread_id}/irreversible-actions',
            json={'action': action, 'arguments': arguments,
                  'proposal_message_id': proposal_id,
                  'confirmed_by_message_id': confirm_id,
                  'idempotency_key': key},
            headers=self.headers(key),
        )
        return response, confirm_id

    def consumed_confirmation(self, message_id):
        conn = self.hamid_db()
        try:
            row = conn.execute(
                'SELECT 1 FROM inbox_action_receipts WHERE consumed_message_id=? LIMIT 1',
                (message_id,),
            ).fetchone()
            return bool(row)
        finally:
            conn.close()

    def identity_targets(self, source_id):
        conn = self.hamid_db()
        try:
            return [int(row['customer_id']) for row in conn.execute(
                'SELECT customer_id FROM identity_link_facts '
                "WHERE identifier_type='source' AND identifier_value LIKE ? "
                "AND (revoked_at='' OR revoked_at IS NULL)", (f'%{source_id}',),
            ).fetchall()]
        finally:
            conn.close()

    def prospect_email(self, source_id):
        payload = self.client.get('/api/integrations/sela/prospects?limit=100',
                                  headers=self.headers('irr:list')).get_json()
        return next(row for row in payload['prospects'] if row['id'] == source_id)['email']

    # -- merge_identity ----------------------------------------------------
    def test_merge_identity_with_a_source_id_target_is_not_a_server_error(self):
        source_id, other_id = 'irr-merge-src', 'irr-merge-other'
        self.create_prospect(source_id)
        other_customer = self.create_prospect(other_id)
        response, confirm_id = self.irreversible(
            source_id, 'irr:merge:srcid', 'merge_identity',
            {'source_id': source_id, 'target_customer_id': other_id})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.get_json()['result']['applied'])
        self.assertTrue(self.consumed_confirmation(confirm_id))
        self.assertIn(other_customer, self.identity_targets(source_id))

    def test_merge_identity_with_numeric_string_target_still_works(self):
        source_id = 'irr-merge-num'
        self.create_prospect(source_id)
        other_customer = self.create_prospect('irr-merge-num-other')
        response, _confirm = self.irreversible(
            source_id, 'irr:merge:num', 'merge_identity',
            {'source_id': source_id, 'target_customer_id': str(other_customer)})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.get_json()['result']['applied'])

    def test_merge_identity_with_unknown_source_id_target_is_a_readable_400(self):
        source_id = 'irr-merge-unknown'
        self.create_prospect(source_id)
        response, confirm_id = self.irreversible(
            source_id, 'irr:merge:unknown', 'merge_identity',
            {'source_id': source_id, 'target_customer_id': 'agent-nobody-here'})
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        error = response.get_json()['error']
        self.assertEqual(error['code'], 'invalid_request')
        self.assertIn('客户编号', error['message'])
        # a refused action must not burn the human's confirmation
        self.assertFalse(self.consumed_confirmation(confirm_id))

    def test_merge_identity_rejects_non_positive_and_garbage_numbers(self):
        source_id = 'irr-merge-bad'
        self.create_prospect(source_id)
        for index, bad in enumerate((-3, '-3', 'abc def', '12; DROP', 1.5, True, [], {})):
            response, _confirm = self.irreversible(
                source_id, f'irr:merge:bad:{index}', 'merge_identity',
                {'source_id': source_id, 'target_customer_id': bad})
            self.assertEqual(response.status_code, 400, (bad, response.get_data(as_text=True)))
            self.assertEqual(response.get_json()['error']['code'], 'invalid_request')

    def test_merge_identity_numeric_target_that_does_not_exist_is_a_readable_error(self):
        source_id = 'irr-merge-ghost'
        self.create_prospect(source_id)
        response, _confirm = self.irreversible(
            source_id, 'irr:merge:ghost', 'merge_identity',
            {'source_id': source_id, 'target_customer_id': 987654})
        self.assertIn(response.status_code, (400, 404), response.get_data(as_text=True))

    def test_merge_identity_refuses_a_source_id_target_that_is_not_a_cold_prospect(self):
        source_id, other_id = 'irr-merge-warm-src', 'irr-merge-warm-other'
        self.create_prospect(source_id)
        other_customer = self.create_prospect(other_id)
        conn = self.hamid_db()
        try:
            conn.execute("UPDATE customers SET status='成交' WHERE id=?", (other_customer,))
            conn.commit()
        finally:
            conn.close()
        response, confirm_id = self.irreversible(
            source_id, 'irr:merge:warm', 'merge_identity',
            {'source_id': source_id, 'target_customer_id': other_id})
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['error']['code'], 'guardrail_rejected')
        self.assertFalse(self.consumed_confirmation(confirm_id))

    # -- overwrite_email ---------------------------------------------------
    def test_overwrite_email_with_a_source_id_customer_is_not_a_server_error(self):
        source_id = 'irr-over-src'
        self.create_prospect(source_id, email='old@irr-over.example',
                             contact={'email': 'old@irr-over.example'})
        response, confirm_id = self.irreversible(
            source_id, 'irr:over:srcid', 'overwrite_email',
            {'customer_id': source_id, 'email': 'new@irr-over.example'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.prospect_email(source_id), 'new@irr-over.example')
        self.assertTrue(self.consumed_confirmation(confirm_id))

    def test_overwrite_email_with_garbage_customer_is_a_readable_400(self):
        source_id = 'irr-over-bad'
        self.create_prospect(source_id)
        response, confirm_id = self.irreversible(
            source_id, 'irr:over:bad', 'overwrite_email',
            {'customer_id': 'no such customer!', 'email': 'new@irr-over-bad.example'})
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['error']['code'], 'invalid_request')
        self.assertFalse(self.consumed_confirmation(confirm_id))

    def test_overwrite_email_with_numeric_id_still_works(self):
        source_id = 'irr-over-num'
        customer_id = self.create_prospect(source_id, email='old@irr-num.example',
                                           contact={'email': 'old@irr-num.example'})
        response, _confirm = self.irreversible(
            source_id, 'irr:over:num', 'overwrite_email',
            {'customer_id': customer_id, 'email': 'new@irr-num.example'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.prospect_email(source_id), 'new@irr-num.example')


if __name__ == '__main__':
    unittest.main()
