import copy
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db


TOKEN = 'test-sela-v2-service-token'


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_sela_prospect_api_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


def prospect(source_id='prospect-1'):
    return {
        'source_id': source_id,
        'company': 'Acrílicos S.A.',
        'country': 'Brazil',
        'website': 'https://acrilicos.example/',
        'business_type': 'Acrylic sheet fabricator',
        'campaign': 'Brazil fabricators',
        'source_run': 'run-20260909',
        'status': 'READY TO CONTACT',
        'research_status': 'VERIFIED',
        'confidence': 'HIGH',
        'reason': 'Public fabrication evidence is present.',
        'angle': 'Reliable PMMA sheet supply',
        'source_urls': ['https://acrilicos.example/about', 'https://acrilicos.example/contact'],
        'evidence': [{'type': 'website', 'text': 'Fabricates acrylic displays.',
                      'source_url': 'https://acrilicos.example/about'}],
        'contact': {
            'name': 'Ana Silva', 'title': 'Purchasing', 'email': 'ANA@acrilicos.example',
            'is_primary': 1,
        },
        'email_source_url': 'https://acrilicos.example/contact',
        'email_type': 'person',
        'outreach_status': 'GMAIL_DRAFTED',
        'subject': 'Acrylic sheet supply',
        'email_draft': 'Hello Ana, we can support acrylic sheet sourcing.',
        'gmail_draft_id': 'draft-1',
        'gmail_thread_id': 'thread-1',
    }


