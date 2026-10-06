"""Sela may stop contact with a COLD prospect only through the §4.4A entry.

Since contract v1.3 the block-direction write is reachable only through
``POST /api/integrations/sela/threads/<id>/irreversible-actions`` with a real
human confirmation message, and the legacy
``POST /api/integrations/sela/prospects/<source_id>/contact-block`` route is no
longer on the sela service-token allowlist.  The business function itself
(cold-only, add-only, audited) is retained for the new entry.
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


TOKEN = 'test-sela-contact-block-service-token'
DEFAULT_REASON = 'Competitor: sells acrylic sheet itself (https://x.example/about)'
PROPOSAL_TEXT = 'Sela 建议停止联系这家公司，并说明了判断依据与来源。'


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_sela_contact_block_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


def prospect(source_id, **overrides):
    body = {
        'source_id': source_id,
        'company': f'Block Co {source_id}',
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


class SelaContactBlockTest(unittest.TestCase):
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
                'source_id': source_id, 'company': f'Block Co {source_id}', 'kind': 'DECISION',
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

    # -- tests -------------------------------------------------------------
    def test_service_token_cannot_reach_legacy_contact_block(self):
        source_id = 'block-legacy-auth-1'
        self.create_prospect(source_id)
        legacy = self.client.post(
            f'/api/integrations/sela/prospects/{source_id}/contact-block',
            json={'reason': DEFAULT_REASON, 'idempotency_key': 'block:legacy-auth'},
            headers=self.headers('block:legacy-auth'),
        )
        self.assertEqual(legacy.status_code, 401, legacy.get_data(as_text=True))
        anonymous = self.module.app.test_client().post(
            f'/api/integrations/sela/prospects/{source_id}/contact-block',
            json={'reason': DEFAULT_REASON, 'idempotency_key': 'block:legacy-anon'},
            headers={'X-Idempotency-Key': 'block:legacy-anon'},
        )
        self.assertEqual(anonymous.status_code, 401)
        self.assertFalse(self.view(source_id)['do_not_contact'])

    def test_service_identity_still_cannot_unblock(self):
        source_id = 'block-unblock-1'
        customer_id = self.create_prospect(source_id)
        self.assertEqual(self.block(source_id, key='block:unblock').status_code, 200)
        unblock = self.client.post(
            f'/api/customers/{customer_id}/agent-prospect/contact-permission',
            json={'permission': 'allowed', 'note': 'sela wants to unblock'},
            headers=self.headers('block:unblock-permission'),
        )
        self.assertIn(unblock.status_code, (401, 403), unblock.get_data(as_text=True))
        self.assertTrue(self.view(source_id)['do_not_contact'])

    def test_cold_prospect_is_blocked_with_audit_and_inbox_decision_resolved(self):
        source_id = 'block-cold-1'
        customer_id = self.create_prospect(source_id)
        decision_id = self.create_exclusion_decision(source_id)
        self.assertEqual(self.inbox_status(decision_id)['status'], 'open')

        response = self.block(source_id, reason='Competitor, sells acrylic sheet (https://x.example/about)')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(body['action'], 'stop_contact')
        result = body['result']
        self.assertEqual(result['status'], 'SYNCED')
        self.assertFalse(result['already_blocked'])
        self.assertTrue(result['prospect']['do_not_contact'])
        self.assertEqual(result['prospect']['exclusion_resolution'], 'AGENT_BLOCKED_NON_FIT')
        self.assertIn('Competitor', result['prospect']['do_not_contact_reason'])
        self.assertEqual(result['resolved_inbox_ids'], [decision_id])

        conn = self.hamid_db()
        try:
            profile = conn.execute(
                'SELECT contact_permission, suppression_reason, research_json FROM agent_prospect_profiles '
                'WHERE source_id=?', (source_id,),
            ).fetchone()
            self.assertEqual(profile['contact_permission'], 'do_not_contact')
            state = json.loads(profile['research_json'])['agent_state']
            self.assertEqual(state['exclusion_resolution'], 'AGENT_BLOCKED_NON_FIT')
            change = state['contact_permission_changes'][-1]
            self.assertEqual((change['actor'], change['from'], change['to']),
                             ('sela', 'allowed', 'do_not_contact'))
            self.assertIn('Competitor', change['note'])
            # Resolved as an agent decision with no customer timeline noise.
            inbox = self.inbox_status(decision_id)
            self.assertEqual(inbox['status'], 'resolved')
            self.assertEqual((inbox['resolution_source'], inbox['resolved_by']), ('agent', 'sela'))
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) AS n FROM follow_up_logs WHERE customer_id=?', (customer_id,),
            ).fetchone()['n'], 0)
        finally:
            conn.close()
        records = self.client.get('/api/integrations/sela/exclusions', headers=self.headers()).get_json()['records']
        self.assertTrue(any(record.get('source_id') == source_id for record in records))
        self.assertFalse(any(str(item.get('candidate_id') or '') == source_id for item in self.open_needs()))

    def test_legacy_decision_without_standard_dedupe_key_is_resolved_by_source(self):
        source_id = 'block-legacy-1'
        self.create_prospect(source_id)
        decision_id = self.create_exclusion_decision(source_id, key='sela:legacy-exclusion-ask-1')
        response = self.block(source_id)
        self.assertEqual(response.get_json()['result']['resolved_inbox_ids'], [decision_id])
        self.assertEqual(self.inbox_status(decision_id)['status'], 'resolved')

    def test_engaged_and_customer_prospects_are_refused_with_guardrail(self):
        engaged_id = 'block-engaged-1'
        self.create_prospect(engaged_id)
        reply = self.client.post(
            '/api/integrations/sela/reply',
            json={'candidate_id': engaged_id,
                  'reply': {'from': 'Ana <ana@engaged.example>', 'subject': 'Re: hello',
                            'body': 'Thanks, let us talk next week.',
                            'received_at': 'Mon, 17 Aug 2026 09:00:00 +0800',
                            'message_id': 'block-engaged-msg-1'},
                  'action': {'event': 'REPLIED'},
                  'idempotency_key': 'block:engaged:reply'},
            headers=self.headers('block:engaged:reply'),
        )
        self.assertEqual(reply.status_code, 200, reply.get_data(as_text=True))
        self.assertEqual(self.view(engaged_id)['lifecycle_stage'], 'engaged_lead')

        won_source = 'block-won-1'
        won_customer = self.create_prospect(won_source)
        conn = self.hamid_db()
        try:
            conn.execute("UPDATE customers SET business_stage='成交' WHERE id=?", (won_customer,))
            conn.commit()
        finally:
            conn.close()
        decision_id = self.create_exclusion_decision(won_source)

        for source_id, key in ((engaged_id, 'block:engaged'), (won_source, 'block:won')):
            response = self.block(source_id, key=key)
            self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
            self.assertEqual(response.get_json()['error']['code'], 'guardrail_rejected')
            self.assertIn('Trosa', response.get_json()['error']['message'])
            self.assertFalse(self.view(source_id)['do_not_contact'])
        # The refused request leaves the human's question open.
        self.assertEqual(self.inbox_status(decision_id)['status'], 'open')

    def test_guardrail_rejection_does_not_consume_confirmation(self):
        source_id = 'block-notcold-consumed-1'
        self.create_prospect(source_id)
        # Make it non-cold so the guardrail refuses the write.
        self.client.post(
            '/api/integrations/sela/reply',
            json={'candidate_id': source_id,
                  'reply': {'from': 'Ana <ana@x.example>', 'subject': 'Re: hi',
                            'body': 'Interested.', 'received_at': 'Mon, 17 Aug 2026 09:00:00 +0800',
                            'message_id': 'block-notcold-msg-1'},
                  'action': {'event': 'REPLIED'},
                  'idempotency_key': 'block:notcold:reply'},
            headers=self.headers('block:notcold:reply'),
        )
        thread_id, proposal_id, confirm_id = self.prepare(source_id, 'block:notcold')
        refused = self.stop(thread_id, source_id, 'block:notcold', proposal_id, confirm_id)
        self.assertEqual(refused.status_code, 409, refused.get_data(as_text=True))
        self.assertEqual(refused.get_json()['error']['code'], 'guardrail_rejected')
        # The confirmation was NOT consumed, so the same ids can be retried.
        self.assertFalse(self.consumed_confirmation(confirm_id))

    def test_missing_confirmation_ids_are_refused(self):
        source_id = 'block-missing-conf-1'
        self.create_prospect(source_id)
        thread_id, _, _ = self.prepare(source_id, 'block:missing-conf')
        response = self.client.post(
            f'/api/integrations/sela/threads/{thread_id}/irreversible-actions',
            json={'action': 'stop_contact', 'arguments': {'source_id': source_id, 'reason': DEFAULT_REASON},
                  'idempotency_key': 'block:missing-conf'},
            headers=self.headers('block:missing-conf'),
        )
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['error']['code'], 'confirmation_required')
        self.assertEqual(response.get_json()['error']['details']['reason'], 'missing_confirmation_ids')

    def test_confirmation_earlier_than_proposal_is_refused(self):
        source_id = 'block-early-1'
        self.create_prospect(source_id)
        create_key = 'block:early:create'
        create = self.client.post(
            '/api/integrations/sela/threads',
            json={'title': 'x', 'text': PROPOSAL_TEXT, 'subject': f'prospect:{source_id}',
                  'idempotency_key': create_key},
            headers=self.headers(create_key),
        )
        thread = create.get_json()['thread']
        thread_id = thread['id']
        human_reply = self.human().post(
            f'/api/inbox/threads/{thread_id}/reply',
            json={'text': '先回复', 'seen_revision': thread['revision']},
        )
        self.assertEqual(human_reply.status_code, 200)
        early_confirm = human_reply.get_json()['message']['id']
        append_key = 'block:early:proposal'
        appended = self.client.post(
            f'/api/integrations/sela/threads/{thread_id}/messages',
            json={'text': PROPOSAL_TEXT, 'seen_revision': 2, 'idempotency_key': append_key},
            headers=self.headers(append_key),
        )
        proposal_id = appended.get_json()['thread']['messages'][-1]['id']
        response = self.stop(thread_id, source_id, 'block:early', proposal_id, early_confirm)
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['error']['details']['reason'],
                         'confirmation_not_later_than_proposal')
        self.assertFalse(self.view(source_id)['do_not_contact'])

    def test_reason_is_required(self):
        source_id = 'block-reason-1'
        self.create_prospect(source_id)
        for reason in ('', '   '):
            thread_id, proposal_id, confirm_id = self.prepare(source_id, f'block:noreason:{len(reason)}')
            response = self.stop(thread_id, source_id, f'block:noreason:{len(reason)}',
                                 proposal_id, confirm_id, reason=reason)
            self.assertEqual(response.status_code, 400, (reason, response.get_data(as_text=True)))
            self.assertEqual(response.get_json()['error']['code'], 'invalid_request')
        self.assertFalse(self.view(source_id)['do_not_contact'])

    def test_unknown_prospect_is_404(self):
        response = self.block('block-does-not-exist')
        self.assertEqual(response.status_code, 404, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['error']['code'], 'not_found')

    def test_repeat_call_is_idempotent_and_conflicting_key_reuse_is_rejected(self):
        source_id = 'block-repeat-1'
        self.create_prospect(source_id)
        thread_id, proposal_id, confirm_id = self.prepare(source_id, 'block:repeat')
        first = self.stop(thread_id, source_id, 'block:repeat', proposal_id, confirm_id)
        replay = self.stop(thread_id, source_id, 'block:repeat', proposal_id, confirm_id)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(first.get_json(), replay.get_json())
        # Same key with a different reason is a different request.
        clash = self.stop(thread_id, source_id, 'block:repeat', proposal_id, confirm_id,
                          reason='A completely different reason')
        self.assertEqual(clash.status_code, 409)
        self.assertEqual(clash.get_json()['error']['code'], 'idempotency_conflict')
        # A fresh confirmation + key on an already-blocked prospect succeeds with
        # no second audit row.
        thread2, proposal2, confirm2 = self.prepare(source_id, 'block:repeat-new-key')
        again = self.stop(thread2, source_id, 'block:repeat-new-key', proposal2, confirm2)
        self.assertEqual(again.status_code, 200, again.get_data(as_text=True))
        self.assertTrue(again.get_json()['result']['already_blocked'])
        conn = self.hamid_db()
        try:
            research = json.loads(conn.execute(
                'SELECT research_json FROM agent_prospect_profiles WHERE source_id=?', (source_id,),
            ).fetchone()['research_json'])
        finally:
            conn.close()
        self.assertEqual(len(research['agent_state']['contact_permission_changes']), 1)

    def test_other_prospects_exclusion_inbox_item_is_untouched(self):
        blocked, other = 'block-target-1', 'block-other-1'
        self.create_prospect(blocked)
        self.create_prospect(other)
        other_decision = self.create_exclusion_decision(other)
        blocked_decision = self.create_exclusion_decision(blocked)
        response = self.block(blocked)
        self.assertEqual(response.get_json()['result']['resolved_inbox_ids'], [blocked_decision])
        self.assertEqual(self.inbox_status(blocked_decision)['status'], 'resolved')
        self.assertEqual(self.inbox_status(other_decision)['status'], 'open')
        self.assertFalse(self.view(other)['do_not_contact'])

    def test_already_blocked_prospect_returns_view_and_closes_stale_question(self):
        source_id = 'block-already-1'
        self.create_prospect(source_id)
        self.assertEqual(self.block(source_id, key='block:first').status_code, 200)
        stale = self.create_exclusion_decision(source_id, key='sela:decision:exclusion:' + source_id + ':again')
        # Different dedupe key but same source and resume_action: still its question.
        response = self.block(source_id, key='block:second')
        body = response.get_json()['result']
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body['already_blocked'])
        self.assertTrue(body['prospect']['do_not_contact'])
        self.assertEqual(body['resolved_inbox_ids'], [stale])

    def test_human_reply_returns_null_undo_token(self):
        source_id = 'block-undo-1'
        self.create_prospect(source_id)
        create_key = 'block:undo:create'
        create = self.client.post(
            '/api/integrations/sela/threads',
            json={'title': 'x', 'text': PROPOSAL_TEXT, 'subject': f'prospect:{source_id}',
                  'idempotency_key': create_key},
            headers=self.headers(create_key),
        )
        thread_id = create.get_json()['thread']['id']
        reply = self.human().post(
            f'/api/inbox/threads/{thread_id}/reply',
            json={'text': '人回复', 'seen_revision': 1},
        )
        self.assertEqual(reply.status_code, 200)
        self.assertIsNone(reply.get_json()['undo_token'])


if __name__ == '__main__':
    unittest.main()
