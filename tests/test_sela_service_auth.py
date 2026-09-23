import hashlib
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

import sys

sys.path.insert(0, str(ROOT))

import db


SERVICE_TOKEN = 'trosa_sela_test_service_token'


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_sela_service_auth_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


class SelaServiceAuthTest(unittest.TestCase):
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
                'token_sha256': hashlib.sha256(SERVICE_TOKEN.encode('utf-8')).hexdigest(),
                'enabled': True,
                'user': 'hamid',
            })),
        )
        conn.commit()
        conn.close()
        self.module = load_app()

    def tearDown(self):
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    def test_service_token_has_only_hamid_customer_operations(self):
        service = self.module.app.test_client()
        service_headers = {'Authorization': f'Bearer {SERVICE_TOKEN}'}
        self.assertEqual(
            service.get('/api/integrations/sela/health', headers=service_headers).status_code,
            200,
        )
        self.assertEqual(service.get('/api/customers', headers=service_headers).status_code, 200)
        self.assertEqual(service.get('/api/inbox', headers=service_headers).status_code, 200)
        self.assertEqual(service.get('/api/integrations/sela/needs', headers=service_headers).status_code, 200)
        self.assertEqual(service.post(
            '/api/integrations/sela/inbox-captures',
            json={'message': {'id': 'auth-capture-1', 'body': 'test'}},
            headers=service_headers,
        ).status_code, 200)
        self.assertEqual(service.post('/api/customers', json={'company': 'Sela Customer'}, headers=service_headers).status_code, 201)
        self.assertEqual(service.get('/api/agent-gateway/tokens', headers=service_headers).status_code, 401)
        self.assertEqual(
            service.post('/api/integrations/sela/token', headers=service_headers).status_code,
            401,
        )

    def test_service_token_can_complete_only_the_sela_inbox_resume_handoff(self):
        service = self.module.app.test_client()
        service_headers = {'Authorization': f'Bearer {SERVICE_TOKEN}'}
        source_id = 'service-resume-auth-1'
        company = 'Service Resume Auth Plastics'
        prospect_key = 'service-resume-auth-prospect'
        created = service.post(
            '/api/integrations/sela/prospects',
            json={'prospect': {
                'source_id': source_id,
                'company': company,
                'website': 'https://service-resume.example/',
                'country': 'US',
                'business_type': 'Acrylic sheet fabricator',
                'status': 'READY TO CONTACT',
                'research_status': 'VERIFIED',
                'confidence': 'HIGH',
                'reason': 'Public company information is ready for follow-up.',
                'source_urls': ['https://service-resume.example/about'],
                'evidence': [{'type': 'website', 'text': 'Fabricates acrylic displays.',
                              'source_url': 'https://service-resume.example/about'}],
                'outreach_status': 'CONTACT_NEEDED',
            }, 'idempotency_key': prospect_key},
            headers={**service_headers, 'X-Idempotency-Key': prospect_key},
        )
        self.assertEqual(created.status_code, 200, created.get_json())
        customer_id = created.get_json()['trosa_id']

        need_key = 'service-resume-auth-need'
        need = service.post('/api/integrations/sela/needs', json={
            'request': {
                'source_id': source_id,
                'candidate_id': source_id,
                'customer_id': customer_id,
                'company': company,
                'kind': 'DECISION',
                'severity': 'AMBER',
                'need': 'Choose whether public research should continue.',
                'decision': {
                    'question': 'What should Sela do next?',
                    'options': ['Research public sources', 'Stop for now'],
                    'recommended': 'Research public sources',
                },
                'resume': 'Continue public research and save the evidence.',
                'dedupe_key': need_key,
            },
            'idempotency_key': need_key,
        }, headers=service_headers)
        self.assertEqual(need.status_code, 200, need.get_json())
        inbox_id = int(need.get_json()['item']['trosa_inbox_id'])

        session = self.module.app.test_client()
        self.assertEqual(session.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)
        question = next(row for row in session.get('/api/inbox').get_json()['questions']
                        if int(row.get('primary_item_id') or 0) == inbox_id)
        answered = session.post(f'/api/inbox/questions/{inbox_id}/respond', json={
            'revision': question['revision'],
            'answer': {'selected_option': 'Research public sources'},
            'idempotency_key': 'service-resume-auth-answer',
        })
        self.assertEqual(answered.status_code, 200, answered.get_json())
        self.assertTrue(answered.get_json()['sela_handoff']['automatic_run'])

        resolved = service.get('/api/integrations/sela/needs?status=resolved', headers=service_headers)
        self.assertEqual(resolved.status_code, 200, resolved.get_json())
        resolved_need = next(row for row in resolved.get_json()['needs'] if row['trosa_inbox_id'] == inbox_id)
        answer_hash = self.module._sela_hash(resolved_need['human_response'])
        running = service.post(f'/api/integrations/sela/needs/{inbox_id}/resume-status', json={
            'status': 'running', 'answer_sha256': answer_hash,
            'run_session_id': 'service-resume-auth-run', 'summary': '',
        }, headers=service_headers)
        self.assertEqual(running.status_code, 200, running.get_json())

        prospects = service.get('/api/integrations/sela/prospects?limit=100', headers=service_headers)
        self.assertEqual(prospects.status_code, 200, prospects.get_json())
        target = next(row for row in prospects.get_json()['prospects'] if row['id'] == source_id)
        resume_key = f'sela:auto-resume:{inbox_id}:{answer_hash}'
        saved = service.post(f'/api/integrations/sela/prospects/{source_id}/resume', json={
            'action': 'research',
            'source_id': source_id,
            'inbox_id': inbox_id,
            'answer_sha256': answer_hash,
            'expected_revision': target['trosa_revision'],
            'research': {
                'research_reason': 'Local service-auth regression research result.',
                'qualification_method': 'Public company website',
                'qualification_reason': 'The public about page confirms acrylic fabrication.',
                'evidence': [{'url': 'https://service-resume.example/about',
                              'quote': 'We fabricate acrylic displays.'}],
                'source_urls': ['https://service-resume.example/about'],
            },
            'idempotency_key': resume_key,
        }, headers={**service_headers, 'X-Idempotency-Key': resume_key})
        self.assertEqual(saved.status_code, 200, saved.get_json())
        self.assertEqual(saved.get_json()['status'], 'SYNCED')

        completed = service.post(f'/api/integrations/sela/needs/{inbox_id}/resume-status', json={
            'status': 'completed', 'answer_sha256': answer_hash,
            'run_session_id': 'service-resume-auth-run',
            'summary': 'Public research was saved to the existing Prospect.',
        }, headers=service_headers)
        self.assertEqual(completed.status_code, 200, completed.get_json())
        self.assertEqual(completed.get_json()['resume_run']['status'], 'completed')

        confirmed = service.get('/api/integrations/sela/prospects?limit=100', headers=service_headers)
        saved_prospect = next(row for row in confirmed.get_json()['prospects'] if row['id'] == source_id)
        self.assertEqual(saved_prospect['research_reason'], 'Local service-auth regression research result.')
        self.assertIn('https://service-resume.example/about', saved_prospect['source_urls'])

    def test_issuance_stores_only_digest_and_replaces_previous_service_token(self):
        session = self.module.app.test_client()
        self.assertEqual(session.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)
        response = session.post('/api/integrations/sela/token')
        self.assertEqual(response.status_code, 200, response.get_json())
        token = response.get_json()['token']
        self.assertTrue(token.startswith('trosa_sela_'))

        conn = sqlite3.connect(os.path.join(db.DB_DIR, 'system.db'))
        try:
            stored = conn.execute(
                'SELECT value FROM app_settings WHERE key=?',
                ('integration_token:sela:hamid',),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertNotIn(token, stored)
        self.assertIn(hashlib.sha256(token.encode('utf-8')).hexdigest(), stored)

        service = self.module.app.test_client()
        self.assertEqual(
            service.get(
                '/api/integrations/sela/health',
                headers={'Authorization': f'Bearer {token}'},
            ).status_code,
            200,
        )


if __name__ == '__main__':
    unittest.main()
