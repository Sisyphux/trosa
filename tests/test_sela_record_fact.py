"""Reversible facts the human gave in a dialogue (contract §4.4B, ``/threads/<id>/facts``).

The cited message must be a human message of the thread, values must appear verbatim in it,
and only cold prospects are written.  No confirmation is consumed.
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


TOKEN = 'test-sela-record-fact-service-token'
DEFAULT_REASON = 'Competitor: sells acrylic sheet itself (https://x.example/about)'
PROPOSAL_TEXT = 'Sela 建议停止联系这家公司，并说明了判断依据与来源。'


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_sela_record_fact_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


def prospect(source_id, **overrides):
    body = {
        'source_id': source_id,
        'company': f'Fact Co {source_id}',
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


class SelaRecordFactTest(unittest.TestCase):
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
    def headers(self, key='block:default'):
        return {'Authorization': f'Bearer {TOKEN}', 'X-Idempotency-Key': key}

    def hamid_db(self):
        db.set_db_user('hamid')
        return db.get_db()

    def human(self):
        client = self.module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        return client

    def create_prospect(self, source_id, **overrides):
        key = f'block:{source_id}:create'
        response = self.client.post(
            '/api/integrations/sela/prospects',
            json={'prospect': prospect(source_id, **overrides), 'idempotency_key': key},
            headers=self.headers(key),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return int(response.get_json()['trosa_id'])

    def create_exclusion_decision(self, source_id, key=None):
        key = key or f'sela:decision:exclusion:{source_id}'
        response = self.client.post(
            '/api/integrations/sela/needs',
            json={'request': {
                'source_id': source_id, 'company': f'Fact Co {source_id}', 'kind': 'DECISION',
                'severity': 'AMBER', 'need': '是否把该公司加入排除？',
                'decision': {'question': '是否加入排除 / DNC？', 'options': [], 'recommended': ''},
                'resume_action': 'resolve_exclusion', 'resume_decision': 'exclude',
                'dedupe_key': key,
            }, 'idempotency_key': key},
            headers=self.headers(key),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return int(response.get_json()['item']['trosa_inbox_id'])

    def prepare(self, source_id, key):
        """Open a thread with a sela proposal plus a human confirmation.

        Returns ``(thread_id, proposal_message_id, confirmed_by_message_id)``.
        The confirmation is a genuine human message with a greater seq, exactly
        as contract §4.4 requires.
        """
        create_key = f'{key}:create'
        create = self.client.post(
            '/api/integrations/sela/threads',
            json={'title': f'停止联系 {source_id}', 'text': PROPOSAL_TEXT,
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
            json={'text': '确认停止联系', 'seen_revision': thread['revision']},
        )
        self.assertEqual(reply.status_code, 200, reply.get_data(as_text=True))
        return thread_id, proposal_id, reply.get_json()['message']['id']

    def stop(self, thread_id, source_id, key, proposal_id, confirm_id,
             reason=DEFAULT_REASON):
        return self.client.post(
            f'/api/integrations/sela/threads/{thread_id}/irreversible-actions',
            json={'action': 'stop_contact',
                  'arguments': {'source_id': source_id, 'reason': reason},
                  'proposal_message_id': proposal_id,
                  'confirmed_by_message_id': confirm_id,
                  'idempotency_key': key},
            headers=self.headers(key),
        )

    def block(self, source_id, reason=DEFAULT_REASON, key='block:one'):
        thread_id, proposal_id, confirm_id = self.prepare(source_id, key)
        return self.stop(thread_id, source_id, key, proposal_id, confirm_id, reason)

    def view(self, source_id):
        payload = self.client.get('/api/integrations/sela/prospects?limit=100', headers=self.headers()).get_json()
        return next(row for row in payload['prospects'] if row['id'] == source_id)

    def inbox_status(self, inbox_id):
        conn = self.hamid_db()
        try:
            row = conn.execute(
                'SELECT status, resolution_source, resolved_by FROM inbox_items WHERE id=?', (inbox_id,),
            ).fetchone()
            return dict(row)
        finally:
            conn.close()

    def open_needs(self):
        return self.client.get('/api/integrations/sela/needs?status=open', headers=self.headers()).get_json()['needs']

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

    # -- fact helpers ------------------------------------------------------
    def thread_with_human(self, source_id, human_text, key):
        create_key = f'{key}:create'
        create = self.client.post(
            '/api/integrations/sela/threads',
            json={'title': f'事实 {source_id}', 'text': '这家我找不到联系方式，你知道吗？',
                  'subject': f'prospect:{source_id}', 'idempotency_key': create_key},
            headers=self.headers(create_key))
        self.assertEqual(create.status_code, 200, create.get_data(as_text=True))
        thread = create.get_json()['thread']
        reply = self.human().post(f'/api/inbox/threads/{thread["id"]}/reply',
                                  json={'text': human_text, 'seen_revision': thread['revision']})
        self.assertEqual(reply.status_code, 200, reply.get_data(as_text=True))
        return thread['id'], reply.get_json()['message']['id']

    def fact(self, thread_id, fact, arguments, message_id, key):
        return self.client.post(
            f'/api/integrations/sela/threads/{thread_id}/facts',
            json={'fact': fact, 'arguments': arguments, 'source_message_id': message_id,
                  'idempotency_key': key},
            headers=self.headers(key))

    def prospect_view(self, source_id):
        payload = self.client.get('/api/integrations/sela/prospects?limit=100',
                                  headers=self.headers()).get_json()
        return next(row for row in payload['prospects'] if row['id'] == source_id)

    # -- tests -------------------------------------------------------------
    def test_email_the_human_gave_is_recorded_on_the_cold_prospect(self):
        source_id = 'fact-email-1'
        self.create_prospect(source_id)
        thread_id, message_id = self.thread_with_human(source_id, '邮箱是 Sales@Fact-Email.example，直接用', 'f:email')
        response = self.fact(thread_id, 'contact_email', {'source_id': source_id, 'email': 'sales@fact-email.example'},
                             message_id, 'f:email:1')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.get_json()['success'])
        self.assertEqual(self.prospect_view(source_id)['email'], 'sales@fact-email.example')
        # the same message may be cited again (nothing is consumed)
        again = self.fact(thread_id, 'contact_email', {'source_id': source_id, 'email': 'sales@fact-email.example'},
                          message_id, 'f:email:2')
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.get_json()['already_present'])
        self.assertFalse(self.consumed_confirmation(message_id))

    def test_value_missing_from_the_human_message_is_refused(self):
        source_id = 'fact-prov-1'
        self.create_prospect(source_id)
        thread_id, message_id = self.thread_with_human(source_id, '邮箱我晚点发给你', 'f:prov')
        response = self.fact(thread_id, 'contact_email', {'source_id': source_id, 'email': 'made.up@fact-prov.example'},
                             message_id, 'f:prov:1')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['error']['code'], 'provenance_mismatch')
        self.assertEqual(self.prospect_view(source_id)['email'], '')

    def test_source_message_must_be_a_human_message_of_this_thread(self):
        source_id = 'fact-src-1'
        self.create_prospect(source_id)
        thread_id, _human_id = self.thread_with_human(source_id, 'a@fact-src.example', 'f:src')
        sela_message_id = self.client.get(f'/api/integrations/sela/threads/{thread_id}',
                                          headers=self.headers()).get_json()['thread']['messages'][0]['id']
        response = self.fact(thread_id, 'contact_email', {'source_id': source_id, 'email': 'a@fact-src.example'},
                             sela_message_id, 'f:src:1')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['error']['code'], 'provenance_mismatch')

    def test_existing_email_is_not_overwritten_here(self):
        source_id = 'fact-over-1'
        self.create_prospect(source_id, email='old@fact-over.example',
                             contact={'email': 'old@fact-over.example'})
        thread_id, message_id = self.thread_with_human(source_id, '换成 new@fact-over.example', 'f:over')
        response = self.fact(thread_id, 'contact_email', {'source_id': source_id, 'email': 'new@fact-over.example'},
                             message_id, 'f:over:1')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['error']['code'], 'guardrail_rejected')
        self.assertEqual(self.prospect_view(source_id)['email'], 'old@fact-over.example')

    def test_invalid_email_and_unknown_prospect(self):
        source_id = 'fact-bad-1'
        self.create_prospect(source_id)
        thread_id, message_id = self.thread_with_human(source_id, '邮箱 not-an-email 还有 x@y.example', 'f:bad')
        bad = self.fact(thread_id, 'contact_email', {'source_id': source_id, 'email': 'not-an-email'},
                        message_id, 'f:bad:1')
        self.assertEqual(bad.status_code, 400)
        missing = self.fact(thread_id, 'contact_email', {'source_id': 'no-such-prospect', 'email': 'x@y.example'},
                            message_id, 'f:bad:2')
        self.assertEqual(missing.status_code, 404)

    def test_person_phone_and_note(self):
        source_id = 'fact-more-1'
        self.create_prospect(source_id)
        thread_id, message_id = self.thread_with_human(
            source_id, '联系人 Ada Lin，电话 +86 138-0000-1234。先不排除，问问他们要不要做第二供应商', 'f:more')
        person = self.fact(thread_id, 'contact_person', {'source_id': source_id, 'name': 'Ada Lin', 'title': 'Buyer'},
                           message_id, 'f:more:person')
        self.assertEqual(person.status_code, 200, person.get_data(as_text=True))
        phone = self.fact(thread_id, 'contact_phone', {'source_id': source_id, 'phone': '8613800001234'},
                          message_id, 'f:more:phone')
        self.assertEqual(phone.status_code, 200, phone.get_data(as_text=True))
        wrong = self.fact(thread_id, 'contact_phone', {'source_id': source_id, 'phone': '99999999'},
                          message_id, 'f:more:wrong')
        self.assertEqual(wrong.status_code, 409)
        note = self.fact(thread_id, 'note',
                         {'source_id': source_id, 'text': '不排除；首触问对方是否需要第二供应商或只作价格参考'},
                         message_id, 'f:more:note')
        self.assertEqual(note.status_code, 200, note.get_data(as_text=True))
        view = self.prospect_view(source_id)
        self.assertEqual(view['contact_details']['name'], 'Ada Lin')
        self.assertIn('1380000', ''.join(ch for ch in view['contact_details']['phone'] if ch.isdigit()))

    def test_note_needs_text(self):
        source_id = 'fact-note-1'
        self.create_prospect(source_id)
        thread_id, message_id = self.thread_with_human(source_id, '随便说一句', 'f:note')
        response = self.fact(thread_id, 'note', {'source_id': source_id, 'text': '  '},
                             message_id, 'f:note:1')
        self.assertEqual(response.status_code, 400)

    def test_different_identity_is_recorded_without_a_profile(self):
        thread_id, message_id = self.thread_with_human('plastic-world-za', '是不同的公司，继续', 'f:diff')
        response = self.fact(thread_id, 'identity_different', {'source_id': 'plastic-world-za'}, message_id, 'f:diff:1')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.get_json()['result']['applied'])

    def test_non_cold_prospect_is_refused(self):
        source_id = 'fact-warm-1'
        customer_id = self.create_prospect(source_id)
        conn = self.hamid_db()
        try:
            conn.execute("UPDATE customers SET status='成交' WHERE id=?", (customer_id,))
            conn.commit()
        finally:
            conn.close()
        thread_id, message_id = self.thread_with_human(source_id, 'a@fact-warm.example', 'f:warm')
        response = self.fact(thread_id, 'contact_email', {'source_id': source_id, 'email': 'a@fact-warm.example'},
                             message_id, 'f:warm:1')
        self.assertIn(response.status_code, (404, 409))
        self.assertEqual(self.prospect_view(source_id)['email'], '')

    def test_route_requires_the_service_token(self):
        thread_id, message_id = self.thread_with_human('fact-auth-1', 'a@b.example', 'f:auth')
        response = self.client.post(f'/api/integrations/sela/threads/{thread_id}/facts',
                                    json={'fact': 'note', 'arguments': {}, 'source_message_id': message_id})
        self.assertEqual(response.status_code, 401)


if __name__ == '__main__':
    unittest.main()
