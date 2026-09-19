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


if __name__ == '__main__':
    unittest.main()
