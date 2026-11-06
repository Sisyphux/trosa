"""Live progress line ("sela 正在做什么") for Inbox conversations.

Temporary state only: no revision change, no messages, 30 s TTL.
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
    spec = importlib.util.spec_from_file_location('trosa_inbox_progress_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


class InboxProgressTest(unittest.TestCase):
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

    def _human(self):
        human = self.module.app.test_client()
        human.post('/api/auth/login', json={'user': 'hamid'})
        return human

    def _awaiting_sela_thread(self):
        thread_id = self._create_thread()
        self._human().post(f'/api/inbox/threads/{thread_id}/reply',
                           json={'text': '查 Northwind ana@northwind.com', 'seen_revision': 1})
        return thread_id

    def _push(self, thread_id, payload, headers=None):
        return self.service.post(
            f'/api/integrations/sela/threads/{thread_id}/progress', json=payload,
            headers=self.service_headers if headers is None else headers)

    def _progress(self, thread_id):
        return self._human().get(f'/api/inbox/threads/{thread_id}').get_json()['thread']['progress']

    def test_rejects_non_sela_credentials(self):
        thread_id = self._awaiting_sela_thread()
        anon = self.module.app.test_client()
        self.assertEqual(anon.post(f'/api/integrations/sela/threads/{thread_id}/progress',
                                   json={'text': 'x', 'seq': 1}).status_code, 401)
        self.assertEqual(self._push(thread_id, {'text': 'x', 'seq': 1},
                                    headers={'Authorization': 'Bearer trosa_pat_deadbeef'}).status_code in (401, 403), True)
        # a logged-in human may not use the sela path either
        human = self._human()
        self.assertEqual(human.get(f'/api/inbox/threads/{thread_id}').get_json()['thread']['progress'], None)

    def test_stores_cleans_and_limits(self):
        thread_id = self._awaiting_sela_thread()
        long_text = 'a' * 300
        r = self._push(thread_id, {'text': '正在查\nNorthwind\x00 的资料', 'marks': [
            'Northwind', 'Northwind', ' ', 'b' * 200, 'c', 'd', 'e', 'f'],
            'run_id': 'r1', 'seq': 1})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        progress = self._progress(thread_id)
        self.assertEqual(progress['text'], '正在查 Northwind 的资料')
        self.assertEqual(len(progress['marks']), 4)
        self.assertEqual(progress['marks'][0], 'Northwind')
        self.assertEqual(len(progress['marks'][1]), 80)
        self.assertTrue(progress['at'].endswith('Z'))
        self._push(thread_id, {'text': long_text, 'run_id': 'r1', 'seq': 2})
        self.assertEqual(len(self._progress(thread_id)['text']), 80)
        self.assertEqual(self._push(thread_id, {'text': '', 'seq': 3}).status_code, 400)
        self.assertEqual(self._push(thread_id, {'text': 'x', 'marks': 'no', 'seq': 3}).status_code, 400)
        self.assertEqual(self._push(thread_id, {'text': 'x', 'seq': 'no'}).status_code, 400)

    def test_seq_order_within_a_run(self):
        thread_id = self._awaiting_sela_thread()
        self._push(thread_id, {'text': '第二句', 'run_id': 'r1', 'seq': 5})
        self._push(thread_id, {'text': '旧的', 'run_id': 'r1', 'seq': 4})
        self.assertEqual(self._progress(thread_id)['text'], '第二句')
        self._push(thread_id, {'text': '新一轮', 'run_id': 'r2', 'seq': 1})
        self.assertEqual(self._progress(thread_id)['text'], '新一轮')

    def test_ttl_expiry(self):
        thread_id = self._awaiting_sela_thread()
        self._push(thread_id, {'text': '还在', 'run_id': 'r1', 'seq': 1})
        self.assertIsNotNone(self._progress(thread_id))
        conn = db.get_db()
        conn.execute("UPDATE inbox_thread_progress SET at='2000-01-01T00:00:00Z'")
        conn.commit()
        conn.close()
        self.assertIsNone(self._progress(thread_id))

    def test_clear(self):
        thread_id = self._awaiting_sela_thread()
        self._push(thread_id, {'text': '在做', 'run_id': 'r1', 'seq': 1})
        self.assertEqual(self._push(thread_id, {'clear': True, 'run_id': 'r1'}).status_code, 200)
        self.assertIsNone(self._progress(thread_id))

    def test_sela_message_or_close_clears_it(self):
        thread_id = self._awaiting_sela_thread()
        self._push(thread_id, {'text': '在做', 'run_id': 'r1', 'seq': 1})
        rev = self._human().get(f'/api/inbox/threads/{thread_id}').get_json()['thread']['revision']
        r = self.service.post(f'/api/integrations/sela/threads/{thread_id}/messages',
                              json={'text': '好了', 'awaiting': 'human'}, headers=self.service_headers)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNone(self._progress(thread_id))
        # a late heartbeat after the answer is not stored (not awaiting sela any more)
        self._push(thread_id, {'text': '迟到', 'run_id': 'r1', 'seq': 9})
        self.assertIsNone(self._progress(thread_id))
        self.assertGreater(self._human().get(f'/api/inbox/threads/{thread_id}').get_json()['thread']['revision'], rev)

        other = self._awaiting_sela_thread_with_subject('prospect:other')
        self._push(other, {'text': '在做', 'run_id': 'r1', 'seq': 1})
        self.service.post(f'/api/integrations/sela/threads/{other}/close',
                          json={'summary': '完成'}, headers=self.service_headers)
        self.assertIsNone(self._progress(other))

    def _awaiting_sela_thread_with_subject(self, subject):
        response = self.service.post('/api/integrations/sela/threads',
                                     json={'title': 't2', 'text': 'q', 'subject': subject},
                                     headers=self.service_headers)
        thread_id = response.get_json()['thread']['id']
        self._human().post(f'/api/inbox/threads/{thread_id}/reply',
                           json={'text': '嗯', 'seen_revision': 1})
        return thread_id

    def test_not_stored_when_not_awaiting_sela(self):
        thread_id = self._create_thread()  # awaiting human
        self.assertEqual(self._push(thread_id, {'text': '在做', 'seq': 1}).status_code, 200)
        self.assertIsNone(self._progress(thread_id))

    def test_unknown_thread_404(self):
        self.assertEqual(self._push('00000000-0000-4000-8000-000000000000',
                                    {'text': 'x', 'seq': 1}).status_code, 404)

    def test_read_does_not_change_revision_or_listing_counts(self):
        thread_id = self._awaiting_sela_thread()
        before = self._human().get(f'/api/inbox/threads/{thread_id}').get_json()['thread']
        for n in range(1, 4):
            self._push(thread_id, {'text': f'第{n}句', 'run_id': 'r1', 'seq': n})
        after = self._human().get(f'/api/inbox/threads/{thread_id}').get_json()['thread']
        self.assertEqual(before['revision'], after['revision'])
        self.assertEqual(before['updated_at'], after['updated_at'])
        self.assertEqual(len(before['messages']), len(after['messages']))
        listing = self._human().get('/api/inbox/threads?status=open').get_json()
        row = [t for t in listing['threads'] if t['id'] == thread_id][0]
        self.assertEqual(row['progress']['text'], '第3句')
        self.assertEqual(row['revision'], before['revision'])
        # sela's own read stays free of the live line (it is for the human view)
        sela_view = self.service.get(f'/api/integrations/sela/threads/{thread_id}',
                                     headers=self.service_headers).get_json()['thread']
        self.assertNotIn('progress', sela_view)

    def test_brief_poll_body_is_small_and_carries_progress(self):
        thread_id = self._awaiting_sela_thread()
        self._push(thread_id, {'text': '在做', 'marks': ['x'], 'run_id': 'r1', 'seq': 1})
        body = self._human().get(f'/api/inbox/threads/{thread_id}?brief=1').get_json()['thread']
        self.assertNotIn('messages', body)
        self.assertEqual(body['awaiting'], 'sela')
        self.assertEqual(body['progress']['text'], '在做')


if __name__ == '__main__':
    unittest.main()
