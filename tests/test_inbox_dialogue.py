"""Inbox dialogue backend tests (contract §8.1 matrix, DB level).

These exercise ``inbox_dialogue`` directly against an isolated SQLite database;
route/auth behaviour lives in ``test_inbox_dialogue_routes.py`` and the
concurrent PostgreSQL rehearsal in ``test_inbox_dialogue_postgres.py``.
"""

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
import inbox_dialogue as dialogue  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / 'fixtures' / 'inbox_contract'
SCHEMA_DIR = FIXTURE_DIR / 'schemas'
VALIDATOR_PATH = FIXTURE_DIR / 'validator.py'


def load_validator():
    spec = importlib.util.spec_from_file_location('inbox_contract_validator_dlg',
                                                  str(VALIDATOR_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DialogueTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        self.original_demo = os.environ.get('CRM_SEED_DEMO_DATA')
        self.original_backend = os.environ.get('TRADE_OS_DATA_BACKEND')
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        os.environ.pop('TRADE_OS_DATA_BACKEND', None)
        db.init_all_dbs()
        db.set_db_user('hamid')
        self.conn = db.get_db()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.original_db_dir
        if self.original_demo is not None:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        if self.original_backend is not None:
            os.environ['TRADE_OS_DATA_BACKEND'] = self.original_backend
        self.tempdir.cleanup()

    # -- helpers -----------------------------------------------------------
    def create(self, **payload):
        payload.setdefault('title', '测试对话')
        payload.setdefault('text', 'sela 提问')
        key = payload.pop('idempotency_key', None)
        return dialogue.create_thread(self.conn, payload=payload, idempotency_key=key)

    def assert_error(self, ctx, code):
        error = ctx.exception
        self.assertEqual(error.code, code)
        return error


class InboxDialogueMatrixTest(DialogueTestCase):
    def test_create_then_existing_subject_returns_existing(self):
        first = self.create(subject='prospect:abc', text='第一条')
        self.assertTrue(first['created'])
        self.assertEqual(first['thread']['revision'], 1)
        self.assertEqual(first['thread']['awaiting'], 'human')
        self.assertEqual(len(first['thread']['messages']), 1)
        self.assertEqual(first['thread']['messages'][0]['role'], 'sela')
        self.assertEqual(first['thread']['messages'][0]['awaiting_after'], 'human')

        second = self.create(subject='prospect:abc', text='第二条')
        self.assertFalse(second['created'])
        self.assertEqual(second['thread']['id'], first['thread']['id'])
        self.assertEqual(len(second['thread']['messages']), 1)

    def test_append_bumps_revision_and_stale_is_rejected(self):
        thread_id = self.create()['thread']['id']
        body = dialogue.append_message(
            self.conn, thread_id=thread_id,
            payload={'text': '追加', 'seen_revision': 1})
        self.assertEqual(body['thread']['revision'], 2)
        self.assertEqual(body['thread']['messages'][-1]['seq'], 2)
        with self.assertRaises(dialogue.DialogueError) as ctx:
            dialogue.append_message(self.conn, thread_id=thread_id,
                                    payload={'text': '过期', 'seen_revision': 1})
        self.assert_error(ctx, 'thread_changed')
        self.assertEqual(ctx.exception.details['current_revision'], 2)

    def test_close_is_idempotent_and_append_after_close_fails(self):
        thread_id = self.create()['thread']['id']
        closed = dialogue.close_thread(self.conn, thread_id=thread_id,
                                       payload={'summary': '已处理', 'seen_revision': 1})
        self.assertEqual(closed['thread']['status'], 'closed')
        self.assertEqual(closed['thread']['awaiting'], 'none')
        self.assertEqual(closed['thread']['closed_by'], 'sela')
        self.assertEqual(len(closed['thread']['messages']), 1)

        again = dialogue.close_thread(self.conn, thread_id=thread_id,
                                      payload={'summary': '再关一次'})
        self.assertTrue(again['success'])
        self.assertEqual(again['thread']['status'], 'closed')
        self.assertEqual(again['thread']['closed_summary'], '已处理')

        with self.assertRaises(dialogue.DialogueError) as ctx:
            dialogue.append_message(self.conn, thread_id=thread_id,
                                    payload={'text': '关闭后追加'})
        self.assert_error(ctx, 'thread_closed')

    def test_human_reply_sets_awaiting_sela_and_stale_rejected(self):
        thread_id = self.create()['thread']['id']
        body = dialogue.reply_human(self.conn, thread_id=thread_id,
                                    payload={'text': '人已回复', 'seen_revision': 1})
        self.assertEqual(body['thread']['awaiting'], 'sela')
        self.assertEqual(body['thread']['revision'], 2)
        self.assertEqual(body['message']['role'], 'human')
        self.assertEqual(body['message']['seq'], 2)
        with self.assertRaises(dialogue.DialogueError) as ctx:
            dialogue.reply_human(self.conn, thread_id=thread_id,
                                 payload={'text': '过期回复', 'seen_revision': 1})
        self.assert_error(ctx, 'thread_changed')

    def test_user_close_records_human_summary(self):
        thread_id = self.create()['thread']['id']
        body = dialogue.user_close(self.conn, thread_id=thread_id,
                                   payload={'seen_revision': 1, 'note': '不需要了'})
        self.assertEqual(body['thread']['closed_by'], 'human')
        self.assertEqual(body['thread']['closed_summary'], '用户关闭：不需要了')

    def test_reply_after_close_is_thread_closed(self):
        thread_id = self.create()['thread']['id']
        dialogue.close_thread(self.conn, thread_id=thread_id,
                              payload={'summary': '关闭'})
        with self.assertRaises(dialogue.DialogueError) as ctx:
            dialogue.reply_human(self.conn, thread_id=thread_id,
                                 payload={'text': '关闭后回复', 'seen_revision': 1})
        self.assert_error(ctx, 'thread_closed')

    def test_reply_after_system_message_sets_awaiting_sela(self):
        thread_id = self.create()['thread']['id']
        now = dialogue._now()
        self.conn.execute(
            'INSERT INTO inbox_messages '
            '(id, thread_id, seq, role, text, suggested_replies, refs, hints, '
            ' attachments, awaiting_after, created_at) '
            "VALUES (?, ?, 2, 'system', '系统失败', '[]', '[]', NULL, '[]', NULL, ?)",
            ('sys-msg-1', thread_id, now))
        self.conn.execute(
            'UPDATE inbox_threads SET revision=2, updated_at=? WHERE id=?',
            (now, thread_id))
        self.conn.commit()
        # A system message is never an action trigger; a human reply still wins.
        body = dialogue.reply_human(self.conn, thread_id=thread_id,
                                    payload={'text': '人回复', 'seen_revision': 2})
        self.assertEqual(body['thread']['awaiting'], 'sela')

    def test_cross_scope_thread_is_not_found(self):
        thread_id = self.create()['thread']['id']
        self.conn.execute(
            "INSERT INTO inbox_threads "
            "(id, organization_id, legacy_user_id, title, status, awaiting, revision, "
            " opened_at, updated_at) "
            "VALUES (?, ?, 'someone-else', '别人的', 'open', 'human', 1, ?, ?)",
            ('other-thread', dialogue.ORG_ID, dialogue._now(), dialogue._now()))
        self.conn.commit()
        with self.assertRaises(dialogue.DialogueError) as ctx:
            dialogue.get_thread(self.conn, thread_id='other-thread')
        self.assert_error(ctx, 'not_found')
        self.assertTrue(dialogue.get_thread(self.conn, thread_id=thread_id)['thread'])

    def test_idempotency_replay_and_conflict(self):
        body = self.create(subject='prospect:replay', text='原请求',
                           idempotency_key='key-1')
        again = self.create(subject='prospect:replay', text='原请求',
                            idempotency_key='key-1')
        self.assertEqual(again['thread']['id'], body['thread']['id'])
        self.assertEqual(again['created'], body['created'])
        with self.assertRaises(dialogue.DialogueError) as ctx:
            self.create(subject='prospect:replay', text='不同请求',
                        idempotency_key='key-1')
        self.assert_error(ctx, 'idempotency_conflict')

    def test_throttle_cap_and_existing_subject_exemption(self):
        original = dialogue.DAILY_NEW_THREAD_CAP
        dialogue.DAILY_NEW_THREAD_CAP = 1
        try:
            self.create(subject='prospect:cap-a', text='第一条')
            # Over the cap, but the subject already has an open thread.
            existing = self.create(subject='prospect:cap-a', text='再来')
            self.assertFalse(existing['created'])
            with self.assertRaises(dialogue.DialogueError) as ctx:
                self.create(subject='prospect:cap-b', text='新主体')
            self.assert_error(ctx, 'rate_limited')
        finally:
            dialogue.DAILY_NEW_THREAD_CAP = original

    def test_irreversible_confirmation_flow(self):
        thread_id = self.create()['thread']['id']
        proposal = dialogue.append_message(
            self.conn, thread_id=thread_id, payload={'text': '提议动作'})
        proposal_id = proposal['thread']['messages'][-1]['id']
        confirmation = dialogue.reply_human(
            self.conn, thread_id=thread_id,
            payload={'text': '确认执行', 'seen_revision': proposal['thread']['revision']})
        confirmation_id = confirmation['message']['id']

        def handler(conn, arguments, now):
            return {'ran': arguments.get('op')}

        body = dialogue.irreversible_action(
            self.conn, thread_id=thread_id, action='merge_identity',
            arguments={'op': 'merge'}, proposal_message_id=proposal_id,
            confirmed_by_message_id=confirmation_id, handler=handler,
            idempotency_key='irr-1')
        self.assertTrue(body['success'])
        self.assertEqual(body['result'], {'ran': 'merge'})

        # The confirmation is consumed once, in the same transaction.
        with self.assertRaises(dialogue.DialogueError) as ctx:
            dialogue.irreversible_action(
                self.conn, thread_id=thread_id, action='merge_identity',
                arguments={'op': 'merge'}, proposal_message_id=proposal_id,
                confirmed_by_message_id=confirmation_id, handler=handler,
                idempotency_key='irr-2')
        error = self.assert_error(ctx, 'confirmation_required')
        self.assertEqual(error.details['reason'], 'already_consumed')

    def test_irreversible_rejects_bad_proposal_and_order(self):
        thread_id = self.create()['thread']['id']

        def handler(conn, arguments, now):
            return {}

        # No confirmation ids at all.
        with self.assertRaises(dialogue.DialogueError) as ctx:
            dialogue.irreversible_action(
                self.conn, thread_id=thread_id, action='stop_contact',
                arguments={}, proposal_message_id=None,
                confirmed_by_message_id=None, handler=handler)
        self.assert_error(ctx, 'confirmation_required')

        # Confirmation earlier than proposal.
        early = dialogue.reply_human(self.conn, thread_id=thread_id,
                                     payload={'text': '确认', 'seen_revision': 1})
        late = dialogue.append_message(self.conn, thread_id=thread_id,
                                       payload={'text': '提议'})
        with self.assertRaises(dialogue.DialogueError) as ctx:
            dialogue.irreversible_action(
                self.conn, thread_id=thread_id, action='stop_contact',
                arguments={}, proposal_message_id=late['thread']['messages'][-1]['id'],
                confirmed_by_message_id=early['message']['id'], handler=handler)
        error = self.assert_error(ctx, 'confirmation_required')
        self.assertEqual(error.details['reason'],
                         'confirmation_not_later_than_proposal')

        with self.assertRaises(dialogue.DialogueError) as ctx:
            dialogue.irreversible_action(
                self.conn, thread_id=thread_id, action='unknown_action',
                arguments={}, proposal_message_id=late['thread']['messages'][-1]['id'],
                confirmed_by_message_id=early['message']['id'], handler=handler)
        self.assert_error(ctx, 'invalid_request')

    def test_legacy_resolution_and_dual_write(self):
        legacy_id = 501
        canonical = '11111111-2222-3333-4444-555555555555'
        self.conn.execute(
            "INSERT INTO legacy_row_refs "
            "(organization_id, legacy_user_id, table_name, legacy_id, target_id, created_at) "
            "VALUES (?, 'hamid', 'inbox_items', ?, ?, ?)",
            (dialogue.ORG_ID, legacy_id, canonical, dialogue._now()))
        self.conn.commit()
        self.assertEqual(dialogue.resolve_legacy_thread_id(self.conn, legacy_id), canonical)

        thread_id = dialogue.dual_write_legacy_answer(
            self.conn, legacy_id=legacy_id, title='历史标题', content='历史内容',
            human_text='人的回答')
        self.assertEqual(thread_id, canonical)
        thread = dialogue.get_thread(self.conn, thread_id=canonical)['thread']
        self.assertEqual(thread['messages'][0]['text'], '历史内容')
        self.assertEqual(thread['messages'][0]['role'], 'sela')
        self.assertEqual(thread['messages'][1]['role'], 'human')
        self.assertEqual(thread['awaiting'], 'sela')

        # Unresolvable legacy id falls back to the deterministic uuid and still
        # writes a human message.
        fallback = dialogue.dual_write_legacy_answer(
            self.conn, legacy_id=999, title='无映射', content='内容', human_text='回答')
        self.assertEqual(fallback,
                         dialogue._deterministic_uuid('inbox:hamid:999'))

    def test_list_queue_and_counts(self):
        waiting_human = self.create(subject='prospect:list-a')['thread']['id']
        waiting_sela = self.create(subject='prospect:list-b')['thread']['id']
        dialogue.reply_human(self.conn, thread_id=waiting_sela,
                             payload={'text': '回复', 'seen_revision': 1})

        queue = dialogue.list_threads(self.conn, awaiting='sela', status='open', limit=200)
        ids = [item['id'] for item in queue['threads']]
        self.assertEqual(ids, [waiting_sela])
        self.assertEqual(queue['counts']['awaiting_sela'], 1)
        self.assertEqual(queue['counts']['awaiting_human'], 1)
        self.assertEqual(queue['counts']['open'], 2)

        with self.assertRaises(dialogue.DialogueError) as ctx:
            dialogue.list_threads(self.conn, awaiting='bogus')
        self.assert_error(ctx, 'invalid_request')
        with self.assertRaises(dialogue.DialogueError) as ctx:
            dialogue.list_threads(self.conn, status='bogus')
        self.assert_error(ctx, 'invalid_request')

    def test_list_pagination_cursor(self):
        for index in range(3):
            self.create(subject='prospect:page-%d' % index)
        first = dialogue.list_threads(self.conn, limit=2)
        self.assertEqual(len(first['threads']), 2)
        self.assertIsNotNone(first['next_cursor'])
        second = dialogue.list_threads(self.conn, limit=2, cursor=first['next_cursor'])
        self.assertEqual(len(second['threads']), 1)
        self.assertIsNone(second['next_cursor'])

    def test_observability_and_legacy_hits(self):
        self.create(subject='prospect:obs')
        dialogue.record_legacy_hit(self.conn, 'POST /api/inbox/questions/<id>/respond')
        dialogue.record_legacy_hit(self.conn, 'POST /api/inbox/questions/<id>/respond')
        observed = dialogue.observability(self.conn)
        self.assertEqual(observed['waiting_human'], 1)
        self.assertEqual(observed['waiting_sela'], 0)
        self.assertEqual(
            observed['legacy_route_hits_30d']['POST /api/inbox/questions/<id>/respond'], 2)
        self.assertIsNone(observed['oldest_wait_seconds'])


class InboxDialogueContractConsistencyTest(DialogueTestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = load_validator().Validator(str(SCHEMA_DIR))

    def schema(self, stem):
        with open(SCHEMA_DIR / (stem + '.schema.json'), encoding='utf-8') as handle:
            return json.load(handle)

    def assert_valid(self, stem, value):
        errors = self.validator.validate(self.schema(stem), value)
        self.assertEqual(errors, [], '%s: %s' % (stem, errors))

    def test_real_responses_validate_against_contract_schemas(self):
        created = self.create(subject='prospect:contract', text='你好',
                              suggested_replies=['是', '否'],
                              refs=[{'type': 'prospect', 'id': 'p1'}])
        self.assert_valid('threads-create-response', created)
        self.assert_valid('thread', created['thread'])
        thread_id = created['thread']['id']

        appended = dialogue.append_message(
            self.conn, thread_id=thread_id,
            payload={'text': '补充', 'seen_revision': 1})
        self.assert_valid('threads-append-response', appended)

        got = dialogue.get_thread(self.conn, thread_id=thread_id)
        self.assert_valid('threads-get-response', got)

        listing = dialogue.list_threads(self.conn, status='open')
        self.assert_valid('threads-list-response', listing)

        replied = dialogue.reply_human(
            self.conn, thread_id=thread_id,
            payload={'text': '人回复', 'seen_revision': 2},
            undo_factory=lambda conn, tid, message: 'undo-token')
        self.assert_valid('threads-reply-response', replied)

        closed = dialogue.close_thread(
            self.conn, thread_id=thread_id, payload={'summary': '完成'})
        self.assert_valid('threads-close-response', closed)

        listing_all = dialogue.list_threads(self.conn, status='all')
        self.assert_valid('threads-list-response', listing_all)

        self.assert_valid('observability', dialogue.observability(self.conn))

    def test_error_envelope_shape(self):
        with self.assertRaises(dialogue.DialogueError) as ctx:
            dialogue.get_thread(self.conn, thread_id='missing-id')
        self.assert_valid('error', ctx.exception.body())

    def test_user_close_response_validates(self):
        thread_id = self.create()['thread']['id']
        body = dialogue.user_close(self.conn, thread_id=thread_id,
                                   payload={'seen_revision': 1, 'note': '不用'})
        self.assert_valid('threads-user-close-response', body)


if __name__ == '__main__':
    unittest.main()
