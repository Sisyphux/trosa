"""S9 read tools: read one prospect, read a few by ids, refuse unknown params.

The whole-worklist read is the slow path; S9 gives the Agent a direct way to
read a single prospect or a bounded set.  These tests pin the response shapes,
the ordering, the missing-id report and the "unknown parameter is an error,
never silently ignored" rule.
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

import db


TOKEN = 'test-sela-read-tools-token'


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_sela_read_tools_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


def prospect(source_id, company):
    return {
        'source_id': source_id,
        'company': company,
        'country': 'Germany',
        'website': f'https://{source_id}.example/',
        'status': 'READY TO CONTACT',
        'contact': {'name': 'Buyer', 'title': 'Purchasing', 'email': f'buyer@{source_id}.example'},
        'outreach_status': 'EMAIL_VERIFY',
    }


class SelaProspectReadToolsTest(unittest.TestCase):
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
        self.headers = {'Authorization': f'Bearer {TOKEN}'}

    def tearDown(self):
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    def upsert(self, source_id, company):
        response = self.client.post(
            '/api/integrations/sela/prospects',
            json={'prospect': prospect(source_id, company),
                  'idempotency_key': f'sela-v2:{source_id}:read-tools'},
            headers={**self.headers, 'X-Idempotency-Key': f'sela-v2:{source_id}:read-tools'},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def list_prospects(self):
        response = self.client.get('/api/integrations/sela/prospects?limit=100', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()['prospects']

    # ---- single prospect -------------------------------------------------
    def test_single_prospect_matches_the_list_view(self):
        self.upsert('alpha', 'Alpha GmbH')
        self.upsert('beta', 'Beta GmbH')

        single = self.client.get('/api/integrations/sela/prospects/beta', headers=self.headers)
        self.assertEqual(single.status_code, 200, single.get_json())
        body = single.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(body['prospect_api'], 'sela-v2')
        self.assertEqual(body['prospect']['id'], 'beta')
        listed = {row['id']: row for row in self.list_prospects()}
        self.assertEqual(body['prospect'], listed['beta'])

    def test_single_prospect_unknown_source_id_is_404(self):
        self.upsert('alpha', 'Alpha GmbH')
        response = self.client.get('/api/integrations/sela/prospects/nope', headers=self.headers)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.get_json()['success'])

    # ---- ids -------------------------------------------------------------
    def test_ids_returns_requested_prospects_in_request_order(self):
        for source_id in ('a-one', 'b-two', 'c-three'):
            self.upsert(source_id, source_id.upper())
        response = self.client.get(
            '/api/integrations/sela/prospects?ids=c-three,a-one', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        self.assertEqual([row['id'] for row in body['prospects']], ['c-three', 'a-one'])
        self.assertEqual(body['missing'], [])

    def test_ids_reports_unknown_ids_without_failing(self):
        self.upsert('known', 'Known GmbH')
        response = self.client.get(
            '/api/integrations/sela/prospects?ids=known,ghost', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        self.assertEqual([row['id'] for row in body['prospects']], ['known'])
        self.assertEqual(body['missing'], ['ghost'])

    def test_ids_allows_exactly_one_hundred(self):
        for index in range(100):
            self.upsert(f'prospect-{index}', f'Company {index}')
        ids = ','.join(f'prospect-{index}' for index in range(100))
        response = self.client.get(f'/api/integrations/sela/prospects?ids={ids}', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(len(response.get_json()['prospects']), 100)

    def test_ids_over_one_hundred_is_400(self):
        ids = ','.join(f'prospect-{index}' for index in range(101))
        response = self.client.get(f'/api/integrations/sela/prospects?ids={ids}', headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])

    def test_empty_id_token_is_400(self):
        response = self.client.get('/api/integrations/sela/prospects?ids=a,,b', headers=self.headers)
        self.assertEqual(response.status_code, 400)

    def test_ids_cannot_combine_with_paging(self):
        response = self.client.get('/api/integrations/sela/prospects?ids=a&after=5', headers=self.headers)
        self.assertEqual(response.status_code, 400)

    # ---- unknown parameters ---------------------------------------------
    def test_unknown_list_param_is_400_with_supported_list(self):
        response = self.client.get('/api/integrations/sela/prospects?bogus=1', headers=self.headers)
        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        self.assertFalse(body['success'])
        self.assertIn('ids', body['supported_params'])
        self.assertIn('after', body['supported_params'])

    def test_unknown_single_param_is_400(self):
        self.upsert('alpha', 'Alpha GmbH')
        response = self.client.get('/api/integrations/sela/prospects/alpha?bogus=1', headers=self.headers)
        self.assertEqual(response.status_code, 400)

    # ---- auth ------------------------------------------------------------
    def test_read_routes_require_the_service_token(self):
        self.upsert('alpha', 'Alpha GmbH')
        self.assertEqual(self.client.get('/api/integrations/sela/prospects/alpha').status_code, 401)
        self.assertEqual(self.client.get('/api/integrations/sela/prospects?ids=alpha').status_code, 401)


    # ---- canonical outreach history (T4) --------------------------------
    def hamid_db(self):
        db.set_db_user('hamid')
        return db.get_db()

    def add_outreach(self, customer_id, external_source, external_id, sent_date,
                     reply_status='pending'):
        conn = self.hamid_db()
        try:
            conn.execute(
                '''INSERT INTO outreach_emails
                   (customer_id, subject, content, sent_date, reply_status,
                    external_source, external_id)
                   VALUES (?, 'Subject', 'Body', ?, ?, ?, ?)''',
                (customer_id, sent_date, reply_status, external_source, external_id),
            )
            conn.commit()
        finally:
            conn.close()

    def single(self, source_id):
        response = self.client.get(
            f'/api/integrations/sela/prospects/{source_id}', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()['prospect']

    def test_outreach_history_includes_mail_from_outside_the_sela_source(self):
        created = self.upsert('emailed-before', 'Emailed Before GmbH')
        customer_id = created['trosa_id']
        # Sent before the customer joined the pool: the mail lives on the
        # customer record under another source, not on the sela source row.
        self.add_outreach(customer_id, 'legacy-sela', 'old-1', '2026-08-13')

        view = self.single('emailed-before')
        self.assertEqual(view['outreach'], {
            'first_sent_at': '2026-08-13', 'last_sent_at': '2026-08-13',
            'sent_count': 1, 'last_status': 'pending',
        })
        self.assertFalse(view['bounced'])
        self.assertFalse(view['replied'])

    def test_imported_draft_without_a_date_is_not_a_send(self):
        created = self.upsert('draft-only', 'Draft Only GmbH')
        self.add_outreach(created['trosa_id'], 'sela', 'draft-only', '')

        view = self.single('draft-only')
        self.assertEqual(view['outreach']['sent_count'], 0)
        self.assertEqual(view['outreach']['first_sent_at'], '')
        self.assertEqual(view['outreach']['last_sent_at'], '')

    def test_outreach_history_bounds_count_and_latest_status_across_sources(self):
        created = self.upsert('many-sends', 'Many Sends GmbH')
        customer_id = created['trosa_id']
        self.add_outreach(customer_id, 'legacy-sela', 'm-1', '2026-08-13', 'replied')
        self.add_outreach(customer_id, 'sela', 'many-sends', '2026-09-01', 'pending')
        self.add_outreach(customer_id, 'manual', 'm-2', '2026-09-20', 'bounced')

        view = self.single('many-sends')
        self.assertEqual(view['outreach']['first_sent_at'], '2026-08-13')
        self.assertEqual(view['outreach']['last_sent_at'], '2026-09-20')
        self.assertEqual(view['outreach']['sent_count'], 3)
        self.assertEqual(view['outreach']['last_status'], 'bounced')
        self.assertTrue(view['bounced'])

    def test_replied_fact_follows_the_latest_status(self):
        created = self.upsert('replied-latest', 'Replied Latest GmbH')
        self.add_outreach(created['trosa_id'], 'legacy-sela', 'r-1', '2026-09-05', 'replied')

        view = self.single('replied-latest')
        self.assertEqual(view['outreach']['last_status'], 'replied')
        self.assertTrue(view['replied'])

    def test_batched_page_and_single_read_agree_on_outreach_history(self):
        for index in range(3):
            created = self.upsert(f'page-{index}', f'Page {index} GmbH')
            self.add_outreach(created['trosa_id'], 'legacy-sela', f'p-{index}', '2026-08-13')
        listed = {row['id']: row for row in self.list_prospects()}
        for index in range(3):
            self.assertEqual(listed[f'page-{index}'], self.single(f'page-{index}'))

    # ---- filters (T2) ----------------------------------------------------
    def upsert_body(self, body):
        key = f"sela-v2:{body['source_id']}:read-tools"
        response = self.client.post(
            '/api/integrations/sela/prospects',
            json={'prospect': body, 'idempotency_key': key},
            headers={**self.headers, 'X-Idempotency-Key': key},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def upsert_without_email(self, source_id, company):
        return self.upsert_body({
            'source_id': source_id, 'company': company, 'country': 'Germany',
            'website': f'https://{source_id}.example/', 'status': 'READY TO CONTACT',
            'contact': {'name': 'Buyer', 'title': 'Purchasing'},
            'outreach_status': 'CONTACT_NEEDED',
        })

    def filtered(self, query):
        response = self.client.get(
            '/api/integrations/sela/prospects?' + query, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        return body, [row['id'] for row in body['prospects']]

    def add_verification(self, email, status='likely_deliverable'):
        conn = self.hamid_db()
        try:
            conn.execute(
                'INSERT INTO email_verifications (email, deliverability_status) VALUES (?, ?)',
                (email, status),
            )
            conn.commit()
        finally:
            conn.close()

    def set_permission(self, source_id, permission):
        conn = self.hamid_db()
        try:
            conn.execute(
                'UPDATE agent_prospect_profiles SET contact_permission=? WHERE source_id=?',
                (permission, source_id),
            )
            conn.commit()
        finally:
            conn.close()

    def test_needs_email_returns_prospects_without_a_contact_email(self):
        self.upsert('has-email', 'Has Email GmbH')
        self.upsert_without_email('no-email', 'No Email GmbH')

        _, ids = self.filtered('needs=email')
        self.assertEqual(ids, ['no-email'])

    def test_needs_verify_tracks_the_email_route_status(self):
        created = self.upsert('to-verify', 'To Verify GmbH')

        _, ids = self.filtered('needs=verify')
        self.assertEqual(ids, ['to-verify'])

        self.add_verification(f'buyer@to-verify.example')
        _, ids = self.filtered('needs=verify')
        self.assertNotIn('to-verify', ids)

    def test_needs_first_touch_needs_a_verified_unsent_mailbox(self):
        self.upsert('unsent', 'Unsent GmbH')
        self.add_verification('buyer@unsent.example')

        _, ids = self.filtered('needs=first_touch')
        self.assertEqual(ids, ['unsent'])

        created = self.upsert('already-sent', 'Already Sent GmbH')
        self.add_verification('buyer@already-sent.example')
        self.add_outreach(created['trosa_id'], 'legacy-sela', 'sent-1', '2026-08-13')

        _, ids = self.filtered('needs=first_touch')
        self.assertEqual(ids, ['unsent'])
        self.assertNotIn('already-sent', ids)

    def test_needs_follow_up_uses_the_cadence_and_the_due_date(self):
        created = self.upsert('due-now', 'Due Now GmbH')
        self.add_verification('buyer@due-now.example')
        self.add_outreach(created['trosa_id'], 'legacy-sela', 'sent-1', '2026-09-14')

        # First touch 30 days ago with no later send -> a milestone is due.
        _, ids = self.filtered('needs=follow_up&due_on_or_before=2026-10-14')
        self.assertEqual(ids, ['due-now'])

        # Same day as the first touch: no milestone has elapsed yet.
        _, ids = self.filtered('needs=follow_up&due_on_or_before=2026-09-14')
        self.assertEqual(ids, [])

    def test_needs_follow_up_skips_bounced_mail(self):
        created = self.upsert('bounced-one', 'Bounced One GmbH')
        self.add_verification('buyer@bounced-one.example')
        self.add_outreach(created['trosa_id'], 'legacy-sela', 'b-1', '2026-09-14', 'bounced')

        _, ids = self.filtered('needs=follow_up&due_on_or_before=2026-10-14')
        self.assertNotIn('bounced-one', ids)

    def test_needs_skip_a_suppressed_prospect(self):
        self.upsert('stopped', 'Stopped GmbH')
        self.set_permission('stopped', 'do_not_contact')

        _, ids = self.filtered('needs=verify')
        self.assertNotIn('stopped', ids)

    def test_updated_after_narrows_without_needs(self):
        self.upsert('one', 'One GmbH')
        self.upsert('two', 'Two GmbH')

        _, ids = self.filtered('updated_after=2999-01-01 00:00:00')
        self.assertEqual(ids, [])
        _, ids = self.filtered('updated_after=2000-01-01 00:00:00')
        self.assertEqual(sorted(ids), ['one', 'two'])

    def test_fields_projects_the_returned_keys(self):
        self.upsert('projected', 'Projected GmbH')

        response = self.client.get(
            '/api/integrations/sela/prospects/projected?fields=id,company,outreach',
            headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(
            set(response.get_json()['prospect']), {'id', 'company', 'outreach'})

        body, _ = self.filtered('fields=id,outreach_status')
        self.assertEqual(body['prospects'][0], {'id': 'projected', 'outreach_status': 'EMAIL_VERIFY'})

    def test_unknown_field_is_400_with_supported_fields(self):
        response = self.client.get(
            '/api/integrations/sela/prospects?fields=id,bogus', headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn('supported_fields', response.get_json())

    def test_unknown_need_is_400(self):
        response = self.client.get(
            '/api/integrations/sela/prospects?needs=bogus', headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn('supported_needs', response.get_json())

    def test_due_on_or_before_requires_the_follow_up_need(self):
        response = self.client.get(
            '/api/integrations/sela/prospects?due_on_or_before=2026-10-14', headers=self.headers)
        self.assertEqual(response.status_code, 400)

        response = self.client.get(
            '/api/integrations/sela/prospects?needs=follow_up&due_on_or_before=not-a-date',
            headers=self.headers)
        self.assertEqual(response.status_code, 400)

    def test_ids_cannot_combine_with_filters(self):
        self.upsert('combo', 'Combo GmbH')
        response = self.client.get(
            '/api/integrations/sela/prospects?ids=combo&needs=verify', headers=self.headers)
        self.assertEqual(response.status_code, 400)

    # ---- change detection (T9) -------------------------------------------
    def list_response(self, query=''):
        response = self.client.get(
            '/api/integrations/sela/prospects' + (('?' + query) if query else ''),
            headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response

    def test_list_reports_total_and_a_data_version(self):
        self.upsert('one', 'One GmbH')
        self.upsert('two', 'Two GmbH')

        response = self.list_response('limit=100')
        body = response.get_json()
        self.assertEqual(body['total'], 2)
        version = body['data_version']
        self.assertTrue(version)
        self.assertEqual(response.headers['ETag'], '"' + version + '"')
        self.assertEqual(response.headers['X-Trosa-Sync-Version'], version)

    def test_matching_etag_returns_304(self):
        self.upsert('cached', 'Cached GmbH')
        version = self.list_response('limit=100').get_json()['data_version']

        quoted = self.client.get('/api/integrations/sela/prospects?limit=100',
                                 headers={**self.headers, 'If-None-Match': '"' + version + '"'})
        self.assertEqual(quoted.status_code, 304)
        self.assertEqual(quoted.get_data(), b'')

        raw = self.client.get('/api/integrations/sela/prospects?limit=100',
                              headers={**self.headers, 'If-None-Match': version})
        self.assertEqual(raw.status_code, 304)

    def test_data_version_moves_when_the_feed_changes(self):
        self.upsert('first', 'First GmbH')
        version = self.list_response('limit=100').get_json()['data_version']

        self.upsert('second', 'Second GmbH')
        stale = self.client.get('/api/integrations/sela/prospects?limit=100',
                                headers={**self.headers, 'If-None-Match': '"' + version + '"'})
        self.assertEqual(stale.status_code, 200)
        self.assertNotEqual(stale.get_json()['data_version'], version)

    def test_ids_and_single_reads_carry_the_version(self):
        self.upsert('alpha', 'Alpha GmbH')

        ids_body = self.client.get(
            '/api/integrations/sela/prospects?ids=alpha', headers=self.headers).get_json()
        self.assertTrue(ids_body['data_version'])

        single = self.client.get(
            '/api/integrations/sela/prospects/alpha', headers=self.headers)
        self.assertTrue(single.get_json()['data_version'])

    def test_single_read_honours_if_none_match(self):
        self.upsert('alpha', 'Alpha GmbH')
        response = self.client.get('/api/integrations/sela/prospects/alpha', headers=self.headers)
        etag = response.headers['ETag']

        cached = self.client.get('/api/integrations/sela/prospects/alpha',
                                 headers={**self.headers, 'If-None-Match': etag})
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.get_data(), b'')


if __name__ == '__main__':
    unittest.main()
