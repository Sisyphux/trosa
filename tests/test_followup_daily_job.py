"""Daily follow-up job.  Synthetic data only; storage is the SQLite mirror."""

import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import followup_job as job  # noqa: E402
import followup_store as store  # noqa: E402

TODAY = date(2026, 11, 2)   # a Monday


def at(day):
    return datetime(day.year, day.month, day.day, 3, 0, tzinfo=timezone.utc)


def inbound(n, account, days_ago, text='Could you send the updated price list for the sample order?'):
    return {'event_id': f'e{n}', 'account_id': account, 'event_type': '客户回复',
            'direction': 'inbound', 'content': text, 'result': '', 'next_plan': '',
            'source_module': 'trosa', 'occurred_at': at(TODAY - timedelta(days=days_ago)),
            'payload': {'source': 'sela_handoff'}}


def data(*events, accounts=('A1',)):
    return {'events': list(events),
            'accounts': [{'account_id': a, 'name': f'客户{a}', 'deleted_at': None,
                          'notes': '', 'system_notes': '', 'import_source': ''} for a in accounts],
            'mails': {}, 'inbox': {}, 'open_task_account_ids': set(), 'peers': {}}


class JobBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = db.DB_DIR
        self.old_backend = os.environ.pop('TRADE_OS_DATA_BACKEND', None)
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.DB_DIR = self.tmp.name
        db.init_all_dbs()
        db.set_db_user('hamid')
        self.conn = db.get_db()
        self.payload = data(inbound(1, 'A1', 4))

    def tearDown(self):
        self.conn.close()
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.old_dir
        if self.old_backend is not None:
            os.environ['TRADE_OS_DATA_BACKEND'] = self.old_backend
        self.tmp.cleanup()

    def run_member(self, day=TODAY):
        return job.run_for_member(self.conn, 'hamid', day, loader=lambda *_: self.payload)


class JobCase(JobBase):
    def test_customer_who_wrote_gets_a_suggestion(self):
        summary = self.run_member()
        self.assertEqual((summary['inserted'], summary['ok']), (1, True))
        row = store.list_by_status(self.conn, 'new')[0]
        self.assertEqual(row['account_id'], 'A1')
        self.assertEqual(row['rule'], 'R1')

    def test_running_twice_adds_no_rows(self):
        self.run_member()
        again = self.run_member()
        self.assertEqual(again['inserted'], 0)
        self.assertEqual(len(store.list_by_status(self.conn, 'new')), 1)

    def test_new_record_voids_the_suggestion_the_same_day(self):
        self.run_member()
        # We replied today: the customer is no longer waiting.
        reply = inbound(2, 'A1', 0, 'Sent the revised quote by email.')
        reply.update(direction='outbound', event_type='跟进')
        self.payload = data(inbound(1, 'A1', 4), reply)
        summary = self.run_member()
        self.assertEqual(summary['voided'], 1)
        self.assertEqual(store.list_by_status(self.conn, 'new'), [])
        self.assertEqual(len(store.list_by_status(self.conn, 'voided')), 1)

    def test_dismissed_evidence_never_comes_back(self):
        self.run_member()
        rid = store.list_by_status(self.conn, 'new')[0]['id']
        store.transition(self.conn, rid, 'dismissed')
        self.run_member(TODAY + timedelta(days=1))
        self.assertEqual(store.list_by_status(self.conn, 'new'), [])

    def test_snoozed_comes_back_only_after_its_day(self):
        self.run_member()
        rid = store.list_by_status(self.conn, 'new')[0]['id']
        store.transition(self.conn, rid, 'snoozed', snooze_until=TODAY + timedelta(days=2))
        self.run_member(TODAY + timedelta(days=1))
        self.assertEqual(store.list_visible(self.conn, TODAY + timedelta(days=1)), [])
        self.assertEqual(len(store.list_by_status(self.conn, 'snoozed')), 1)
        self.run_member(TODAY + timedelta(days=2))
        self.assertEqual(len(store.list_visible(self.conn, TODAY + timedelta(days=2))), 1)

    def test_unanswered_suggestions_expire(self):
        self.run_member()
        later = TODAY + timedelta(days=3)
        summary = self.run_member(later)
        self.assertEqual(summary['expired'], 1)
        self.assertEqual(store.list_visible(self.conn, later), [])
        # and the same evidence does not pop up again afterwards
        self.run_member(later + timedelta(days=1))
        self.assertEqual(store.list_by_status(self.conn, 'new'), [])

    def test_self_check_fails_when_stale_rows_pile_up(self):
        self.run_member()
        original = store.expire_old
        store.expire_old = lambda *a, **k: 0
        try:
            with self.assertLogs(job.logger, 'ERROR'):
                summary = self.run_member(TODAY + timedelta(days=5))
        finally:
            store.expire_old = original
        self.assertFalse(summary['ok'])
        self.assertGreater(summary['stale_new'], 0)

    def test_disabled_member_is_skipped_and_one_failure_does_not_stop_others(self):
        calls = []

        def loader(conn, member, today):
            calls.append(member)
            if member == 'amy':
                raise RuntimeError('boom')
            return self.payload

        with self.assertLogs(job.logger, 'ERROR'):
            out = job.run_daily(['hamid', 'amy', 'kelley'], lambda u: u != 'kelley',
                                today=TODAY, loader=loader)
        self.assertEqual(calls, ['hamid', 'amy'])
        self.assertEqual([(s['member'], s['ok']) for s in out], [('hamid', True), ('amy', False)])
        db.set_db_user('hamid')
        conn = db.get_db()
        self.assertEqual(len(store.list_by_status(conn, 'new')), 1)
        conn.close()


