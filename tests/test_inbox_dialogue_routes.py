"""Route/auth tests for the Inbox dialogue backend (contract §3.1).

Human routes must be ``@login_required``; the sela service token must only
reach the exact whitelisted sela paths, and personal gateway tokens must never
reach the human conversation routes.
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

SERVICE_TOKEN = 'trosa_sela_dialogue_test_token'


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_dialogue_routes_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


class InboxDialogueRoutesTest(unittest.TestCase):
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
        self.service = self.module.app.test_client()
        self.service_headers = {'Authorization': f'Bearer {SERVICE_TOKEN}'}

    def tearDown(self):
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    def _create_thread(self, text='sela 提问'):
        response = self.service.post(
            '/api/integrations/sela/threads',
            json={'title': '路由测试', 'text': text, 'subject': 'prospect:route'},
            headers=self.service_headers,
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()['thread']['id']

    def test_sela_routes_require_service_token(self):
        self.assertEqual(
            self.service.post('/api/integrations/sela/threads',
                              json={'title': 'x', 'text': 'y'}).status_code, 401)
        thread_id = self._create_thread()
        self.assertEqual(
            self.service.post(f'/api/integrations/sela/threads/{thread_id}/messages',
                              json={'text': 'z'}).status_code, 401)
        self.assertEqual(
            self.service.get('/api/integrations/sela/threads').status_code, 401)
        self.assertEqual(
            self.service.get(f'/api/integrations/sela/threads/{thread_id}').status_code, 401)

    def test_service_token_can_use_dialogue_routes(self):
        thread_id = self._create_thread()
        appended = self.service.post(
            f'/api/integrations/sela/threads/{thread_id}/messages',
            json={'text': '追加', 'seen_revision': 1}, headers=self.service_headers)
        self.assertEqual(appended.status_code, 200)
        self.assertEqual(appended.get_json()['thread']['revision'], 2)

        fetched = self.service.get(
            f'/api/integrations/sela/threads/{thread_id}', headers=self.service_headers)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.get_json()['thread']['id'], thread_id)

        listing = self.service.get(
            '/api/integrations/sela/threads?awaiting=sela&status=open',
            headers=self.service_headers)
        self.assertEqual(listing.status_code, 200)
        self.assertIn('counts', listing.get_json())

        closed = self.service.post(
            f'/api/integrations/sela/threads/{thread_id}/close',
            json={'summary': '完成'}, headers=self.service_headers)
        self.assertEqual(closed.status_code, 200)
        self.assertEqual(closed.get_json()['thread']['status'], 'closed')

    def test_service_token_not_allowed_off_whitelist_path(self):
        # Same prefix but not whitelisted: the token must not authenticate it.
        self.assertEqual(
            self.service.get('/api/integrations/sela/threads/bogus',
                             headers=self.service_headers).status_code, 401)

    def test_human_reply_requires_login_and_sets_awaiting_sela(self):
        thread_id = self._create_thread()
        anon = self.module.app.test_client()
        self.assertEqual(
            anon.post(f'/api/inbox/threads/{thread_id}/reply',
                      json={'text': '匿名', 'seen_revision': 1}).status_code, 401)
        # A personal gateway token must not reach the human route either.
        gateway = self.module.app.test_client()
        self.assertIn(
            gateway.post(f'/api/inbox/threads/{thread_id}/reply',
                         json={'text': '令牌', 'seen_revision': 1},
                         headers={'Authorization': 'Bearer trosa_pat_deadbeef'}).status_code,
            (401, 403))

        human = self.module.app.test_client()
        human.post('/api/auth/login', json={'user': 'hamid'})
        response = human.post(
            f'/api/inbox/threads/{thread_id}/reply',
            json={'text': '人已回复', 'seen_revision': 1})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        self.assertEqual(body['thread']['awaiting'], 'sela')
        self.assertEqual(body['message']['role'], 'human')

    def test_human_thread_read_names_the_prospect_customer(self):
        thread_id = self._create_thread()
        human = self.module.app.test_client()
        human.post('/api/auth/login', json={'user': 'hamid'})
        original = self.module._sela_profile_by_source
        try:
            self.module._sela_profile_by_source = lambda conn, source_id: (
                {'customer_id': 4321} if source_id == 'route' else None)
            human_body = human.get(f'/api/inbox/threads/{thread_id}').get_json()
            sela_body = self.service.get(
                f'/api/integrations/sela/threads/{thread_id}',
                headers=self.service_headers).get_json()
        finally:
            self.module._sela_profile_by_source = original
        self.assertEqual(human_body['thread']['customer_id'], 4321)
        self.assertNotIn('customer_id', sela_body['thread'])

    def test_human_list_hides_threads_of_archived_prospect_customer(self):
        thread_id = self._create_thread()
        human = self.module.app.test_client()
        human.post('/api/auth/login', json={'user': 'hamid'})
        original = (self.module._sela_profile_by_source, self.module._sela_profile_customer)
        try:
            self.module._sela_profile_by_source = lambda conn, source_id: (
                {'customer_id': 4321} if source_id == 'route' else None)
            self.module._sela_profile_customer = lambda conn, cid: {'id': cid, 'is_deleted': 0}
            shown = human.get('/api/inbox/threads').get_json()
            self.module._sela_profile_customer = lambda conn, cid: None
            hidden = human.get('/api/inbox/threads').get_json()
        finally:
            self.module._sela_profile_by_source, self.module._sela_profile_customer = original
        self.assertIn(thread_id, [t['id'] for t in shown['threads']])
        self.assertNotIn(thread_id, [t['id'] for t in hidden['threads']])
        self.assertEqual(hidden['counts']['open'], shown['counts']['open'] - 1)

    def test_human_close_requires_login(self):
        thread_id = self._create_thread()
        anon = self.module.app.test_client()
        self.assertEqual(
            anon.post(f'/api/inbox/threads/{thread_id}/close',
                      json={'seen_revision': 1}).status_code, 401)
        human = self.module.app.test_client()
        human.post('/api/auth/login', json={'user': 'hamid'})
        response = human.post(
            f'/api/inbox/threads/{thread_id}/close',
            json={'seen_revision': 1, 'note': '不需要'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['thread']['closed_by'], 'human')

    def test_idempotency_conflict_returns_error_envelope(self):
        thread_id = self._create_thread()
        first = self.service.post(
            f'/api/integrations/sela/threads/{thread_id}/messages',
            json={'text': '第一次', 'seen_revision': 1, 'idempotency_key': 'route-key'},
            headers=self.service_headers)
        self.assertEqual(first.status_code, 200)
        replay = self.service.post(
            f'/api/integrations/sela/threads/{thread_id}/messages',
            json={'text': '第一次', 'seen_revision': 1, 'idempotency_key': 'route-key'},
            headers=self.service_headers)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.get_json()['thread']['revision'],
                         first.get_json()['thread']['revision'])

        conflict = self.service.post(
            f'/api/integrations/sela/threads/{thread_id}/messages',
            json={'text': '不一样', 'seen_revision': 2, 'idempotency_key': 'route-key'},
            headers=self.service_headers)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()['error']['code'], 'idempotency_conflict')

    def test_invalid_body_returns_envelope(self):
        response = self.service.post(
            '/api/integrations/sela/threads', data='not json',
            content_type='text/plain', headers=self.service_headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error']['code'], 'invalid_request')

    def test_observability_route(self):
        self._create_thread()
        response = self.service.get(
            '/api/integrations/sela/inbox-observability', headers=self.service_headers)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIn('waiting_human', body)
        self.assertIn('legacy_route_hits_30d', body)

    def test_human_thread_list_and_get(self):
        thread_id = self._create_thread(text='人可读的对话')
        anon = self.module.app.test_client()
        self.assertEqual(anon.get('/api/inbox/threads').status_code, 401)
        self.assertEqual(anon.get(f'/api/inbox/threads/{thread_id}').status_code, 401)
        gateway = self.module.app.test_client()
        self.assertIn(
            gateway.get('/api/inbox/threads',
                        headers={'Authorization': 'Bearer trosa_pat_deadbeef'}).status_code,
            (401, 403))

        human = self.module.app.test_client()
        human.post('/api/auth/login', json={'user': 'hamid'})
        listing = human.get('/api/inbox/threads?status=open')
        self.assertEqual(listing.status_code, 200, listing.get_data(as_text=True))
        body = listing.get_json()
        self.assertIn('threads', body)
        self.assertIn('counts', body)
        self.assertEqual(body['counts']['awaiting_human'], 1)
        self.assertEqual(body['threads'][0]['id'], thread_id)
        self.assertIn('last_message_preview', body['threads'][0])

        fetched = human.get(f'/api/inbox/threads/{thread_id}')
        self.assertEqual(fetched.status_code, 200, fetched.get_data(as_text=True))
        self.assertEqual(fetched.get_json()['thread']['id'], thread_id)

    def test_human_thread_get_unknown_not_found(self):
        human = self.module.app.test_client()
        human.post('/api/auth/login', json={'user': 'hamid'})
        missing = '00000000-0000-0000-0000-000000000000'
        response = human.get(f'/api/inbox/threads/{missing}')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()['error']['code'], 'not_found')

    def test_human_thread_list_invalid_filter(self):
        human = self.module.app.test_client()
        human.post('/api/auth/login', json={'user': 'hamid'})
        response = human.get('/api/inbox/threads?awaiting=nonsense')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error']['code'], 'invalid_request')

    def test_malformed_thread_id_and_updated_after(self):
        human = self.module.app.test_client()
        human.post('/api/auth/login', json={'user': 'hamid'})
        response = human.get('/api/inbox/threads/not-a-thread-id')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()['error']['code'], 'not_found')
        response = human.get('/api/inbox/threads?updated_after=yesterday')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error']['code'], 'invalid_request')
        response = human.get('/api/inbox/threads?updated_after=2026-10-01T00:00:00Z')
        self.assertEqual(response.status_code, 200)


if __name__ == '__main__':
    unittest.main()


class HumanNoteRoutesTest(unittest.TestCase):
    setUp = InboxDialogueRoutesTest.setUp
    tearDown = InboxDialogueRoutesTest.tearDown
    _create_thread = InboxDialogueRoutesTest._create_thread

    def test_human_can_open_a_note_thread_but_not_anonymously_or_by_token(self):
        anon = self.module.app.test_client()
        self.assertEqual(anon.post('/api/inbox/threads', json={'text': '匿名'}).status_code, 401)
        self.assertIn(
            self.service.post('/api/inbox/threads', json={'text': '服务令牌'},
                              headers=self.service_headers).status_code, (401, 403))
        human = self.module.app.test_client()
        human.post('/api/auth/login', json={'user': 'hamid'})
        response = human.post('/api/inbox/threads', json={'text': '今天和 Carlos 聊了报价'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        thread = response.get_json()['thread']
        self.assertEqual(thread['awaiting'], 'sela')
        # sela sees it in its queue and can read it with the service token.
        listing = self.service.get('/api/integrations/sela/threads?awaiting=sela&status=open',
                                   headers=self.service_headers).get_json()
        self.assertIn(thread['id'], [t['id'] for t in listing['threads']])

    def test_service_token_reaches_thread_attachments_path_only(self):
        thread_id = self._create_thread()
        file_id = '8f1c2a52-3c1e-4a4b-9f60-111111111111'
        response = self.service.get(
            f'/api/integrations/sela/threads/{thread_id}/attachments/{file_id}',
            headers=self.service_headers)
        self.assertNotEqual(response.status_code, 401)  # whitelisted; SQLite has no file store
        self.assertEqual(
            self.service.get(f'/api/integrations/sela/threads/{thread_id}/attachments/not-a-uuid',
                             headers=self.service_headers).status_code, 401)
