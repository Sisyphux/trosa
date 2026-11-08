"""Inbox assistant answers "is X due for a follow-up?" from real records.  Synthetic data only."""

import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ask_customer as ask  # noqa: E402
import db  # noqa: E402


class ParseTest(unittest.TestCase):
    def test_questions(self):
        self.assertEqual(ask.parse_question('甲公司是不是该跟进了？'), ('ask', '甲公司'))
        self.assertEqual(ask.parse_question('Bob 最近怎么样'), ('ask', 'Bob'))
        self.assertEqual(ask.parse_question('那个客户是不是也该跟了'), ('ask', ''))
        self.assertEqual(ask.parse_question('2'), ('choose', 2))
        self.assertEqual(ask.parse_question('确认'), ('confirm', ''))

    def test_notes_are_left_alone(self):
        self.assertIsNone(ask.parse_question('今天和 Carlos 通了电话，他要 3mm 透明板报价，下周二前给他'))
        self.assertIsNone(ask.parse_question('已发报价'))

    def test_when(self):
        today = date(2026, 10, 30)
        self.assertEqual(ask.parse_when('推迟到11月5日', today), date(2026, 11, 5))
        self.assertEqual(ask.parse_when('下周三', today), date(2026, 11, 4))
        self.assertEqual(ask.parse_when('两周后', today), date(2026, 11, 13))
        self.assertIsNone(ask.parse_when('晚点吧', today))


def event(day, direction, text, eid):
    return {'account_id': 'a', 'event_id': eid, 'event_type': 'communication', 'direction': direction,
            'content': text, 'result': '', 'next_plan': '', 'source_module': 'sela', 'occurred_at': day,
            'payload': {}}


ACCOUNT = {'account_id': 'a', 'name': '甲公司', 'deleted_at': ''}
TODAY = date(2026, 10, 30)


class AssessTest(unittest.TestCase):
    def test_customer_waiting_can_follow_with_dated_basis(self):
        r = ask.assess(ACCOUNT, [event('2026-10-20', 'inbound', '请报价 3mm 板', 'e1')], [], TODAY)
        self.assertEqual(r['verdict'], ask.CAN)
        self.assertEqual(r['rule'], 'R1')
        text = ask.compose('甲公司', r, TODAY)
        self.assertTrue(text.startswith('结论：可以跟'))
        self.assertIn('10月20日', text)
        self.assertIn('之后的情况我看不到', text)

    def test_just_contacted_waits(self):
        r = ask.assess(ACCOUNT, [event('2026-10-28', 'outbound', '已回复', 'e1')], [], TODAY)
        self.assertEqual(r['verdict'], ask.WAIT)

    def test_future_task_waits(self):
        tasks = [{'remind_date': '2026-11-10', 'title': '发样品', 'reminder_type': 'follow_up'}]
        r = ask.assess(ACCOUNT, [event('2026-10-01', 'outbound', '报价已发', 'e1')], tasks, TODAY)
        self.assertEqual(r['verdict'], ask.WAIT)
        self.assertIn('11月10日', r['why'])

    def test_no_records_and_archived_say_no(self):
        r = ask.assess(ACCOUNT, [], [], TODAY)
        self.assertEqual(r['verdict'], ask.NO)
        self.assertIn('没有', ask.compose('甲公司', r, TODAY))
        self.assertEqual(ask.assess({**ACCOUNT, 'deleted_at': '2026-01-01'}, [], [], TODAY)['verdict'], ask.NO)

    def test_quote_silence_can_follow(self):
        r = ask.assess(ACCOUNT, [event('2026-10-05', 'outbound', '发了报价单 quote', 'e1')], [], TODAY)
        self.assertEqual(r['verdict'], ask.CAN)
        self.assertEqual(r['rule'], 'R2')


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_ask_customer_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_a, **_k: None
    return module


class ConversationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = db.DB_DIR
        self.old_demo = os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.DB_DIR = self.tmp.name
        db.init_all_dbs()
        self.module = load_app()
        self.today = self.module._calendar_today()
        db.set_db_user('hamid')
        conn = db.get_db()
        self.ids = {}
        for key, name, company in (('a', '李', '蓝海贸易'), ('b', '王', '蓝海塑料'), ('c', '赵', '晨光板材')):
            conn.execute('INSERT INTO customers (name, company) VALUES (?, ?)', (name, company))
            self.ids[key] = conn.execute('SELECT max(id) FROM customers').fetchone()[0]
        day = (self.today - timedelta(days=9)).isoformat()
        conn.execute("INSERT INTO follow_up_logs (customer_id, content, follow_date, direction) "
                     "VALUES (?, '客户来信问 3mm 板价格', ?, 'inbound')", (self.ids['c'], day))
        conn.commit()
        conn.close()
        self.client = self.module.app.test_client()
        with self.client.session_transaction() as s:
            s['user'] = 'hamid'

    def tearDown(self):
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.old_dir
        if self.old_demo is not None:
            os.environ['CRM_SEED_DEMO_DATA'] = self.old_demo
        self.tmp.cleanup()

    def ask(self, text, thread=None):
        if thread is None:
            r = self.client.post('/api/inbox/threads', json={'text': text, 'idempotency_key': 'k-' + text})
        else:
            r = self.client.post(f'/api/inbox/threads/{thread["id"]}/reply',
                                 json={'text': text, 'seen_revision': thread['revision'], 'idempotency_key': 'k-' + text})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.last_body = r.get_json()
        return r.get_json()['thread']

    def last(self, thread):
        return thread['messages'][-1]

    def rows(self, sql):
        conn = db.get_db()
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def test_answer_has_verdict_dates_and_customer_link(self):
        thread = self.ask('晨光板材是不是该跟进了？')
        msg = self.last(thread)
        self.assertEqual(msg['role'], 'sela')
        self.assertTrue(msg['text'].startswith('结论：可以跟'))
        self.assertEqual(msg['refs'], [{'type': 'customer', 'id': str(self.ids['c'])}])
        self.assertEqual(thread['awaiting'], 'human')
        thread = self.ask('晨光板材最近怎么样', thread)
        self.assertTrue(self.last_body.get('answered'))    # the page stays on this conversation

    def test_unknown_customer_is_said_plainly(self):
        msg = self.last(self.ask('不存在的某某公司是不是该跟进了？'))
        self.assertIn('没有找到', msg['text'])

    def test_ambiguous_asks_which_then_answers(self):
        thread = self.ask('蓝海 最近怎么样')
        self.assertIn('有几位对得上', self.last(thread)['text'])
        thread = self.ask('1', thread)
        self.assertTrue(self.last(thread)['text'].startswith('结论：'))

    def test_postpone_needs_confirm_then_creates_dated_task(self):
        thread = self.ask('晨光板材是不是该跟进了？')
        before = self.rows('SELECT count(*) FROM reminders')[0][0]
        thread = self.ask('推迟到11月5日', thread)
        self.assertIn('请回复「确认」', self.last(thread)['text'])
        self.assertEqual(self.rows('SELECT count(*) FROM reminders')[0][0], before)    # nothing written yet
        thread = self.ask('确认', thread)
        self.assertIn('已记下', self.last(thread)['text'])
        row = self.rows("SELECT customer_id, remind_date FROM reminders WHERE is_done=0")[-1]
        self.assertEqual(row[0], self.ids['c'])
        self.assertTrue(row[1].endswith('-11-05'))

    def test_asking_is_read_only(self):
        tables = ('customers', 'follow_up_logs', 'reminders')
        before = [self.rows(f'SELECT count(*) FROM {t}')[0][0] for t in tables]
        self.ask('晨光板材是不是该跟进了？')
        self.assertEqual(before, [self.rows(f'SELECT count(*) FROM {t}')[0][0] for t in tables])

    def test_notes_still_go_to_the_normal_assistant(self):
        thread = self.ask('今天和晨光板材的赵总通了电话，他要报价')
        self.assertEqual(len(thread['messages']), 1)
        self.assertEqual(thread['awaiting'], 'sela')

    def test_replay_does_not_answer_twice(self):
        first = self.ask('晨光板材是不是该跟进了？')
        self.ask('晨光板材是不是该跟进了？')
        stored = self.client.get(f'/api/inbox/threads/{first["id"]}').get_json()['thread']
        self.assertEqual(len(stored['messages']), 2)


if __name__ == '__main__':
    unittest.main()