class MorningNoteCase(JobBase):
    """The Inbox conversation: said once a day, only when there is something to say."""

    def setUp(self):
        super().setUp()
        self.conn.execute("INSERT INTO customers (id, name, company) VALUES (1, '测试联系人', '乙公司')")
        self.conn.commit()
        self.payload = data(inbound(1, '1', 4), accounts=('1',))

    def threads(self):
        db.set_db_user('hamid')
        conn = db.get_db()
        rows = [dict(r) for r in conn.execute(
            "SELECT t.subject, t.awaiting, m.text, m.refs FROM inbox_threads t "
            "JOIN inbox_messages m ON m.thread_id = t.id").fetchall()]
        conn.close()
        return rows

    def run_day(self, day=TODAY):
        return job.run_daily(['hamid'], lambda u: True, today=day, loader=lambda *_: self.payload)

    def test_names_the_customer_and_the_reason_once(self):
        out = self.run_day()
        self.assertEqual(out[0]['noted'], 1)
        rows = self.threads()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['subject'], 'followup:' + TODAY.isoformat())
        self.assertIn('乙公司', rows[0]['text'])
        self.assertIn('没有回复', rows[0]['text'])

    def test_running_twice_does_not_say_it_twice(self):
        self.run_day()
        self.run_day()
        self.assertEqual(len(self.threads()), 1)

    def test_a_quiet_day_posts_nothing(self):
        self.payload = data(accounts=('1',))
        self.assertEqual(self.run_day()[0]['noted'], 0)
        self.assertEqual(self.threads(), [])

    def test_a_failed_note_never_loses_the_suggestions(self):
        original = job.dialogue.create_thread
        job.dialogue.create_thread = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom'))
        try:
            with self.assertLogs(job.logger, 'ERROR'):
                out = self.run_day()
        finally:
            job.dialogue.create_thread = original
        self.assertEqual((out[0]['ok'], out[0]['noted']), (True, 0))
        db.set_db_user('hamid')
        conn = db.get_db()
        self.assertEqual(len(store.list_by_status(conn, 'new')), 1)
        conn.close()

    def test_ignoring_it_is_enough_to_quiet_the_customer(self):
        self.run_day()
        self.run_day(TODAY + timedelta(days=3))
        db.set_db_user('hamid')
        conn = db.get_db()
        ignored = store.recent_dismissals(conn, TODAY - timedelta(days=14))
        conn.close()
        self.assertEqual(len(ignored.get('1', [])), 1)


if __name__ == '__main__':
    unittest.main()
