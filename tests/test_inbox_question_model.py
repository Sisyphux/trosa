"""Inbox 问题模型回归：只承接必须人工判断的未决问题。"""
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
import inbox_reconcile


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_inbox_question_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


def gmail_capture_content(sender_email, message_id, text='Need acrylic sheets'):
    return json.dumps({
        'channel': 'gmail',
        'platform': 'Gmail',
        'conversation_identity': sender_email,
        'messages': [{
            'message_id': message_id,
            'sender_email': sender_email,
            'sender': sender_email,
            'direction': 'inbound',
            'text': text,
        }],
    }, ensure_ascii=False)


class InboxQuestionModelTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        self.original_demo = os.environ.get('CRM_SEED_DEMO_DATA')
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.init_all_dbs()
        self.module = load_app()
        self.client = self.module.app.test_client()
        self.client.post('/api/auth/login', json={'user': 'hamid'})

    def tearDown(self):
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    def _conn(self):
        db.set_db_user('hamid')
        return db.get_db()

    def _insert_capture(self, sender_email, message_id, item_type='gmail_capture', question_key='', text='Need acrylic sheets'):
        conn = self._conn()
        try:
            conn.execute(
                '''INSERT INTO inbox_items
                   (item_type, customer_id, title, content, dedupe_key, status, created_at,
                    question_kind, question_key, source_type)
                   VALUES (?, NULL, ?, ?, ?, 'open', '2026-09-18 09:00:00', 'identity', ?, 'gmail')''',
                (item_type, '待归属：' + sender_email,
                 gmail_capture_content(sender_email, message_id, text),
                 'gmail:acct:' + message_id,
                 question_key or ('identity:' + sender_email)),
            )
            conn.commit()
            return conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
        finally:
            conn.close()

    def _row(self, item_id):
        conn = self._conn()
        try:
            return dict(conn.execute('SELECT * FROM inbox_items WHERE id=?', (item_id,)).fetchone())
        finally:
            conn.close()

    def test_get_inbox_has_no_side_effects(self):
        item_id = self._insert_capture('mailer-daemon@googlemail.com', 'noise-read')
        before = self._row(item_id)['status']
        self.client.get('/api/inbox')
        self.client.get('/api/inbox')
        self.assertEqual(self._row(item_id)['status'], before)
        self.assertEqual(before, 'open')
        # 读取不触发自动归档；重新判定才处理。
        conn = self._conn()
        try:
            inbox_reconcile.reconcile_inbox_connection(conn)
        finally:
            conn.close()
        row = self._row(item_id)
        self.assertEqual(row['status'], 'resolved')
        self.assertEqual(row['resolution_source'], 'auto')
        self.assertEqual(row['resolution_reason'], 'inbound_noise')

    def test_same_sender_evidence_groups_into_one_question(self):
        self._insert_capture('buyer@texfire.test', 'm1')
        self._insert_capture('buyer@texfire.test', 'm2', text='Second message')
        payload = self.client.get('/api/inbox').get_json()
        questions = payload['questions']
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]['kind'], 'identity')
        self.assertEqual(len(questions[0]['evidence']), 2)
        self.assertEqual(payload['counts']['questions'], 1)

    def test_archive_resolves_every_evidence_of_the_question(self):
        first = self._insert_capture('buyer@texfire.test', 'm1')
        second = self._insert_capture('buyer@texfire.test', 'm2')
        response = self.client.post('/api/inbox/archive', json={
            'dedupe_key': 'gmail:acct:m1', 'item_type': 'gmail_capture'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._row(first)['status'], 'archived')
        self.assertEqual(self._row(second)['status'], 'archived')

    def test_later_match_auto_resolves_capture(self):
        item_id = self._insert_capture('buyer@texfire.test', 'm-later')
        conn = self._conn()
        try:
            conn.execute(
                '''INSERT INTO gmail_message_states
                   (provider_message_id, match_status, updated_at)
                   VALUES ('m-later', 'matched', '2026-09-18 10:00:00')''')
            conn.commit()
            inbox_reconcile.reconcile_inbox_connection(conn)
        finally:
            conn.close()
        row = self._row(item_id)
        self.assertEqual(row['status'], 'resolved')
        self.assertEqual(row['resolution_reason'], 'later_fact')
        self.assertEqual(row['resolution_source'], 'auto')

    def test_identity_review_decision_closes_without_business_action(self):
        conn = self._conn()
        try:
            conn.execute(
                '''INSERT INTO inbox_items
                   (item_type, customer_id, title, content, dedupe_key, status, created_at,
                    question_kind, question_key, source_type)
                   VALUES ('sela_identity_review', NULL, 'sela 身份待确认', ?,
                           'sela:prospect-review:p1', 'open', '2026-09-18 09:00:00',
                           'identity_review', 'sela:prospect-review:p1', 'sela')''',
                (json.dumps({'source_id': 'p1', 'company': 'Acrílicos',
                             'reason': 'MULTIPLE_TROSA_MATCHES'}),),
            )
            conn.commit()
            item_id = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
        finally:
            conn.close()
        response = self.client.post('/api/inbox/%d/decide' % item_id,
                                    json={'decision': 'different', 'note': '不同主体'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        row = self._row(item_id)
        self.assertEqual(row['status'], 'resolved')
        self.assertEqual(row['resolution_source'], 'human')
        self.assertEqual(row['resolution_reason'], 'identity_different')
        # 关闭问题不等于执行业务动作：没有写出任何沟通事实。
        conn = self._conn()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM follow_up_logs').fetchone()[0], 0)
        finally:
            conn.close()

    def test_retired_types_are_never_questions(self):
        conn = self._conn()
        try:
            conn.execute(
                '''INSERT INTO inbox_items (item_type, title, content, dedupe_key, status, created_at)
                   VALUES ('sela_proposal', '旧提案', '{}', 'old:proposal', 'open', '2026-09-01 09:00:00')''')
            conn.commit()
        finally:
            conn.close()
        payload = self.client.get('/api/inbox').get_json()
        self.assertEqual(payload['counts']['all'], 0)

    def test_self_resolving_identity_review_reasons_auto_close(self):
        conn = self._conn()
        try:
            for source_id, reason in (('p1', 'TROSA_REVISION_CONFLICT'),
                                      ('p2', 'CUSTOMER_ALREADY_LINKED')):
                conn.execute(
                    '''INSERT INTO inbox_items
                       (item_type, title, content, dedupe_key, status, created_at,
                        question_kind, question_key, source_type)
                       VALUES ('sela_identity_review', 'sela 身份待确认', ?,
                               ?, 'open', '2026-09-18 09:00:00',
                               'identity_review', ?, 'sela')''',
                    (json.dumps({'source_id': source_id, 'reason': reason}),
                     'sela:prospect-review:' + source_id,
                     'sela:prospect-review:' + source_id),
                )
            conn.commit()
            stats = inbox_reconcile.reconcile_inbox_connection(conn)
        finally:
            conn.close()
        self.assertEqual(stats['self_resolved'], 2)
        self.assertEqual(self.client.get('/api/inbox').get_json()['counts']['all'], 0)

    def test_multiple_matches_identity_review_stays(self):
        conn = self._conn()
        try:
            conn.execute(
                '''INSERT INTO inbox_items
                   (item_type, title, content, dedupe_key, status, created_at,
                    question_kind, question_key, source_type)
                   VALUES ('sela_identity_review', 'sela 身份待确认', ?,
                           'sela:prospect-review:p3', 'open', '2026-09-18 09:00:00',
                           'identity_review', 'sela:prospect-review:p3', 'sela')''',
                (json.dumps({'source_id': 'p3', 'reason': 'MULTIPLE_TROSA_MATCHES'}),),
            )
            conn.commit()
        finally:
            conn.close()
        payload = self.client.get('/api/inbox').get_json()
        self.assertEqual(payload['counts']['all'], 1)
        self.assertEqual(payload['questions'][0]['kind'], 'identity_review')


    def _insert_customer_with_contact(self, email='old@example.com'):
        conn = self._conn()
        try:
            conn.execute("INSERT INTO customers (name, company, country) VALUES ('CW Plastic', 'CW Plastic', 'UK')")
            customer_id = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
            conn.execute('INSERT INTO contacts (customer_id, name, email, is_primary) VALUES (?, ?, ?, 1)',
                         (customer_id, 'Buyer', email))
            contact_id = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
            conn.commit()
            return customer_id, contact_id
        finally:
            conn.close()

    def _insert_question(self, item_type, question_kind, content, customer_id=None,
                         title='待处理', dedupe_key='sela:question:1', request_json=''):
        conn = self._conn()
        try:
            conn.execute(
                '''INSERT INTO inbox_items
                   (item_type, customer_id, title, content, dedupe_key, status, created_at,
                    question_kind, question_key, source_type, request_json)
                   VALUES (?, ?, ?, ?, ?, 'open', '2026-09-18 09:00:00', ?, ?, 'sela', ?)''',
                (item_type, customer_id, title, content, dedupe_key, question_kind, dedupe_key, request_json))
            conn.commit()
            return conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
        finally:
            conn.close()

    def test_choice_fields_expose_human_labels(self):
        request = {
            'kind': 'DECISION', 'decision': {
                'question': '优先哪个产品线？', 'options': ['板材', '展示架'], 'recommended': '板材',
            },
        }
        self._insert_question('sela_agent_request', 'approval',
                              '公司：CW Plastic\n类型：DECISION\n优先级：AMBER\n\n正文\n\n建议：板材',
                              request_json=json.dumps(request, ensure_ascii=False))
        question = self.client.get('/api/inbox').get_json()['questions'][0]
        self.assertEqual(question['kind'], 'sela_request')
        decision = next(field for field in question['response_schema']['fields'] if field['key'] == 'selected_option')
        self.assertEqual(decision['input_type'], 'choice')
        self.assertEqual([choice['value'] for choice in decision['choices']], ['板材', '展示架'])

    def test_sela_agent_request_evidence_is_structured(self):
        self._insert_question(
            'sela_agent_request', 'approval',
            '公司：CW Plastic\n类型：DATA_CONFLICT\n优先级：AMBER\n\n官网邮箱冲突。\n\n建议：把邮箱改为 info@cwplastic.co.uk')
        evidence = self.client.get('/api/inbox').get_json()['questions'][0]['evidence'][0]
        structured = evidence['structured']
        self.assertEqual(structured['company'], 'CW Plastic')
        self.assertEqual(structured['kind'], 'DATA_CONFLICT')
        self.assertEqual(structured['severity'], 'AMBER')
        self.assertIn('info@cwplastic.co.uk', structured['proposal'])
        self.assertIn('官网邮箱冲突', structured['context'])
        self.assertEqual(structured['fields'], [])

    def test_sela_json_context_becomes_labeled_fields(self):
        payload = json.dumps({
            'action': 'send_first_outreach',
            'policy': {
                'decision': 'require_confirmation',
                'rule_id': 'POL-007',
                'reason': '邮箱尚未核验，需要人工确认',
                'facts_hash': 'ee38efb655a45a5afca7f3496c194c5d',
                'audit': {'source_id': 'auto-au-boomart-20260916', 'company': 'Boomart'},
            },
            'email': 'plastics@boomart.com.au',
            'subject': 'Acrylic sheet supply',
        }, ensure_ascii=False)
        self._insert_question(
            'sela_agent_request', 'approval',
            '公司：Boomart\n类型：SEND_APPROVAL\n优先级：AMBER\n\n' + payload + '\n\n建议：Hello Boomart team,')
        structured = self.client.get('/api/inbox').get_json()['questions'][0]['evidence'][0]['structured']
        fields = {row['key']: row for row in structured['fields']}
        self.assertEqual(fields['action']['label'], '动作')
        self.assertEqual(fields['action']['value'], '发送首封开发信')
        self.assertEqual(fields['email']['label'], '收件人')
        self.assertEqual(fields['email']['value'], 'plastics@boomart.com.au')
        self.assertEqual(fields['subject']['label'], '主题')
        self.assertEqual(fields['policy.reason']['label'], '判定依据')
        self.assertEqual(fields['policy.decision']['value'], '需要人工确认')
        # Raw context is still returned for compatibility, but the human panel
        # must not surface technical audit/hash rows.
        self.assertIn('send_first_outreach', structured['context'])
        self.assertTrue(all('facts_hash' not in row['key'] for row in structured['fields']))
        self.assertFalse(any('audit' in row['key'] for row in structured['fields']))

    def test_sela_request_effects_state_answer_does_not_start_sela(self):
        self._insert_question('sela_agent_request', 'approval', '类型：FACT_GAP')
        question = self.client.get('/api/inbox').get_json()['questions'][0]
        self.assertEqual(question['kind'], 'sela_request')
        self.assertTrue(any('不会因此自动启动 Sela' in entry for entry in question['will_not_do']))
        self.assertIn('Sela 后续读取', question['why_human'])

    def test_sela_decision_only_accepts_the_options_it_requested(self):
        request = {'kind': 'DECISION', 'decision': {
            'question': '优先哪个产品线？', 'options': ['板材', '展示架'],
        }}
        item_id = self._insert_question(
            'sela_agent_request', 'approval', '类型：DECISION\n\n优先选方向',
            dedupe_key='sela:agent-request:prospect-2:direction',
            request_json=json.dumps(request, ensure_ascii=False),
        )
        question = self.client.get('/api/inbox').get_json()['questions'][0]
        invalid = self.client.post('/api/inbox/questions/%d/respond' % item_id, json={
            'revision': question['revision'], 'answer': {'selected_option': '发邮件'},
            'idempotency_key': 'test-sela-invalid-option',
        })
        self.assertEqual(invalid.status_code, 400)
        valid = self.client.post('/api/inbox/questions/%d/respond' % item_id, json={
            'revision': question['revision'], 'answer': {'selected_option': '板材'},
            'idempotency_key': 'test-sela-valid-option',
        })
        self.assertEqual(valid.status_code, 200, valid.get_data(as_text=True))
        resolved = self.client.get('/api/integrations/sela/needs?status=resolved').get_json()['needs']
        need = next(item for item in resolved if item['trosa_inbox_id'] == item_id)
        self.assertEqual(need['human_response']['selected_option'], '板材')

    def test_retired_send_approval_can_only_be_closed_without_sending(self):
        item_id = self._insert_question(
            'sela_agent_request', 'approval',
            '公司：Boomart\n类型：SEND_APPROVAL\n优先级：AMBER\n\n未核验邮箱要求确认发送。',
            dedupe_key='sela:agent-request:prospect-legacy:send',
        )
        question = self.client.get('/api/inbox').get_json()['questions'][0]
        self.assertTrue(question['response_schema']['retired_send_approval'])
        self.assertEqual(question['response_schema']['fields'], [])
        response = self.client.post('/api/inbox/questions/%d/respond' % item_id, json={
            'revision': question['revision'], 'answer': {},
            'idempotency_key': 'test-retired-send-request',
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIn('没有发送邮件', response.get_json()['next_system_step'])
        row = self._row(item_id)
        self.assertEqual(row['resolution_reason'], 'retired_send_approval')
        self.assertIn('没有发送邮件', row['resolution_note'])

    def test_sela_fact_answer_is_structured_for_next_run_not_written_to_contact(self):
        customer_id, contact_id = self._insert_customer_with_contact('old@example.com')
        request = {
            'kind': 'FACT_GAP', 'session_id': 'session-123',
            'missing_facts': [{'field': 'contact_email', 'label': '确认的联系邮箱',
                               'why': 'Sela 无法从公开来源确认', 'blocking': True}],
            'resume': '补充后继续研究公开资料',
        }
        item_id = self._insert_question('sela_agent_request', 'approval',
                                        '公司：CW Plastic\n类型：FACT_GAP\n\n缺少邮箱',
                                        customer_id=customer_id,
                                        dedupe_key='sela:agent-request:prospect-1:email',
                                        request_json=json.dumps(request, ensure_ascii=False))
        question = self.client.get('/api/inbox').get_json()['questions'][0]
        email_field = next(field for field in question['response_schema']['fields'] if field['key'] == 'fact_0')
        self.assertEqual(email_field['input_type'], 'email')
        response = self.client.post('/api/inbox/questions/%d/respond' % item_id, json={
            'revision': question['revision'],
            'answer': {'fact_0': 'fixed@example.com'},
            'idempotency_key': 'test-sela-fact-answer-1',
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        self.assertFalse(body['sela_handoff']['automatic_run'])
        self.assertEqual(body['sela_handoff']['status'], 'awaiting_agent')
        self.assertEqual(body['sela_handoff']['session_id'], 'session-123')
        resolved = self.client.get('/api/integrations/sela/needs?status=resolved').get_json()['needs']
        need = next(item for item in resolved if item['trosa_inbox_id'] == item_id)
        self.assertEqual(need['human_response']['facts'][0]['field'], 'contact_email')
        self.assertEqual(need['human_response']['facts'][0]['value'], 'fixed@example.com')
        self.assertEqual(need['session_id'], 'session-123')
        conn = self._conn()
        try:
            self.assertEqual(conn.execute('SELECT email FROM contacts WHERE id=?', (contact_id,)).fetchone()['email'],
                             'old@example.com')
            self.assertEqual(conn.execute('SELECT status FROM inbox_items WHERE id=?', (item_id,)).fetchone()['status'],
                             'resolved')
        finally:
            conn.close()


    def test_investigation_conclusion_has_human_choices(self):
        self._insert_question('sela_agent_request', 'investigation_request', '请上传报关单',
                              dedupe_key='sela:inv:1')
        question = self.client.get('/api/inbox').get_json()['questions'][0]
        conclusion = next(field for field in question['response_schema']['fields'] if field['key'] == 'conclusion')
        self.assertEqual(conclusion['input_type'], 'investigation_conclusion')
        self.assertEqual({choice['value'] for choice in conclusion['choices']},
                         {'supported', 'not_supported', 'insufficient'})

    def test_manual_investigation_conclusion_is_recorded(self):
        item_id = self._insert_question('sela_agent_request', 'investigation_request', '请上传报关单',
                                        dedupe_key='sela:inv:2')
        question = self.client.get('/api/inbox').get_json()['questions'][0]
        response = self.client.post('/api/inbox/questions/%d/respond' % item_id, json={
            'revision': question['revision'],
            'answer': {'conclusion': 'insufficient'},
            'idempotency_key': 'test-investigation-1',
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = self._conn()
        try:
            row = conn.execute('SELECT status, resolution_note FROM inbox_items WHERE id=?', (item_id,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row['status'], 'resolved')
        self.assertIn('insufficient', row['resolution_note'])

    def test_generic_fact_request_hides_email_correction(self):
        self._insert_question('customer_reply', 'fact_request', '请补充该客户的供应商账号以便对账。',
                              dedupe_key='fact:plain')
        question = self.client.get('/api/inbox').get_json()['questions'][0]
        keys = [field['key'] for field in question['response_schema']['fields']]
        self.assertNotIn('confirmed_email', keys)
        self.assertIn('answer', keys)

    def test_email_fact_request_offers_correction(self):
        self._insert_question('customer_reply', 'fact_request', '请为该联系人更正邮箱。', dedupe_key='fact:email')
        question = self.client.get('/api/inbox').get_json()['questions'][0]
        keys = [field['key'] for field in question['response_schema']['fields']]
        self.assertIn('confirmed_email', keys)
        self.assertIn('contact_id', keys)


if __name__ == '__main__':
    unittest.main()
