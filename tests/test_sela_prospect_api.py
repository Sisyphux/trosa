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
        db.cancel_safety_backup()
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

    def add_customer(self, name, **fields):
        conn = self.hamid_db()
        try:
            columns = {'name': name, 'company': name, **fields}
            conn.execute(
                'INSERT INTO customers (' + ', '.join(columns) + ') VALUES (' + ', '.join('?' for _ in columns) + ')',
                tuple(columns.values()),
            )
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.commit()
            return customer_id
        finally:
            conn.close()

    def add_reminder(self, customer_id, title, remind_date, *, reason='', content='', reminder_type='follow_up'):
        conn = self.hamid_db()
        try:
            cursor = conn.execute(
                '''INSERT INTO reminders (customer_id, title, content, reason, remind_date,
                                         is_done, reminder_type, created_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?, '2026-08-04 10:00:00')''',
                (customer_id, title, content, reason, remind_date, reminder_type),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def add_follow_log(self, customer_id, content, *, direction='outbound', source='manual',
                       activity_type='follow_up', follow_date='2026-09-01'):
        conn = self.hamid_db()
        try:
            conn.execute(
                '''INSERT INTO follow_up_logs
                   (customer_id, content, follow_date, result, next_plan, activity_type,
                    direction, source, is_reported, created_at)
                   VALUES (?, ?, ?, '', '', ?, ?, ?, 0, '2026-09-01 10:00:00')''',
                (customer_id, content, follow_date, activity_type, direction, source),
            )
            conn.commit()
        finally:
            conn.close()

    def reminder_done(self, reminder_id):
        conn = self.hamid_db()
        try:
            return conn.execute(
                'SELECT is_done FROM reminders WHERE id=?', (reminder_id,),
            ).fetchone()['is_done']
        finally:
            conn.close()

    def exclusion_records(self):
        response = self.client.get('/api/integrations/sela/exclusions', headers=self.headers())
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()['records']

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

        conn = self.hamid_db()
        try:
            created = conn.execute(
                'SELECT source FROM customers WHERE id=?', (result['trosa_id'],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(created['source'], 'Sela')

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

        # 技术性的 revision conflict 由 Sela 重试，不应转嫁成人工 Inbox 问题。
        conn = self.hamid_db()
        try:
            review = conn.execute(
                "SELECT COUNT(*) FROM inbox_items WHERE item_type='sela_identity_review' AND status='open'"
            ).fetchone()[0]
            self.assertEqual(review, 0)
        finally:
            conn.close()

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
        # 人工判断"是否同一主体"前必须看到具体匹配到哪些客户及依据。
        candidates = response.get_json()['candidates']
        self.assertEqual([candidate['company'] for candidate in candidates], ['Existing A', 'Existing B'])
        self.assertTrue(all('email' in candidate['matched_by'] for candidate in candidates))

        conn = self.hamid_db()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_prospect_profiles').fetchone()[0], 0)
            inbox = conn.execute(
                "SELECT item_type, status FROM inbox_items WHERE dedupe_key='sela:prospect-review:ambiguous-prospect'"
            ).fetchone()
            self.assertEqual(tuple(inbox), ('sela_identity_review', 'open'))
            stored = json.loads(conn.execute(
                "SELECT content FROM inbox_items WHERE dedupe_key='sela:prospect-review:ambiguous-prospect'"
            ).fetchone()['content'])
            self.assertEqual([item['company'] for item in stored['candidates']],
                             ['Existing A', 'Existing B'])
        finally:
            conn.close()

    def test_duplicate_domain_customers_auto_link_without_human_review(self):
        # 两条 Trosa 客户记录共享同一官网域名 = 同一家公司的重复记录，
        # 不该再让人回答“是否同一主体”。
        first = self.add_customer('Excelite Plas', website='https://exceliteplas.com.au/')
        self.add_customer('Excelite Plas Pty', website='https://exceliteplas.com.au/')
        conn = self.hamid_db()
        try:
            conn.execute(
                'INSERT INTO contacts(customer_id, name, email) VALUES (?, ?, ?)',
                (first, 'Buyer', 'buyer@exceliteplas.com.au'))
            conn.commit()
        finally:
            conn.close()
        body = prospect('dup-domain-prospect')
        body['company'] = 'Excelite Plas'
        body['website'] = 'https://exceliteplas.com.au/'
        body['contact'] = {'name': 'New Buyer', 'email': 'new@exceliteplas.com.au', 'is_primary': 1}
        response = self.post_prospect(body, 'sela-v2:dup-domain:one')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()
        self.assertEqual(result['status'], 'SYNCED', result)
        # 更完整、名称更吻合的记录被复用。
        self.assertEqual(result['trosa_id'], first)
        self.assertTrue(any('重复客户' in warning for warning in result['warnings']), result['warnings'])
        conn = self.hamid_db()
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM inbox_items WHERE item_type='sela_identity_review'"
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_prospect_profiles').fetchone()[0], 1)
        finally:
            conn.close()

    def test_same_name_without_domain_still_requires_review(self):
        # 只有名称相同、没有域名证据时仍必须人工判断，不能自动合并。
        self.add_customer('Shared Plastics')
        self.add_customer('Shared Plastics')
        body = prospect('name-only-prospect')
        body['company'] = 'Shared Plastics'
        body['website'] = ''
        body['contact'] = {'name': 'X', 'email': 'x@shared.example', 'is_primary': 1}
        response = self.post_prospect(body, 'sela-v2:name-only:one')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['status'], 'REVIEW')
        self.assertEqual(response.get_json()['reason'], 'MULTIPLE_TROSA_MATCHES')

    def test_duplicate_domain_prefers_record_without_sela_profile(self):
        first = self.add_customer('Excelite Plas', website='https://exceliteplas.com.au/')
        seed = prospect('seed-source')
        seed['company'] = 'Excelite Plas'
        seed['website'] = 'https://exceliteplas.com.au/'
        seed['contact'] = {'name': 'A', 'email': 'a@exceliteplas.com.au', 'is_primary': 1}
        seeded = self.post_prospect(seed, 'sela-v2:seed:one')
        self.assertEqual(seeded.get_json()['status'], 'SYNCED', seeded.get_data(as_text=True))
        self.assertEqual(seeded.get_json()['trosa_id'], first)
        second = self.add_customer('Excelite Plas Pty', website='https://exceliteplas.com.au/')
        body = prospect('dup-source')
        body['company'] = 'Excelite Plas'
        body['website'] = 'https://exceliteplas.com.au/'
        body['contact'] = {'name': 'B', 'email': 'b@exceliteplas.com.au', 'is_primary': 1}
        response = self.post_prospect(body, 'sela-v2:dup:one')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['status'], 'SYNCED', response.get_json())
        # 已归属其它 Sela 来源的记录不再复用，改用无归属的重复记录。
        self.assertEqual(response.get_json()['trosa_id'], second)

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

    def test_inbox_exclusion_decision_updates_sela_profile(self):
        self.client.post('/api/auth/login', json={'user': 'hamid'})
        body = prospect()
        body['agent_state'] = {'exclusion_review': {
            'canonical_name': 'Acrílicos Histórico', 'matched_value': 'acrilicos',
            'registry_status': 'recommended_pending', 'source': 'legacy_exclusion_registry',
        }}
        self.assertEqual(self.post_prospect(body).status_code, 200)
        payload = self.client.get('/api/inbox').get_json()
        self.assertEqual(len(payload['questions']), 1)
        question = payload['questions'][0]
        self.assertEqual(question['kind'], 'exclusion_review')
        self.assertEqual(question['evidence'][0]['structured']['fields'][0]['value'], 'Acrílicos Histórico')
        decision = next(field for field in question['response_schema']['fields'] if field['key'] == 'decision')
        self.assertEqual({choice['value'] for choice in decision['choices']}, {'accept', 'reject'})
        response = self.client.post('/api/inbox/questions/%s/respond' % question['id'], json={
            'revision': question['revision'],
            'answer': {'decision': 'reject', 'note': '名称和来源已核实为同一主体'},
            'idempotency_key': 'test-inbox-exclusion-decision',
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = self.hamid_db()
        try:
            profile = conn.execute(
                'SELECT contact_permission, research_json FROM agent_prospect_profiles WHERE source_id=?',
                ('prospect-1',),
            ).fetchone()
            self.assertEqual(profile['contact_permission'], 'do_not_contact')
            self.assertEqual(json.loads(profile['research_json'])['agent_state']['exclusion_resolution'], 'REJECTED_SAME_ENTITY')
        finally:
            conn.close()
        self.assertIn('更新 Trosa 中的 Sela 排除状态', response.get_json()['effects'][0])

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

    def test_sela_resume_is_not_queued_for_non_cold_contact_email_gap(self):
        source_id = 'resume-customer-email-gap'
        body = prospect(source_id)
        body.update({
            'contact': {}, 'outreach_status': '', 'subject': '', 'email_draft': '',
            'gmail_draft_id': '', 'gmail_thread_id': '', 'email': '',
        })
        created = self.post_prospect(body, 'sela-v2:resume-customer:create')
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        customer_id = int(created.get_json()['trosa_id'])

        need_key = 'sela:resume-customer:email-gap'
        need = self.client.post('/api/integrations/sela/needs', json={
            'request': {
                'source_id': source_id, 'customer_id': customer_id,
                'company': 'Acrílicos S.A.', 'kind': 'FACT_GAP', 'severity': 'AMBER',
                'need': '确认联系人邮箱',
                'missing_facts': [{'field': 'contact_email', 'label': '联系人邮箱', 'why': '尚未确认'}],
                'resume': '补充后继续研究', 'dedupe_key': need_key,
            }, 'idempotency_key': need_key,
        }, headers=self.headers(need_key))
        self.assertEqual(need.status_code, 200, need.get_data(as_text=True))
        inbox_id = int(need.get_json()['item']['trosa_inbox_id'])

        conn = self.hamid_db()
        try:
            # A legacy row can carry a generic fact_request marker. It still
            # needs Sela's structured response fields, not the generic form.
            conn.execute("UPDATE inbox_items SET question_kind='fact_request' WHERE id=?", (inbox_id,))
            conn.execute("UPDATE customers SET business_stage='成交' WHERE id=?", (customer_id,))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)

        question = next(item for item in self.client.get('/api/inbox').get_json()['questions']
                        if int(item['id']) == inbox_id)
        self.assertEqual(question['kind'], 'sela_request')
        self.assertEqual(question['subject']['label'], 'Trosa 客户')
        email_field = next(field for field in question['response_schema']['fields'] if field['key'] == 'fact_0')
        self.assertEqual(email_field['label'], '联系邮箱（仅供本次 Sela 请求/未发送草稿）')
        self.assertIn('不会写入或验证 Trosa 联系人', email_field['help'])
        self.assertTrue(any('不会写入或验证 Trosa 联系人' in effect
                            for effect in question['completion_effects']))
        self.assertTrue(any('不会发送邮件、验证邮箱' in effect for effect in question['will_not_do']))

        answered = self.client.post(f'/api/inbox/questions/{inbox_id}/respond', json={
            'revision': question['revision'], 'answer': {'fact_0': 'buyer@acrilicos.example'},
            'idempotency_key': 'resume-customer-email-gap-answer',
        })
        self.assertEqual(answered.status_code, 200, answered.get_data(as_text=True))
        result = answered.get_json()
        self.assertFalse(result['sela_handoff']['automatic_run'])
        self.assertEqual(result['sela_handoff']['status'], 'needs_review')
        self.assertIn('Sela 未自动续跑', result['next_system_step'])
        self.assertIn('不会写入或验证 Trosa 联系人', result['next_system_step'])

        resolved = self.client.get(
            f'/api/integrations/sela/needs?status=resolved&item_id={inbox_id}', headers=self.headers(),
        )
        self.assertEqual(resolved.status_code, 200, resolved.get_data(as_text=True))
        need_view = resolved.get_json()['needs'][0]
        self.assertEqual(need_view['human_response']['status'], 'answered')
        self.assertEqual(need_view['resume_run']['status'], 'needs_review')
        self.assertEqual(need_view['resume_run']['reason'], 'prospect_not_cold')
        answer_hash = self.module._sela_hash(need_view['human_response'])
        target = next(row for row in self.client.get(
            '/api/integrations/sela/prospects?limit=100', headers=self.headers(),
        ).get_json()['prospects'] if row['id'] == source_id)
        blocked_result = self.client.post(
            f'/api/integrations/sela/prospects/{source_id}/resume',
            headers=self.headers(f'sela:auto-resume:{inbox_id}:{answer_hash}'),
            json={
                'action': 'research', 'source_id': source_id, 'inbox_id': inbox_id,
                'answer_sha256': answer_hash, 'expected_revision': target['trosa_revision'], 'research': {},
            },
        )
        self.assertEqual(blocked_result.status_code, 200, blocked_result.get_data(as_text=True))
        self.assertEqual(blocked_result.get_json()['status'], 'REVIEW')
        self.assertEqual(blocked_result.get_json()['reason'], 'TROSA_STAGE_REQUIRES_TROSA')
        state = self.client.get(f'/api/inbox/questions/{inbox_id}/sela-handoff').get_json()
        self.assertEqual(state['status'], 'needs_review')
        self.assertFalse(state['automatic_run'])
        runs = self.client.get('/api/inbox').get_json()['sela_resume_runs']
        visible_run = next(run for run in runs if run['inbox_id'] == inbox_id)
        self.assertEqual(visible_run['status'], 'needs_review')

        conn = self.hamid_db()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM contacts WHERE customer_id=?', (customer_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM email_verifications WHERE lower(email)=?',
                                          ('buyer@acrilicos.example',)).fetchone()[0], 0)
        finally:
            conn.close()

    def test_historical_sela_resolution_is_not_backfilled_into_resume_queue(self):
        source_id = 'resume-history-1'
        body = prospect(source_id)
        body.update({'contact': {}, 'outreach_status': '', 'subject': '', 'email_draft': '',
                     'gmail_draft_id': '', 'gmail_thread_id': '', 'email': ''})
        created = self.post_prospect(body, 'sela-v2:resume-history:create')
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        need_key = 'sela:resume-history:need'
        need = self.client.post('/api/integrations/sela/needs', json={
            'request': {'source_id': source_id, 'customer_id': created.get_json()['trosa_id'],
                        'kind': 'FACT_GAP', 'need': '历史回答回归',
                        'missing_facts': [{'field': 'product_direction', 'label': '产品方向'}],
                        'dedupe_key': need_key},
            'idempotency_key': need_key,
        }, headers=self.headers(need_key))
        self.assertEqual(need.status_code, 200, need.get_data(as_text=True))
        inbox_id = int(need.get_json()['item']['trosa_inbox_id'])

        old_resolution = {'action': 'edit', 'resolution': '既有历史人工处理'}
        resolved = self.client.post(
            f'/api/integrations/sela/needs/{inbox_id}/resolve', json=old_resolution,
            headers=self.headers('resume-history:resolve'),
        )
        self.assertEqual(resolved.status_code, 200, resolved.get_data(as_text=True))
        need_view = self.client.get(
            f'/api/integrations/sela/needs?status=resolved&item_id={inbox_id}', headers=self.headers(),
        ).get_json()['needs'][0]
        self.assertIsNone(need_view['human_response'])
        self.assertIsNone(need_view['resume_run'])
        self.assertFalse(any(run['inbox_id'] == inbox_id
                             for run in self.client.get('/api/inbox', headers=self.headers()).get_json()['sela_resume_runs']))

    def test_send_approval_response_never_queues_sela(self):
        source_id = 'resume-retired-send-1'
        body = prospect(source_id)
        body.update({'contact': {}, 'outreach_status': '', 'subject': '', 'email_draft': '',
                     'gmail_draft_id': '', 'gmail_thread_id': '', 'email': ''})
        created = self.post_prospect(body, 'sela-v2:resume-retired-send:create')
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        need_key = 'sela:resume-retired-send:need'
        need = self.client.post('/api/integrations/sela/needs', json={
            'request': {'source_id': source_id, 'customer_id': created.get_json()['trosa_id'],
                        'kind': 'SEND_APPROVAL', 'need': '旧发送审批', 'context': '不得触发发送。',
                        'dedupe_key': need_key},
            'idempotency_key': need_key,
        }, headers=self.headers(need_key))
        self.assertEqual(need.status_code, 200, need.get_data(as_text=True))
        inbox_id = int(need.get_json()['item']['trosa_inbox_id'])
        self.assertEqual(self.client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)
        question = next(item for item in self.client.get('/api/inbox').get_json()['questions']
                        if int(item['id']) == inbox_id)
        answered = self.client.post(f'/api/inbox/questions/{inbox_id}/respond', json={
            'revision': question['revision'], 'answer': {}, 'idempotency_key': 'retired-send-answer',
        })
        self.assertEqual(answered.status_code, 200, answered.get_data(as_text=True))
        self.assertFalse(answered.get_json()['sela_handoff']['automatic_run'])
        self.assertIn('没有发送邮件', answered.get_json()['next_system_step'])
        need_view = self.client.get(
            f'/api/integrations/sela/needs?status=resolved&item_id={inbox_id}', headers=self.headers(),
        ).get_json()['needs'][0]
        self.assertEqual(need_view['human_response']['status'], 'retired')
        self.assertIsNone(need_view['resume_run'])

    def test_answered_inbox_can_save_only_an_unsent_research_backed_draft(self):
        source_id = 'resume-draft-1'
        body = prospect(source_id)
        body.update({
            'contact': {}, 'outreach_status': '', 'subject': '', 'email_draft': '',
            'gmail_draft_id': '', 'gmail_thread_id': '', 'email': '',
        })
        created = self.post_prospect(body, 'sela-v2:resume-draft:create')
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        customer_id = int(created.get_json()['trosa_id'])

        need_key = 'sela:resume-draft:need'
        need = self.client.post('/api/integrations/sela/needs', json={
            'request': {
                'source_id': source_id, 'candidate_id': source_id,
                'customer_id': customer_id, 'company': 'Acrílicos S.A.',
                'kind': 'FACT_GAP', 'severity': 'AMBER', 'need': '确认业务邮箱后继续研究',
                'missing_facts': [{'field': 'contact_email', 'label': '联系邮箱', 'why': '当前无已确认邮箱'}],
                'resume': '补充后继续研究并准备未发送草稿', 'dedupe_key': need_key,
            }, 'idempotency_key': need_key,
        }, headers=self.headers(need_key))
        self.assertEqual(need.status_code, 200, need.get_data(as_text=True))
        inbox_id = int(need.get_json()['item']['trosa_inbox_id'])

        self.assertEqual(self.client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)
        question = next(row for row in self.client.get('/api/inbox').get_json()['questions']
                        if int(row['id']) == inbox_id)
        self.assertEqual(question['subject']['label'], 'Trosa 冷线索')
        self.assertTrue(any('未互动冷线索' in item for item in question['known_facts']))
        answered = self.client.post(f'/api/inbox/questions/{inbox_id}/respond', json={
            'revision': question['revision'], 'answer': {'fact_0': 'buyer@acrilicos.example'},
            'idempotency_key': 'resume-draft-answer-1',
        })
        self.assertEqual(answered.status_code, 200, answered.get_data(as_text=True))
        self.assertTrue(answered.get_json()['sela_handoff']['automatic_run'])
        resolved = self.client.get('/api/integrations/sela/needs?status=resolved', headers=self.headers())
        self.assertEqual(resolved.status_code, 200, resolved.get_data(as_text=True))
        resolved_need = next(row for row in resolved.get_json()['needs'] if row['trosa_inbox_id'] == inbox_id)
        answer_hash = self.module._sela_hash(resolved_need['human_response'])
        listed = self.client.get('/api/integrations/sela/prospects?limit=100', headers=self.headers())
        target = next(row for row in listed.get_json()['prospects'] if row['id'] == source_id)

        resume_key = f'sela:auto-resume:{inbox_id}:{answer_hash}'
        resume_payload = {
            'action': 'draft', 'source_id': source_id, 'inbox_id': inbox_id,
            'answer_sha256': answer_hash, 'expected_revision': target['trosa_revision'],
            'research': {
                'research_reason': '官网确认主营亚克力板材加工。',
                'qualification_method': '官网公开资料',
                'qualification_reason': '公开页面展示板材加工业务。',
                'evidence': [{'url': 'https://acrilicos.example/about', 'quote': 'We fabricate acrylic sheets.'}],
                'source_urls': ['https://acrilicos.example/about'],
            },
            'draft': {
                'recipient_email': 'buyer@acrilicos.example',
                'subject': 'Acrylic sheet inquiry',
                'body': 'Hello, I would like to learn about your acrylic sheet range.',
            },
        }
        rejected_payload = dict(resume_payload)
        rejected_payload['draft'] = dict(resume_payload['draft'], recipient_email='unconfirmed@elsewhere.example')
        rejected = self.client.post(
            f'/api/integrations/sela/prospects/{source_id}/resume', json=rejected_payload,
            headers=self.headers(resume_key),
        )
        self.assertEqual(rejected.status_code, 409, rejected.get_data(as_text=True))

        saved = self.client.post(
            f'/api/integrations/sela/prospects/{source_id}/resume', json=resume_payload,
            headers=self.headers(resume_key),
        )
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        self.assertEqual(saved.get_json()['status'], 'SYNCED')
        replay_payload = dict(resume_payload)
        replay_payload['draft'] = dict(resume_payload['draft'], body='A different retry must not overwrite the first saved draft.')
        replay = self.client.post(
            f'/api/integrations/sela/prospects/{source_id}/resume', json=replay_payload,
            headers=self.headers(resume_key),
        )
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(replay.get_json(), saved.get_json())

        conn = self.hamid_db()
        try:
            profile = conn.execute(
                'SELECT research_json FROM agent_prospect_profiles WHERE source_id=?', (source_id,),
            ).fetchone()
            research = json.loads(profile['research_json'])
            self.assertEqual(research['research_reason'], '官网确认主营亚克力板材加工。')
            self.assertEqual(research['evidence'][0]['url'], 'https://acrilicos.example/about')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM contacts WHERE customer_id=?', (customer_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM reminders WHERE customer_id=?', (customer_id,)).fetchone()[0], 0)
            outreach = conn.execute(
                'SELECT * FROM outreach_emails WHERE external_source=? AND external_id=?',
                ('sela', source_id),
            ).fetchone()
            self.assertIsNotNone(outreach)
            self.assertEqual(outreach['recipient_email'], 'buyer@acrilicos.example')
            self.assertEqual(outreach['sent_date'], '')
            self.assertIsNone(outreach['contact_id'])
            self.assertEqual(outreach['message_id'], '')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM email_delivery_events').fetchone()[0], 0)
        finally:
            conn.close()

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

    def test_customer_without_real_contact_is_not_a_hard_exclusion(self):
        customer_id = self.add_customer('Uncontacted Prospect Co', website='https://uncontacted.example/')
        records = self.exclusion_records()
        self.assertFalse(any(row.get('record_id') == f'trosa-customer:{customer_id}' for row in records))

        conn = self.hamid_db()
        try:
            conn.execute(
                """INSERT INTO follow_up_logs (customer_id, content, follow_date, direction, activity_type, created_at)
                   VALUES (?, '客户回复了报价', '2026-09-10', 'inbound', 'customer_reply', '2026-09-10 10:00:00')""",
                (customer_id,),
            )
            conn.commit()
        finally:
            conn.close()
        record = next(row for row in self.exclusion_records() if row.get('record_id') == f'trosa-customer:{customer_id}')
        self.assertEqual(record['match_policy'], 'hard')
        self.assertTrue(record['contacted'])
        self.assertEqual(record['contact_evidence'], 'recorded_communication')

    def test_imported_outreach_date_alone_does_not_establish_contact(self):
        customer_id = self.add_customer('Legacy Import Co', website='https://legacy-import.example/')
        conn = self.hamid_db()
        try:
            conn.execute(
                """INSERT INTO outreach_emails (customer_id, subject, content, sent_date, reply_status, created_at)
                   VALUES (?, '历史 sela 外联', '历史 sela 外联', '2026-08-20', 'pending', '2026-09-09 11:00:00')""",
                (customer_id,),
            )
            conn.commit()
            outreach_id = conn.execute('SELECT id FROM outreach_emails WHERE customer_id=?', (customer_id,)).fetchone()[0]
        finally:
            conn.close()
        self.assertFalse(any(row.get('record_id') == f'trosa-customer:{customer_id}' for row in self.exclusion_records()))

        conn = self.hamid_db()
        try:
            conn.execute(
                """INSERT INTO email_delivery_events (email, outreach_email_id, event_type, message_id, source, occurred_at)
                   VALUES ('sales@legacy-import.example', ?, 'sent', 'message-1', 'sela', '2026-09-09 12:00:00')""",
                (outreach_id,),
            )
            conn.commit()
        finally:
            conn.close()
        record = next(row for row in self.exclusion_records() if row.get('record_id') == f'trosa-customer:{customer_id}')
        self.assertEqual(record['contact_evidence'], 'delivery_event')

    def test_open_inbound_capture_matched_to_contact_is_contact_evidence(self):
        customer_id = self.add_customer('Capture Evidence Co', website='https://capture-evidence.example/')
        conn = self.hamid_db()
        try:
            conn.execute(
                "INSERT INTO contacts (customer_id, name, email, is_primary) "
                "VALUES (?, 'Sales', 'sales@capture-evidence.example', 1)",
                (customer_id,),
            )
            conn.commit()
        finally:
            conn.close()
        message = {
            'id': 'gmail-reply-capture-1', 'thread_id': 'thread-1',
            'from': 'Sales <sales@capture-evidence.example>',
            'subject': 'Re: Acrylic sheet', 'received_at': '2026-09-14T10:00:00+08:00',
            'body': 'Please send more information.',
        }
        key = 'sela:gmail-capture:gmail-reply-capture-1'
        posted = self.client.post(
            '/api/integrations/sela/inbox-captures',
            json={'message': message, 'idempotency_key': key}, headers=self.headers(key),
        )
        self.assertEqual(posted.status_code, 200, posted.get_data(as_text=True))
        record = next(row for row in self.exclusion_records() if row.get('record_id') == f'trosa-customer:{customer_id}')
        self.assertTrue(record['contacted'])
        self.assertEqual(record['contact_evidence'], 'inbound_capture')

    def test_prospect_reuses_existing_customer_by_domain_instead_of_duplicating(self):
        customer_id = self.add_customer('Acrilicos S.A.', website='https://acrilicos.example/')
        body = prospect('domain-reuse-prospect')
        body['contact'] = {'name': 'Nuevo Contacto', 'email': 'nuevo@acrilicos.example', 'is_primary': 1}
        response = self.post_prospect(body, 'sela-v2:domain-reuse:one')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()
        self.assertEqual(result['status'], 'SYNCED', result)
        self.assertFalse(result['created'])
        self.assertEqual(result['trosa_id'], customer_id)
        conn = self.hamid_db()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_prospect_profiles').fetchone()[0], 1)
        finally:
            conn.close()

    def test_same_name_with_conflicting_domain_requires_review(self):
        customer_id = self.add_customer('Conflict Plastics', website='https://conflict-plastics.example/')
        body = prospect('conflict-prospect')
        body['company'] = 'Conflict Plastics'
        body['website'] = 'https://conflict-plastics-other.example/'
        body['contact'] = {'name': 'X', 'email': 'x@conflict-plastics-other.example', 'is_primary': 1}
        response = self.post_prospect(body, 'sela-v2:conflict:one')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['status'], 'REVIEW')
        self.assertEqual(response.get_json()['reason'], 'COMPANY_DOMAIN_CONFLICT')
        conn = self.hamid_db()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM inbox_items WHERE item_type='sela_identity_review'"
            ).fetchone()[0], 1)
        finally:
            conn.close()


    def test_confirmed_outreach_closes_legacy_development_task(self):
        created = self.post_prospect(prospect())
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        customer_id = created.get_json()['trosa_id']
        reminder_id = self.add_reminder(
            customer_id, '开发新客户: Acrílicos S.A.', '2026-09-01',
            reason='官网导入，待首次联系', content='开发新客户。\n备注：新开发流程实验中。',
        )
        self.assertEqual(self.reminder_done(reminder_id), 0)

        sent = prospect()
        sent.update({
            'outreach_status': 'SENT',
            'sent_at': '2026-09-09 10:10:00',
            'gmail_message_id': 'message-1',
        })
        response = self.post_prospect(sent, 'sela-v2:prospect-1:sent')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

        self.assertEqual(self.reminder_done(reminder_id), 1)
        self.client.post('/api/auth/login', json={'user': 'hamid'})
        today = self.client.get('/api/reminders/today')
        self.assertEqual(today.status_code, 200, today.get_data(as_text=True))
        self.assertNotIn(reminder_id, [row['id'] for row in today.get_json()])

    def test_confirmed_outreach_closes_prospect_stage_tasks_by_relationship_fact(self):
        created = self.post_prospect(prospect())
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        customer_id = created.get_json()['trosa_id']
        # Without a real interaction every open follow-up is prospect-stage
        # development, whatever its title or due date.  The boundary is a
        # relationship fact, never a title keyword.
        plain_task = self.add_reminder(
            customer_id, '联系 Acrílicos S.A.', '2026-09-01', reason='人工安排的下一步',
        )
        future_task = self.add_reminder(
            customer_id, '二次开发: Acrílicos S.A.', '2026-09-20', reason='计划内的二次开发',
        )
        later_task = self.add_reminder(
            customer_id, '开发新客户: Acrílicos S.A.', '2026-09-25', reason='官网导入，待首次联系',
        )

        sent = prospect()
        sent.update({
            'outreach_status': 'SENT',
            'sent_at': '2026-09-09 10:10:00',
            'gmail_message_id': 'message-1',
        })
        response = self.post_prospect(sent, 'sela-v2:prospect-1:sent')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

        self.assertEqual(self.reminder_done(plain_task), 1)
        self.assertEqual(self.reminder_done(future_task), 1)
        self.assertEqual(self.reminder_done(later_task), 1)

    def test_today_hides_prospect_tasks_until_a_real_interaction_exists(self):
        body = prospect('today-boundary-prospect')
        created = self.post_prospect(body, 'sela-v2:today-boundary:create')
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        customer_id = created.get_json()['trosa_id']
        task_id = self.add_reminder(customer_id, '联系 Acrílicos S.A.', '2026-09-05', reason='')

        self.client.post('/api/auth/login', json={'user': 'hamid'})
        today = self.client.get('/api/reminders/today')
        self.assertEqual(today.status_code, 200, today.get_data(as_text=True))
        self.assertNotIn(task_id, [row['id'] for row in today.get_json()])

        # A logged one-way contact (our sent note, an imported history row) is
        # still unreplied development and must not bring the task into Today.
        outbound = self.client.post(
            f'/api/customers/{customer_id}/follow_history',
            json={'activity_content': '已发开发信', 'direction': 'outbound',
                  'follow_date': '2026-09-01'},
        )
        self.assertEqual(outbound.status_code, 200, outbound.get_data(as_text=True))
        today = self.client.get('/api/reminders/today')
        self.assertEqual(today.status_code, 200, today.get_data(as_text=True))
        self.assertNotIn(task_id, [row['id'] for row in today.get_json()])

        # An explicitly recorded inbound communication is a real relationship
        # fact, so the same task is allowed to enter Today again.
        recorded = self.client.post(
            f'/api/customers/{customer_id}/follow_history',
            json={'activity_content': '客户回复询价', 'direction': 'inbound',
                  'follow_date': '2026-09-02'},
        )
        self.assertEqual(recorded.status_code, 200, recorded.get_data(as_text=True))
        today = self.client.get('/api/reminders/today')
        self.assertEqual(today.status_code, 200, today.get_data(as_text=True))
        self.assertIn(task_id, [row['id'] for row in today.get_json()])

    def test_today_boundary_judges_content_not_a_single_field(self):
        self.client.post('/api/auth/login', json={'user': 'hamid'})
        # One-way imported record (our letter mentioning a quote/catalogue) -> Sela.
        one_way = self.add_customer('One Way Import Co')
        one_way_task = self.add_reminder(one_way, '联系 One Way', '2026-09-05')
        self.add_follow_log(one_way, '7.10发送开发信，附报价目录和产品册', direction='outbound')
        # Customer-side inquiry, even logged as outbound source -> human reminder.
        inquiry = self.add_customer('Inquiry Co')
        inquiry_task = self.add_reminder(inquiry, '联系 Inquiry', '2026-09-05')
        self.add_follow_log(inquiry, '客户询问价格和规格', direction='outbound')
        # Confirmed exchange (real quote given) -> engaged.
        quoted = self.add_customer('Quoted Co')
        quoted_task = self.add_reminder(quoted, '联系 Quoted', '2026-09-05')
        self.add_follow_log(quoted, '7.30询问价格 7.31报价2.68、2.80', direction='outbound')

        today = self.client.get('/api/reminders/today')
        self.assertEqual(today.status_code, 200, today.get_data(as_text=True))
        ids = [row['id'] for row in today.get_json()]
        self.assertNotIn(one_way_task, ids)
        self.assertIn(inquiry_task, ids)
        self.assertIn(quoted_task, ids)

    def test_human_can_unblock_dnc_and_sela_cannot(self):
        body = prospect()
        body['agent_state'] = {'exclusion_review': {
            'canonical_name': 'Acrílicos Histórico', 'matched_value': 'acrilicos',
            'registry_status': 'recommended_pending', 'source': 'legacy_exclusion_registry',
        }}
        created = self.post_prospect(body)
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        customer_id = created.get_json()['trosa_id']
        blocked = self.client.post(
            '/api/integrations/sela/prospects/prospect-1/exclusion-decision',
            json={'decision': 'reject', 'note': '同一主体，停止联系。'}, headers=self.headers(),
        )
        self.assertEqual(blocked.status_code, 200, blocked.get_data(as_text=True))
        self.assertTrue(blocked.get_json()['prospect']['do_not_contact'])
        suppressed = [row for row in self.exclusion_records()
                      if row.get('status') == 'do_not_contact']
        self.assertTrue(suppressed)

        # Sela service token must not reach the human-only unblock route.
        denied = self.client.post(
            f'/api/customers/{customer_id}/agent-prospect/contact-permission',
            json={'permission': 'allowed', 'note': 'sela 自助解禁'},
            headers=self.headers('sela-v2:unblock:denied'),
        )
        self.assertIn(denied.status_code, (401, 403), denied.get_data(as_text=True))

        # Human login can unblock with an audit note.
        login = self.client.post('/api/auth/login', json={'user': 'hamid'})
        self.assertEqual(login.status_code, 200, login.get_data(as_text=True))
        missing_note = self.client.post(
            f'/api/customers/{customer_id}/agent-prospect/contact-permission',
            json={'permission': 'allowed', 'note': ''},
        )
        self.assertEqual(missing_note.status_code, 400, missing_note.get_data(as_text=True))
        unblocked = self.client.post(
            f'/api/customers/{customer_id}/agent-prospect/contact-permission',
            json={'permission': 'allowed', 'note': '批量误标，人工核实后恢复'},
        )
        self.assertEqual(unblocked.status_code, 200, unblocked.get_data(as_text=True))
        body = unblocked.get_json()
        self.assertTrue(body['success'])
        self.assertFalse(body['prospect']['do_not_contact'])
        self.assertNotEqual(body['prospect']['outreach_status'], 'PAUSED')
        suppressed_after = [row for row in self.exclusion_records()
                            if row.get('status') == 'do_not_contact']
        self.assertFalse(suppressed_after)
        conn = self.hamid_db()
        try:
            profile = conn.execute(
                'SELECT contact_permission, suppression_reason, research_json FROM agent_prospect_profiles'
            ).fetchone()
            self.assertEqual(profile['contact_permission'], 'allowed')
            self.assertEqual(profile['suppression_reason'], '')
            changes = json.loads(profile['research_json'])['agent_state']['contact_permission_changes']
            self.assertEqual(changes[-1]['to'], 'allowed')
            self.assertIn('批量误标', changes[-1]['note'])
            audit = conn.execute(
                "SELECT action, target_type, target_id, details FROM operation_logs"
                " WHERE target_type='sela_prospect' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(audit['action'], 'UNBLOCK')
            self.assertEqual(audit['target_id'], customer_id)
            self.assertIn('批量误标', audit['details'])
        finally:
            conn.close()

    def test_business_exclusion_view_exposes_is_active_and_supports_deactivate(self):
        login = self.client.post('/api/auth/login', json={'user': 'hamid'})
        self.assertEqual(login.status_code, 200, login.get_data(as_text=True))
        created = self.client.post('/api/business-exclusions', json={
            'canonical_name': 'Block Co', 'source_id': 'block-co-1', 'reason': '测试排除',
        })
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        record_id = created.get_json()['record']['record_id']
        exclusion_id = int(record_id.split(':')[1])
        listed = self.client.get('/api/business-exclusions').get_json()['records']
        self.assertTrue(next(row for row in listed if row['record_id'] == record_id)['is_active'])
        removed = self.client.delete(
            f'/api/business-exclusions/{exclusion_id}', json={'reason': '误标，人工停用'})
        self.assertEqual(removed.status_code, 200, removed.get_data(as_text=True))
        self.assertFalse(removed.get_json()['record']['is_active'])
        self.assertFalse(any(
            row.get('record_id') == record_id for row in self.exclusion_records()))
        conn = self.hamid_db()
        try:
            audit = conn.execute(
                "SELECT action, target_type, target_id FROM operation_logs"
                " WHERE target_type='business_exclusion' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(audit['action'], 'UNBLOCK')
            self.assertEqual(audit['target_id'], exclusion_id)
        finally:
            conn.close()

    def _publish(self, source_id, **fields):
        body = prospect(source_id)
        body['company'] = f'{source_id} Co'
        body['website'] = f'https://{source_id}.example/'
        body['contact'] = {'name': 'Buyer', 'email': f'buyer@{source_id}.example'}
        body.update(fields)
        return self.post_prospect(body, f'sela-v2:{source_id}:one')

    def test_prospect_projection_uses_earliest_sent_event_as_stable_first_touch(self):
        source_id = 'first-touch-time'
        self._publish(source_id)
        conn = self.hamid_db()
        try:
            outreach_id = conn.execute(
                '''SELECT id FROM outreach_emails
                   WHERE external_source='sela' AND external_id=?''',
                (source_id,),
            ).fetchone()['id']
            conn.execute(
                "UPDATE outreach_emails SET sent_date='2026-09-24' WHERE id=?",
                (outreach_id,),
            )
            conn.commit()
        finally:
            conn.close()

        before = self.client.get(
            '/api/integrations/sela/prospects?limit=100', headers=self.headers(),
        ).get_json()['prospects']
        row = next(item for item in before if item['id'] == source_id)
        self.assertEqual(row['first_touch_at'], '')
        before_revision = row['trosa_revision']

        conn = self.hamid_db()
        try:
            # The offset-aware event is lexically first, but the naive local
            # timestamp is the earlier instant in Asia/Shanghai.
            conn.executemany(
                '''INSERT INTO email_delivery_events
                   (email, outreach_email_id, event_type, message_id, source, occurred_at)
                   VALUES (?, ?, ?, ?, 'test', ?)''',
                [
                    (f'buyer@{source_id}.example', outreach_id, 'sent', 'later-utc',
                     '2026-09-17T22:30:00Z'),
                    (f'buyer@{source_id}.example', outreach_id, 'sent', 'earlier-local',
                     '2026-09-18 00:10:00'),
                    (f'buyer@{source_id}.example', outreach_id, 'bounced', 'bounce',
                     '2026-09-01 00:00:00'),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        after = self.client.get(
            '/api/integrations/sela/prospects?limit=100', headers=self.headers(),
        ).get_json()['prospects']
        row = next(item for item in after if item['id'] == source_id)
        self.assertEqual(row['first_touch_at'], '2026-09-18')
        self.assertNotEqual(row['trosa_revision'], before_revision)

    def _post_reply(self, source_id, event, intent='UNKNOWN'):
        reply = {
            'candidate_id': source_id,
            'reply': {
                'message_id': f'reply-{source_id}', 'subject': 'Re: Acrylic sheet supply',
                'received_at': '2026-09-09 11:00:00', 'body': 'Please send your price list.',
            },
            'action': {'name': 'REPLIED', 'route': 'HUMAN', 'event': event, 'intent': intent},
            'idempotency_key': f'sela-reply:{source_id}',
        }
        response = self.client.post(
            '/api/integrations/sela/reply', json=reply,
            headers=self.headers(reply['idempotency_key']),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def test_lifecycle_stage_and_customer_linked_come_from_business_facts(self):
        """Four real cases must agree with the Sela fact layer, without ids."""
        # 1. Never-replied prospect: a trosa_id exists but is not a relationship.
        cold = self._publish('cold-1').get_json()
        self.assertIsNotNone(cold['trosa_id'])

        # 4. Won customer: explicit human business_stage=成交.
        won_id = self._publish('won-1').get_json()['trosa_id']
        conn = self.hamid_db()
        try:
            conn.execute("UPDATE customers SET business_stage='成交' WHERE id=?", (won_id,))
            conn.commit()
        finally:
            conn.close()

        # 2. Replied lead: a neutral real reply is an engaged lead.
        self._publish('lead-1')
        self._post_reply('lead-1', 'REPLIED')
        # 3. Clear opportunity: a commercially interested reply.
        self._publish('opp-1')
        self._post_reply('opp-1', 'INTERESTED', intent='INTERESTED')

        listed = self.client.get('/api/integrations/sela/prospects', headers=self.headers())
        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        by_id = {row['id']: row for row in listed.get_json()['prospects']}
        self.assertEqual(by_id['cold-1']['lifecycle_stage'], 'cold_prospect')
        self.assertFalse(by_id['cold-1']['customer_linked'])
        self.assertEqual(by_id['lead-1']['lifecycle_stage'], 'engaged_lead')
        self.assertTrue(by_id['lead-1']['customer_linked'])
        self.assertEqual(by_id['opp-1']['lifecycle_stage'], 'qualified_opportunity')
        self.assertTrue(by_id['opp-1']['customer_linked'])
        self.assertEqual(by_id['won-1']['lifecycle_stage'], 'customer')
        self.assertTrue(by_id['won-1']['customer_linked'])

    def test_customer_row_alone_is_not_a_customer_or_opportunity(self):
        """A synced customer row plus ids must never imply a relationship."""
        created = self._publish('id-only-1').get_json()
        self.assertIsNotNone(created['trosa_id'])
        conn = self.hamid_db()
        try:
            customer = conn.execute(
                'SELECT business_stage, type FROM customers WHERE id=?', (created['trosa_id'],)
            ).fetchone()
            # The row exists and carries an import classification, yet it is a
            # cold prospect because no real interaction happened.
            self.assertEqual(customer['business_stage'], '')
            conn.execute(
                "UPDATE customers SET customer_type='existing' WHERE id=?", (created['trosa_id'],)
            )
            conn.commit()
        finally:
            conn.close()
        rows = self.client.get('/api/integrations/sela/prospects', headers=self.headers()).get_json()['prospects']
        row = next(item for item in rows if item['id'] == 'id-only-1')
        self.assertEqual(row['lifecycle_stage'], 'cold_prospect')
        self.assertFalse(row['customer_linked'])

    def test_agent_request_preserves_structured_fact_fields(self):
        customer_id = self.post_prospect(prospect()).get_json()['trosa_id']
        key = 'sela:agent-request:prospect-1:email-1'
        request_body = {
            'request': {
                'candidate_id': 'prospect-1', 'customer_id': customer_id,
                'company': 'Acrílicos S.A.', 'kind': 'FACT_GAP', 'severity': 'AMBER',
                'session_id': 'session-fact-gap-1',
                'need': '缺少关键邮箱', 'context': '官网没有公开邮箱，无法首次触达。',
                'proposal': '板材',
                'missing_facts': [{'field': 'contact_email', 'label': '关键邮箱',
                                   'why': '官网无公开邮箱', 'blocking': True}],
                'decision': {'question': '优先哪个产品线？', 'options': ['板材', '展示架'],
                             'recommended': '板材'},
                'evidence': [{'source': '官网', 'quote': '联系我们'}],
                'resume': '补充邮箱后继续首次开发',
                'dedupe_key': key,
            },
            'idempotency_key': key,
        }
        response = self.client.post(
            '/api/integrations/sela/needs', json=request_body, headers=self.headers(key),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        item = response.get_json()['item']
        self.assertEqual(item['kind'], 'FACT_GAP')
        self.assertEqual(item['severity'], 'AMBER')
        self.assertEqual(item['missing_facts'][0]['field'], 'contact_email')
        self.assertEqual(item['decision']['options'], ['板材', '展示架'])
        self.assertEqual(item['evidence'][0]['source'], '官网')
        self.assertEqual(item['resume'], '补充邮箱后继续首次开发')
        self.assertEqual(item['session_id'], 'session-fact-gap-1')

        # The structure is persisted as a field, not only rendered into prose.
        conn = self.hamid_db()
        try:
            stored = conn.execute(
                'SELECT request_json FROM inbox_items WHERE id=?', (item['trosa_inbox_id'],)
            ).fetchone()['request_json']
        finally:
            conn.close()
        self.assertTrue(stored)
        self.assertEqual(json.loads(stored)['kind'], 'FACT_GAP')

        listed = self.client.get(
            '/api/integrations/sela/needs?status=open', headers=self.headers(),
        ).get_json()['needs']
        returned = next(need for need in listed if need['trosa_inbox_id'] == item['trosa_inbox_id'])
        self.assertEqual(returned['missing_facts'][0]['field'], 'contact_email')
        self.assertEqual(returned['resume'], '补充邮箱后继续首次开发')


if __name__ == '__main__':
    unittest.main()
