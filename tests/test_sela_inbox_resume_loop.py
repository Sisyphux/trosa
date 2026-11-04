"""End-to-end proof that an Inbox answer changes business facts and drives Sela.

Three real scenarios are exercised through the same HTTP surface Sela uses:

1. FACT_GAP contact email  -> email is written to a real Trosa contact and a
   ``verify_email`` continuation is queued, consumed and completed.
2. DECISION exclusion      -> the prospect is really set to do-not-contact and
   recorded as a business exclusion; Sela must obey it and not ask again.
3. IDENTITY same/different  -> the human judgment becomes a reusable identity
   fact that Trosa resolves against on the next sync.

Plus the continuation state machine: idempotency, one continuation per answer,
and no duplicate execution on retry.
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
import sela_continuation as continuation  # noqa: E402


TOKEN = 'test-sela-loop-service-token'


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_sela_resume_loop_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


def prospect(source_id='loop-prospect-1', **overrides):
    body = {
        'source_id': source_id,
        'company': 'Loop Plastics',
        'country': 'Brazil',
        'website': 'https://loop-plastics.example/',
        'business_type': 'Acrylic sheet fabricator',
        'status': 'READY TO CONTACT',
        'confidence': 'HIGH',
        'reason': 'Public fabrication evidence is present.',
        'source_urls': ['https://loop-plastics.example/about'],
        'evidence': [{'type': 'website', 'text': 'Fabricates acrylic displays.',
                      'source_url': 'https://loop-plastics.example/about'}],
        'contact': {},
        'email': '',
        'outreach_status': '',
        'subject': '',
        'email_draft': '',
        'gmail_draft_id': '',
        'gmail_thread_id': '',
        'sent_at': '',
    }
    body.update(overrides)
    return body


class ContinuationStateMachineTest(unittest.TestCase):
    def test_status_aliases_and_transitions(self):
        self.assertEqual(continuation.normalize_status('waiting_for_sela'), continuation.QUEUED)
        self.assertEqual(continuation.normalize_status('resumed'), continuation.RESUMED)
        self.assertTrue(continuation.is_open('queued'))
        self.assertTrue(continuation.is_terminal('completed'))
        self.assertTrue(continuation.can_transition('queued', 'resumed'))
        self.assertTrue(continuation.can_transition('resumed', 'completed'))
        self.assertTrue(continuation.can_transition('completed', 'completed'))
        self.assertFalse(continuation.can_transition('completed', 'resumed'))
        # A failed continuation keeps its reason and can be requeued.
        self.assertTrue(continuation.can_transition('failed', 'queued'))
        self.assertTrue(continuation.can_transition('needs_review', 'queued'))

    def test_one_continuation_key_per_question_and_answer(self):
        digest = hashlib.sha256(b'answer').hexdigest()
        first = continuation.continuation_key(7, digest)
        self.assertEqual(first, f'inbox:7:{digest}')
        self.assertEqual(continuation.continuation_key(7, digest), first)
        self.assertNotEqual(continuation.continuation_key(8, digest), first)
        self.assertEqual(continuation.continuation_key(7, ''), '')

    def test_build_and_view_round_trip(self):
        digest = hashlib.sha256(b'answer').hexdigest()
        run = continuation.build_resume_run(
            status='waiting_for_sela', answer_sha256=digest, inbox_item_id=3,
            action='verify_email', facts_applied=[{'field': 'contact_email'}],
        )
        self.assertEqual(run['status'], continuation.QUEUED)
        self.assertEqual(run['action'], 'verify_email')
        view = continuation.view(run)
        self.assertTrue(view['waiting_for_sela'])
        self.assertEqual(view['facts_applied'][0]['field'], 'contact_email')
        # Unknown action degrades to the safe default, never a send.
        self.assertEqual(continuation.normalize_action('send_email'), continuation.DEFAULT_ACTION)


class SelaInboxResumeLoopTest(unittest.TestCase):
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
    def headers(self, key='sela-loop:one'):
        return {'Authorization': f'Bearer {TOKEN}', 'X-Idempotency-Key': key}

    def hamid_db(self):
        db.set_db_user('hamid')
        return db.get_db()

    def create_prospect(self, source_id='loop-prospect-1', **overrides):
        body = prospect(source_id, **overrides)
        response = self.client.post(
            '/api/integrations/sela/prospects',
            json={'prospect': body, 'idempotency_key': f'sela-loop:{source_id}:create'},
            headers=self.headers(f'sela-loop:{source_id}:create'),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(payload.get('status'), 'SYNCED', payload)
        return int(payload['trosa_id'])

    def create_need(self, source_id, request, key):
        response = self.client.post(
            '/api/integrations/sela/needs',
            json={'request': {**request, 'source_id': source_id}, 'idempotency_key': key},
            headers=self.headers(key),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return int(response.get_json()['item']['trosa_inbox_id'])

    def login(self):
        self.assertEqual(self.client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)

    def open_question(self, inbox_id):
        question = next(item for item in self.client.get('/api/inbox').get_json()['questions']
                        if int(item['id']) == inbox_id)
        return question

    def answer(self, inbox_id, answer, key):
        question = self.open_question(inbox_id)
        return self.client.post(f'/api/inbox/questions/{inbox_id}/respond', json={
            'revision': question['revision'], 'answer': answer, 'idempotency_key': key,
        })

    def resolved_need(self, inbox_id):
        payload = self.client.get(
            f'/api/integrations/sela/needs?status=resolved&item_id={inbox_id}',
            headers=self.headers(),
        ).get_json()
        return next(item for item in payload['needs'] if item['trosa_inbox_id'] == inbox_id)

    def report(self, inbox_id, answer_hash, status, **extra):
        body = {'status': status, 'answer_sha256': answer_hash}
        body.update(extra)
        return self.client.post(
            f'/api/integrations/sela/needs/{inbox_id}/resume-status', json=body,
            headers=self.headers(),
        )

    def continuations(self, status='queued'):
        return self.client.get(
            f'/api/integrations/sela/continuations?status={status}', headers=self.headers(),
        ).get_json()['continuations']

    def prospect_view(self, source_id):
        payload = self.client.get('/api/integrations/sela/prospects?limit=100', headers=self.headers()).get_json()
        return next(row for row in payload['prospects'] if row['id'] == source_id)

    # -- scenario 1: FACT_GAP email ---------------------------------------
    def test_fact_gap_email_becomes_real_contact_and_verify_continuation(self):
        source_id = 'loop-email-1'
        customer_id = self.create_prospect(source_id)
        need_key = 'sela-loop:email-need'
        inbox_id = self.create_need(source_id, {
            'company': 'Loop Plastics', 'kind': 'FACT_GAP', 'severity': 'AMBER',
            'need': '确认联系人邮箱',
            'missing_facts': [{'field': 'contact_email', 'label': '联系人邮箱', 'why': '公开来源无邮箱'}],
            'resume': '补充后继续研究并准备草稿', 'dedupe_key': need_key,
        }, need_key)
        self.login()

        answered = self.answer(inbox_id, {'fact_0': 'buyer@loop-plastics.example'}, 'loop-email-answer')
        self.assertEqual(answered.status_code, 200, answered.get_data(as_text=True))
        handoff = answered.get_json()['sela_handoff']
        self.assertEqual(handoff['status'], 'queued')
        self.assertEqual(handoff['action'], 'verify_email')
        self.assertTrue(handoff['automatic_run'])
        self.assertTrue(handoff['continuation_key'])

        conn = self.hamid_db()
        try:
            row = conn.execute(
                'SELECT id, email FROM contacts WHERE customer_id=?', (customer_id,),
            ).fetchone()
            self.assertIsNotNone(row, 'email answer must create a real Trosa contact')
            self.assertEqual(row['email'], 'buyer@loop-plastics.example')
        finally:
            conn.close()

        # Sela sees the committed fact and the explicit continuation.
        need = self.resolved_need(inbox_id)
        self.assertEqual(need['continuation']['action'], 'verify_email')
        self.assertEqual(need['continuation']['status'], 'queued')
        self.assertEqual(need['continuation']['facts_applied'][0]['field'], 'contact_email')
        # The existing verification capability can now read the address.
        self.assertEqual(self.prospect_view(source_id)['email'], 'buyer@loop-plastics.example')
        queued = self.continuations()
        self.assertTrue(any(item['inbox_id'] == inbox_id for item in queued))

        answer_hash = self.module._sela_hash(need['human_response'])
        resumed = self.report(inbox_id, answer_hash, 'resumed', run_session_id='sela-run-1')
        self.assertEqual(resumed.status_code, 200, resumed.get_data(as_text=True))
        self.assertEqual(resumed.get_json()['resume_run']['status'], 'resumed')
        completed = self.report(inbox_id, answer_hash, 'completed', run_session_id='sela-run-1',
                                summary='邮箱已核验，继续准备草稿。')
        self.assertEqual(completed.status_code, 200, completed.get_data(as_text=True))
        self.assertEqual(completed.get_json()['resume_run']['status'], 'completed')
        self.assertFalse(any(item['inbox_id'] == inbox_id for item in self.continuations()))
        handoff_state = self.client.get(f'/api/inbox/questions/{inbox_id}/sela-handoff').get_json()
        self.assertEqual(handoff_state['status'], 'completed')

    def test_fact_gap_email_answer_is_idempotent_and_writes_once(self):
        source_id = 'loop-email-idem'
        customer_id = self.create_prospect(source_id)
        need_key = 'sela-loop:email-idem-need'
        inbox_id = self.create_need(source_id, {
            'company': 'Loop Plastics', 'kind': 'FACT_GAP',
            'need': '确认联系人邮箱',
            'missing_facts': [{'field': 'contact_email', 'label': '联系人邮箱', 'why': '待确认'}],
            'dedupe_key': need_key,
        }, need_key)
        self.login()
        question = self.open_question(inbox_id)
        body = {'revision': question['revision'], 'answer': {'fact_0': 'buyer@loop-plastics.example'},
                'idempotency_key': 'loop-email-idem-answer'}
        first = self.client.post(f'/api/inbox/questions/{inbox_id}/respond', json=body)
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        # The same idempotency key replays the stored response, not a second write.
        replay = self.client.post(f'/api/inbox/questions/{inbox_id}/respond', json=body)
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(replay.get_json(), first.get_json())
        conn = self.hamid_db()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE customer_id=? AND lower(trim(email))=?",
                (customer_id, 'buyer@loop-plastics.example'),
            ).fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            conn.close()

    # -- scenario 2: DECISION exclusion -----------------------------------
    def test_decision_exclusion_changes_business_state_and_is_not_reasked(self):
        source_id = 'loop-exclude-1'
        customer_id = self.create_prospect(source_id)
        need_key = 'sela-loop:exclude-need'
        inbox_id = self.create_need(source_id, {
            'company': 'Loop Plastics', 'kind': 'DECISION', 'severity': 'AMBER',
            'need': '确认加入排除',
            'decision': {'question': '是否加入排除 / DNC？',
                         'options': ['加入排除 / DNC', '不加入'],
                         'recommended': '加入排除 / DNC'},
            'resume_action': 'resolve_exclusion', 'resume_decision': 'exclude',
            'resume': '人工决定后 Sela 停止联系该主体。', 'dedupe_key': need_key,
        }, need_key)
        self.login()

        answered = self.answer(inbox_id, {'selected_option': '加入排除 / DNC'}, 'loop-exclude-answer')
        self.assertEqual(answered.status_code, 200, answered.get_data(as_text=True))
        handoff = answered.get_json()['sela_handoff']
        self.assertEqual(handoff['status'], 'queued')
        self.assertEqual(handoff['action'], 'resolve_exclusion')

        # Real business state: the prospect is do-not-contact and recorded as an
        # active business exclusion, so Sela's own snapshot and policy obey it.
        view = self.prospect_view(source_id)
        self.assertTrue(view['do_not_contact'])
        records = self.client.get('/api/integrations/sela/exclusions', headers=self.headers()).get_json()['records']
        self.assertTrue(any(record.get('source_id') == source_id for record in records))

        conn = self.hamid_db()
        try:
            profile = conn.execute(
                'SELECT contact_permission FROM agent_prospect_profiles WHERE source_id=?', (source_id,),
            ).fetchone()
            self.assertEqual(profile['contact_permission'], 'do_not_contact')
        finally:
            conn.close()

        # Sela consumes the decision and reports back.
        need = self.resolved_need(inbox_id)
        answer_hash = self.module._sela_hash(need['human_response'])
        resumed = self.report(inbox_id, answer_hash, 'resumed', run_session_id='sela-run-exclude')
        self.assertEqual(resumed.get_json()['resume_run']['status'], 'resumed')
        completed = self.report(inbox_id, answer_hash, 'completed', run_session_id='sela-run-exclude',
                                summary='已按人工决定停止联系。')
        self.assertEqual(completed.get_json()['resume_run']['status'], 'completed')

        # No new open question for the same source: Sela must not ask again.
        open_needs = self.client.get('/api/integrations/sela/needs?status=open', headers=self.headers()).get_json()['needs']
        self.assertFalse(any(str(item.get('candidate_id') or '') == source_id for item in open_needs))

    # -- scenario 3: IDENTITY same / different ----------------------------
    def _insert_identity_review(self, source_id, company='Loop Plastics'):
        conn = self.hamid_db()
        try:
            content = json.dumps({
                'source_id': source_id, 'company': company,
                'website': 'https://loop-plastics.example/',
                'email': 'buyer@loop-plastics.example',
                'reason': 'MULTIPLE_TROSA_MATCHES',
            }, ensure_ascii=False)
            item_id = self.module._create_inbox_item(
                conn, item_type='sela_identity_review', title='sela Prospect 身份待确认',
                content=content, dedupe_key=f'sela:prospect-review:{source_id}', status='open',
                created_at='2026-09-27 09:00:00',
                **self.module._inbox_question_meta(
                    'sela_identity_review', f'sela:prospect-review:{source_id}', source_id),
            )
            conn.commit()
            return int(item_id)
        finally:
            conn.close()

    def _insert_customer(self, name):
        conn = self.hamid_db()
        try:
            conn.execute('INSERT INTO customers (name, company) VALUES (?, ?)', (name, name))
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.commit()
            return int(customer_id)
        finally:
            conn.close()

    def test_identity_same_records_reusable_fact_and_stops_reasking(self):
        source_id = 'loop-identity-same'
        own_customer_id = self.create_prospect(source_id)
        other_customer_id = self._insert_customer('Other Match Co')
        self.assertNotEqual(own_customer_id, other_customer_id)
        review_id = self._insert_identity_review(source_id)
        self.login()

        answered = self.answer(review_id, {'decision': 'same', 'customer_id': other_customer_id},
                               'loop-identity-same-answer')
        self.assertEqual(answered.status_code, 200, answered.get_data(as_text=True))
        handoff = answered.get_json()['sela_handoff']
        self.assertEqual(handoff['status'], 'queued')
        self.assertEqual(handoff['action'], 'continue_development')

        conn = self.hamid_db()
        try:
            facts = self.module.identity_link.active_facts_for_identifier(
                conn, 'source', f'sela:{source_id}')
            self.assertTrue(facts, 'same decision must persist a reusable identity fact')
            self.assertEqual(int(facts[0]['customer_id']), other_customer_id)
            # The next sync resolves against the recorded fact instead of asking again.
            matches, error = self.module._sela_match_customers(conn, {
                'candidate_id': source_id, 'company': 'Loop Plastics',
                'website': 'https://loop-plastics.example/', 'contact': {}})
            self.assertEqual(error, '')
            self.assertEqual([int(row['id']) for row in matches], [other_customer_id])
            self.assertEqual(matches[0]['matched_by'], ['confirmed_source'])
        finally:
            conn.close()

    def test_identity_different_pins_own_record_and_stops_reasking(self):
        source_id = 'loop-identity-different'
        own_customer_id = self.create_prospect(source_id)
        review_id = self._insert_identity_review(source_id)
        self.login()

        answered = self.answer(review_id, {'decision': 'different'}, 'loop-identity-different-answer')
        self.assertEqual(answered.status_code, 200, answered.get_data(as_text=True))
        self.assertEqual(answered.get_json()['sela_handoff']['action'], 'continue_development')

        conn = self.hamid_db()
        try:
            facts = self.module.identity_link.active_facts_for_identifier(
                conn, 'source', f'sela:{source_id}')
            self.assertTrue(facts)
            self.assertEqual(int(facts[0]['customer_id']), own_customer_id)
            matches, error = self.module._sela_match_customers(conn, {
                'candidate_id': source_id, 'company': 'Loop Plastics',
                'website': 'https://loop-plastics.example/', 'contact': {}})
            self.assertEqual(error, '')
            self.assertEqual([int(row['id']) for row in matches], [own_customer_id])
        finally:
            conn.close()

    def test_identity_different_creates_own_record_when_no_profile(self):
        # An identity review with no Sela profile (e.g. MULTIPLE_TROSA_MATCHES):
        # "different" must create the prospect's own record and pin the source
        # fact to it, so a re-sync resolves deterministically instead of asking
        # the same question again.
        source_id = 'loop-identity-new'
        review_id = self._insert_identity_review(source_id)
        self.login()
        answered = self.answer(review_id, {'decision': 'different'}, 'loop-identity-new-answer')
        self.assertEqual(answered.status_code, 200, answered.get_data(as_text=True))
        conn = self.hamid_db()
        try:
            profile = conn.execute(
                'SELECT customer_id FROM agent_prospect_profiles WHERE source_id=?', (source_id,),
            ).fetchone()
            self.assertIsNotNone(profile, 'different must create the prospect own record')
            facts = self.module.identity_link.active_facts_for_identifier(
                conn, 'source', f'sela:{source_id}')
            self.assertEqual(int(facts[0]['customer_id']), int(profile['customer_id']))
            matches, error = self.module._sela_match_customers(conn, {
                'candidate_id': source_id, 'company': 'Loop Plastics',
                'website': 'https://loop-plastics.example/', 'contact': {}})
            self.assertEqual(error, '')
            self.assertEqual([int(row['id']) for row in matches], [int(profile['customer_id'])])
        finally:
            conn.close()

    # -- continuation state machine over HTTP -----------------------------
    def test_continuation_reports_are_idempotent_and_ordered(self):
        source_id = 'loop-state-1'
        self.create_prospect(source_id)
        need_key = 'sela-loop:state-need'
        inbox_id = self.create_need(source_id, {
            'company': 'Loop Plastics', 'kind': 'FACT_GAP',
            'need': '确认联系人邮箱',
            'missing_facts': [{'field': 'contact_email', 'label': '联系人邮箱', 'why': '待确认'}],
            'dedupe_key': need_key,
        }, need_key)
        self.login()
        self.answer(inbox_id, {'fact_0': 'buyer@loop-plastics.example'}, 'loop-state-answer')
        need = self.resolved_need(inbox_id)
        answer_hash = self.module._sela_hash(need['human_response'])

        # Duplicate "resumed" reports are stale, not double execution.
        first = self.report(inbox_id, answer_hash, 'resumed', run_session_id='run-1')
        self.assertEqual(first.get_json()['resume_run']['status'], 'resumed')
        second = self.report(inbox_id, answer_hash, 'resumed', run_session_id='run-1')
        self.assertTrue(second.get_json().get('stale'))
        # A terminal state cannot be reopened by a late queue receipt.
        done = self.report(inbox_id, answer_hash, 'completed', run_session_id='run-1')
        self.assertEqual(done.get_json()['resume_run']['status'], 'completed')
        late = self.report(inbox_id, answer_hash, 'queued')
        self.assertTrue(late.get_json().get('stale'))
        self.assertEqual(late.get_json()['resume_run']['status'], 'completed')
        # A wrong answer hash is rejected.
        bad = self.report(inbox_id, '0' * 64, 'resumed')
        self.assertEqual(bad.status_code, 409)

    def test_continuation_failure_keeps_reason_for_retry(self):
        source_id = 'loop-fail-1'
        self.create_prospect(source_id)
        need_key = 'sela-loop:fail-need'
        inbox_id = self.create_need(source_id, {
            'company': 'Loop Plastics', 'kind': 'FACT_GAP',
            'need': '确认联系人邮箱',
            'missing_facts': [{'field': 'contact_email', 'label': '联系人邮箱', 'why': '待确认'}],
            'dedupe_key': need_key,
        }, need_key)
        self.login()
        self.answer(inbox_id, {'fact_0': 'buyer@loop-plastics.example'}, 'loop-fail-answer')
        need = self.resolved_need(inbox_id)
        answer_hash = self.module._sela_hash(need['human_response'])
        self.report(inbox_id, answer_hash, 'resumed', run_session_id='run-f')
        failed = self.report(inbox_id, answer_hash, 'failed', run_session_id='run-f',
                             error='邮箱核验超时', error_code='verify_timeout')
        self.assertEqual(failed.status_code, 200, failed.get_data(as_text=True))
        resume_run = failed.get_json()['resume_run']
        self.assertEqual(resume_run['status'], 'failed')
        self.assertEqual(resume_run['error'], '邮箱核验超时')
        self.assertEqual(resume_run['error_code'], 'verify_timeout')
        self.assertGreaterEqual(resume_run['attempt'], 1)
        # A human can requeue a failed continuation; the fact is not lost.
        requeued = self.report(inbox_id, answer_hash, 'queued')
        self.assertEqual(requeued.status_code, 200, requeued.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