class SelaProspectApiTest(unittest.TestCase):
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

    def tearDown(self):
        db.set_db_user(None)
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    def headers(self, key='sela-v2:prospect-1:one'):
        return {'Authorization': f'Bearer {TOKEN}', 'X-Idempotency-Key': key}

    def post_prospect(self, body, key='sela-v2:prospect-1:one'):
        return self.client.post(
            '/api/integrations/sela/prospects',
            json={'prospect': body, 'idempotency_key': key},
            headers=self.headers(key),
        )

    def hamid_db(self):
        db.set_db_user('hamid')
        return db.get_db()

    def test_sela_upsert_conflict_target_matches_sqlite_and_postgres_shapes(self):
        self.assertEqual(
            self.module._sela_conflict_target('legacy_user_id', 'source', 'source_id'),
            '(legacy_user_id, source, source_id)',
        )
        with mock.patch.object(self.module, 'postgres_mode', return_value=True):
            self.assertEqual(
                self.module._sela_conflict_target('legacy_user_id', 'source', 'source_id'),
                '(organization_id, legacy_user_id, source, source_id)',
            )

    def test_upsert_creates_trosa_owned_prospect_before_send_and_is_idempotent(self):
        body = prospect()
        first = self.post_prospect(body)
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        result = first.get_json()
        self.assertEqual(result['status'], 'SYNCED')
        self.assertTrue(result['created'])
        self.assertTrue(result['trosa_id'])

        repeat = self.post_prospect(body)
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat.get_json(), result)

        listed = self.client.get('/api/integrations/sela/prospects', headers=self.headers())
        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        rows = listed.get_json()['prospects']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], 'prospect-1')
        self.assertEqual(rows[0]['trosa_id'], result['trosa_id'])
        self.assertEqual(rows[0]['outreach_status'], 'GMAIL_DRAFTED')
        self.assertEqual(rows[0]['gmail_thread_id'], 'thread-1')
        self.assertEqual(rows[0]['contact'], 'Ana Silva — Purchasing')
        self.assertEqual(rows[0]['contact_details']['title'], 'Purchasing')

        self.client.post('/api/auth/login', json={'user': 'hamid'})
        summary = self.client.get(f"/api/customers/{result['trosa_id']}/summary")
        self.assertEqual(summary.status_code, 200, summary.get_data(as_text=True))
        self.assertEqual(summary.get_json()['agent_prospect']['source_id'], 'prospect-1')
        self.assertEqual(summary.get_json()['agent_prospect']['reason'], 'Public fabrication evidence is present.')

        conn = self.hamid_db()
        try:
            customer = conn.execute('SELECT business_stage, business_role FROM customers').fetchone()
            self.assertEqual(tuple(customer), ('', ''))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM contacts').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM outreach_emails').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM email_delivery_events').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_prospect_profiles').fetchone()[0], 1)
        finally:
            conn.close()

        exported = self.client.get(f"/api/customers/{result['trosa_id']}/context?mode=full")
        self.assertEqual(exported.status_code, 200, exported.get_data(as_text=True))
        self.assertIn('Public fabrication evidence is present.', exported.get_json()['content'])

        changed = copy.deepcopy(body)
        changed['reason'] = 'Different data under the same idempotency key.'
        conflict = self.post_prospect(changed)
        self.assertEqual(conflict.status_code, 409)

    def test_confirmed_gmail_receipt_updates_the_same_trosa_outreach(self):
        created = self.post_prospect(prospect())
        self.assertEqual(created.status_code, 200)
        sent = prospect()
        sent.update({
            'outreach_status': 'SENT',
            'sent_at': '2026-09-09 10:10:00',
            'gmail_message_id': 'message-1',
        })
        response = self.post_prospect(sent, 'sela-v2:prospect-1:sent')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['status'], 'SYNCED')

        conn = self.hamid_db()
        try:
            outreach = conn.execute(
                'SELECT sent_date, reply_status, message_id FROM outreach_emails'
            ).fetchone()
            self.assertEqual(tuple(outreach), ('2026-09-09', 'pending', 'message-1'))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM outreach_emails').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM email_delivery_events').fetchone()[0], 1)
        finally:
            conn.close()

    def test_stale_agent_projection_becomes_review_instead_of_overwriting_trosa(self):
        created = self.post_prospect(prospect())
        self.assertEqual(created.status_code, 200)
        original_revision = created.get_json()['revision']

        current = prospect()
        current['expected_revision'] = original_revision
        current['reason'] = 'Current agent update.'
        updated = self.post_prospect(current, 'sela-v2:prospect-1:current')
        self.assertEqual(updated.status_code, 200, updated.get_data(as_text=True))
        self.assertEqual(updated.get_json()['status'], 'SYNCED')
        self.assertNotEqual(updated.get_json()['revision'], original_revision)

        stale = prospect()
        stale['expected_revision'] = original_revision
        stale['reason'] = 'Stale update must not win.'
        conflict = self.post_prospect(stale, 'sela-v2:prospect-1:stale')
        self.assertEqual(conflict.status_code, 200, conflict.get_data(as_text=True))
        self.assertEqual(conflict.get_json()['status'], 'REVIEW')
        self.assertEqual(conflict.get_json()['reason'], 'TROSA_REVISION_CONFLICT')

        listed = self.client.get('/api/integrations/sela/prospects', headers=self.headers())
        self.assertEqual(listed.get_json()['prospects'][0]['reason'], 'Current agent update.')

    def test_ambiguous_identity_becomes_trosa_inbox_review_not_a_local_queue(self):
        conn = self.hamid_db()
        for name in ('Existing A', 'Existing B'):
            customer_id = conn.execute(
                'INSERT INTO customers(name, company) VALUES (?, ?)', (name, name),
            ).lastrowid
            conn.execute(
                'INSERT INTO contacts(customer_id, name, email) VALUES (?, ?, ?)',
                (customer_id, name, 'ana@acrilicos.example'),
            )
        conn.commit()
        conn.close()

        response = self.post_prospect(prospect('ambiguous-prospect'), 'sela-v2:ambiguous:one')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['status'], 'REVIEW')
        self.assertEqual(response.get_json()['reason'], 'MULTIPLE_TROSA_MATCHES')

        conn = self.hamid_db()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_prospect_profiles').fetchone()[0], 0)
            inbox = conn.execute(
                "SELECT item_type, status FROM inbox_items WHERE dedupe_key='sela:prospect-review:ambiguous-prospect'"
            ).fetchone()
            self.assertEqual(tuple(inbox), ('sela_identity_review', 'open'))
        finally:
            conn.close()

    def test_reply_can_resolve_trosa_profile_without_a_sela_identity_map(self):
        created = self.post_prospect(prospect())
        self.assertEqual(created.status_code, 200)
        reply = {
            'candidate_id': 'prospect-1',
            'reply': {
                'message_id': 'reply-1', 'subject': 'Re: Acrylic sheet supply',
                'received_at': '2026-09-09 11:00:00', 'body': 'Please do not email us again.',
            },
            'action': {
                'name': 'STOPPED', 'route': 'STOP', 'intent': 'OPT_OUT',
                'reason': '客户明确要求停止联系。', 'do_not_contact': True,
            },
            'idempotency_key': 'sela-reply:profile-only',
        }
        response = self.client.post(
            '/api/integrations/sela/reply', json=reply,
            headers=self.headers(reply['idempotency_key']),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['status'], 'SYNCED')

        conn = self.hamid_db()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM follow_up_logs').fetchone()[0], 1)
            activity = conn.execute(
                'SELECT result FROM follow_up_logs WHERE customer_id=?', (created.get_json()['trosa_id'],)
            ).fetchone()
            self.assertIn('事件：REPLIED', activity['result'])
            profile = conn.execute(
                'SELECT contact_permission, suppression_reason, research_json FROM agent_prospect_profiles'
            ).fetchone()
            self.assertEqual(profile['contact_permission'], 'do_not_contact')
            self.assertIn('停止联系', profile['suppression_reason'])
            self.assertNotIn('last_outreach_event', json.loads(profile['research_json']).get('agent_state', {}))
            outreach = conn.execute(
                'SELECT reply_status, reply_content, reply_date FROM outreach_emails'
            ).fetchone()
            self.assertEqual(tuple(outreach), ('replied', 'Please do not email us again.', '2026-09-09'))
        finally:
            conn.close()

    def test_email_verification_is_stored_once_in_trosa_not_on_the_agent(self):
        created = self.post_prospect(prospect())
        self.assertEqual(created.status_code, 200)
        self.module._verify_email_with_original_rules = lambda email: {
            'email': email,
            'normalized': email,
            'status': 'valid',
            'category': '可以尝试发送',
            'deliverability_status': 'likely_deliverable',
            'confidence': 'medium',
            'address_type': 'person',
            'risk_flags': [],
            'evidence': [{'detail': 'Test MX record'}],
            'mx': [{'priority': 10, 'host': 'mail.acrilicos.example'}],
            'checked_at': '2026-09-09 12:00:00',
        }
        first = self.client.post(
            '/api/integrations/sela/prospects/prospect-1/email-verification',
            json={'email': 'ana@acrilicos.example'}, headers=self.headers(),
        )
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertFalse(first.get_json()['cached'])
        self.assertEqual(first.get_json()['prospect']['email_route_status'], 'VERIFIED')

        second = self.client.post(
            '/api/integrations/sela/prospects/prospect-1/email-verification',
            json={'email': 'ana@acrilicos.example'}, headers=self.headers(),
        )
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()['cached'])
        conn = self.hamid_db()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM email_verifications').fetchone()[0], 1)
        finally:
            conn.close()

    def test_business_exclusion_is_trosa_owned_and_idempotent(self):
        record = {
            'source': 'legacy_exclusion_registry', 'source_id': 'cust_00001',
            'canonical_name': '110 Events', 'aliases': ['110 Events LLC'],
            'domains': ['https://110uae.com/'], 'country': '阿联酋',
            'status': 'recommended_pending', 'match_policy': 'hard',
            'reason': '历史开发登记。',
        }
        key = 'sela-v2-exclusion:registry:one'
        first = self.client.post(
            '/api/integrations/sela/exclusions',
            json={'record': record, 'idempotency_key': key}, headers=self.headers(key),
        )
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertEqual(first.get_json()['status'], 'SYNCED')
        repeat = self.client.post(
            '/api/integrations/sela/exclusions',
            json={'record': record, 'idempotency_key': key}, headers=self.headers(key),
        )
        self.assertEqual(repeat.get_json(), first.get_json())

        snapshot = self.client.get('/api/integrations/sela/exclusions', headers=self.headers())
        self.assertEqual(snapshot.status_code, 200, snapshot.get_data(as_text=True))
        imported = next(row for row in snapshot.get_json()['records'] if row.get('source_id') == 'cust_00001')
        self.assertEqual(imported['source'], 'legacy_exclusion_registry')
        self.assertEqual(imported['domains'], ['110uae.com'])

    def test_exclusion_identity_decision_lives_in_trosa_profile_and_inbox(self):
        body = prospect()
        body['agent_state'] = {'exclusion_review': {
            'canonical_name': 'Acrílicos Histórico', 'matched_value': 'acrilicos',
            'registry_status': 'recommended_pending', 'source': 'legacy_exclusion_registry',
        }}
        created = self.post_prospect(body)
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        conn = self.hamid_db()
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM inbox_items WHERE item_type='sela_exclusion_review' AND status='open'"
            ).fetchone()[0], 1)
        finally:
            conn.close()

        response = self.client.post(
            '/api/integrations/sela/prospects/prospect-1/exclusion-decision',
            json={'decision': 'reject', 'note': '同一主体，停止联系。'}, headers=self.headers(),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['prospect']['do_not_contact'], True)
        conn = self.hamid_db()
        try:
            profile = conn.execute(
                'SELECT contact_permission, research_json FROM agent_prospect_profiles'
            ).fetchone()
            self.assertEqual(profile['contact_permission'], 'do_not_contact')
            self.assertEqual(json.loads(profile['research_json'])['agent_state']['exclusion_resolution'], 'REJECTED_SAME_ENTITY')
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM inbox_items WHERE item_type='sela_exclusion_review' AND status='resolved'"
            ).fetchone()[0], 1)
        finally:
            conn.close()

    def test_agent_requests_use_trosa_inbox_and_timeline_idempotently(self):
        created = self.post_prospect(prospect())
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        customer_id = created.get_json()['trosa_id']
        request_body = {
            'request': {
                'candidate_id': 'prospect-1',
                'customer_id': customer_id,
                'company': 'Acrílicos S.A.',
                'kind': 'CUSTOMER_REPLY',
                'severity': 'RED',
                'need': '人工回复报价问题',
                'context': '客户询问 3mm PMMA 价格。',
                'proposal': '确认价格和交期后回复。',
                'dedupe_key': 'sela:agent-request:prospect-1:quote-1',
            },
            'idempotency_key': 'sela:agent-request:prospect-1:quote-1',
        }
        headers = self.headers(request_body['idempotency_key'])
        first = self.client.post('/api/integrations/sela/needs', json=request_body, headers=headers)
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        first_body = first.get_json()
        self.assertEqual(first_body['status'], 'SYNCED')
        self.assertTrue(first_body['created'])
        item_id = first_body['item']['trosa_inbox_id']
        self.assertEqual(first_body['item']['company'], 'Acrílicos S.A.')
        self.assertEqual(first_body['item']['severity'], 'RED')

        repeat = self.client.post('/api/integrations/sela/needs', json=request_body, headers=headers)
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat.get_json(), first_body)
        listed = self.client.get(
            '/api/integrations/sela/needs?status=open', headers=self.headers(),
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([item['trosa_inbox_id'] for item in listed.get_json()['needs']], [item_id])

        resolve_body = {
            'action': 'edit',
            'resolution': '已确认客户需求，先核对当前价格表。',
            'idempotency_key': 'sela:agent-request-resolve:quote-1',
        }
        resolved = self.client.post(
            f'/api/integrations/sela/needs/{item_id}/resolve',
            json=resolve_body, headers=self.headers(resolve_body['idempotency_key']),
        )
        self.assertEqual(resolved.status_code, 200, resolved.get_data(as_text=True))
        self.assertEqual(resolved.get_json()['item']['status'], 'RESOLVED')
        self.assertEqual(resolved.get_json()['item']['resolution'], resolve_body['resolution'])

        conn = self.hamid_db()
        try:
            inbox = conn.execute(
                "SELECT item_type, status, resolution_reason, resolution_note FROM inbox_items WHERE id=?",
                (item_id,),
            ).fetchone()
            self.assertEqual(tuple(inbox), ('sela_agent_request', 'resolved', 'edit', resolve_body['resolution']))
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM follow_up_logs WHERE activity_type='agent_decision'"
            ).fetchone()[0], 1)
        finally:
            conn.close()

        resolved_repeat = self.client.post(
            f'/api/integrations/sela/needs/{item_id}/resolve',
            json=resolve_body, headers=self.headers(resolve_body['idempotency_key']),
        )
        self.assertEqual(resolved_repeat.status_code, 200)
        self.assertEqual(resolved_repeat.get_json(), resolved.get_json())

    def test_unmatched_gmail_is_a_trosa_capture_not_a_second_sela_history(self):
        message = {
            'id': 'gmail-unmatched-1',
            'thread_id': 'gmail-thread-unmatched-1',
            'from': 'unknown@example.com',
            'to': 'sales@example.com',
            'subject': 'Acrylic inquiry',
            'received_at': '2026-09-09T14:00:00+08:00',
            'body': 'Please send your product catalogue.',
        }
        key = 'sela:gmail-capture:gmail-unmatched-1'
        first = self.client.post(
            '/api/integrations/sela/inbox-captures',
            json={'message': message, 'idempotency_key': key},
            headers=self.headers(key),
        )
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertTrue(first.get_json()['created'])
        repeat = self.client.post(
            '/api/integrations/sela/inbox-captures',
            json={'message': message, 'idempotency_key': key},
            headers=self.headers(key),
        )
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat.get_json(), first.get_json())

        inbox = self.client.get('/api/inbox', headers=self.headers())
        self.assertEqual(inbox.status_code, 200, inbox.get_data(as_text=True))
        capture = next(item for item in inbox.get_json()['items'] if item['item_type'] == 'gmail_capture')
        self.assertEqual(capture['id'], first.get_json()['inbox_item_id'])
        self.assertIn('Please send your product catalogue.', capture['capture_content'])
        conn = self.hamid_db()
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM inbox_items WHERE dedupe_key=?", (key,)
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM follow_up_logs').fetchone()[0], 0)
        finally:
            conn.close()



if __name__ == '__main__':
    unittest.main()
