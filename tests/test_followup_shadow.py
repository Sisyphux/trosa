"""Pure logic of the shadow-run tool (tools/followup_shadow.py).

Everything here is synthetic: a fake in-memory "database" answers the tool's queries,
no network, no real customers.  ``TODAY`` is a Wednesday.
"""

from __future__ import annotations

import stat
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / 'tools'):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import followup_candidates as fc
import followup_shadow as fs

TZ = ZoneInfo('Asia/Shanghai')
TODAY = date(2026, 10, 28)


def at(days_ago: int) -> datetime:
    return datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time(), TZ)


_counter = [0]


def event(account, days_ago, content, direction='unknown', event_type='follow_up', source='manual', **extra):
    _counter[0] += 1
    return {'event_id': f'e{_counter[0]}', 'account_id': account, 'occurred_at': at(days_ago), 'event_type': event_type,
            'direction': direction, 'content': content, 'result': None, 'next_plan': None,
            'source_module': source, 'payload': {}, 'owner': 'amy', **extra}


def customer_reply(account, days_ago, content='客户回复：想了解报价和样品，请尽快回复'):
    return event(account, days_ago, content, 'inbound', 'customer_reply', 'sela_reply_engine')


def account(account_id, name, **extra):
    return {'account_id': account_id, 'name': name, 'deleted_at': None, 'notes': None,
            'system_notes': None, 'import_source': None, **extra}


class FakeDb:
    """Answers the tool's queries from in-memory rows, honouring the row limit like the real channel."""

    def __init__(self, members: dict, tasks=(), totals=None):
        self.members = members                        # member -> {'accounts': [...], 'events': [...]}
        self.tasks = list(tasks)
        self.totals = totals or {}
        self.calls = []

    def __call__(self, sql, params=(), limit=400):
        self.calls.append(' '.join(sql.split())[:60])
        if 'min(e.occurred_at)' in sql:
            events = self.members[params[0]]['events']
            return [{'first_at': min((e['occurred_at'] for e in events), default=None)}], False
        if 'FROM trosa.timeline_events' in sql:
            member, lo, hi = params
            rows = [{**e, 'id': e['event_id']} for e in self.members[member]['events'] if lo <= e['occurred_at'] < hi]
            return rows[:limit], len(rows) > limit
        if 'FROM trosa.accounts a\n  LEFT JOIN' in sql or 'trosa.customer_details' in sql:
            wanted = set(params[0])
            rows = [{'id': a['account_id'], 'display_name': a['name'], 'deleted_at': a['deleted_at'], 'notes': a['notes'],
                     'system_notes': a['system_notes'], 'import_source': a['import_source']}
                    for m in self.members.values() for a in m['accounts'] if a['account_id'] in wanted]
            return rows[:limit], len(rows) > limit
        if 'trosa.outreach_messages' in sql or 'trosa.inbox_items' in sql:
            return [], False
        if 'FROM trosa.tasks' in sql:
            after = params[0]
            rows = [t for t in self.tasks if t['id'] > after]
            return rows[:300], False
        if 'count(*) AS accounts' in sql:
            return [{'member': m, 'accounts': v['accounts'], 'archived': v['archived']} for m, v in self.totals.items()], False
        raise AssertionError(f'unexpected query: {sql[:80]}')


class ReadingTest(unittest.TestCase):
    def test_full_window_is_split_until_it_fits(self):
        rows = [{'occurred_at': at(d)} for d in range(40, 0, -1)]
        calls = []

        def query(sql, params, limit):
            lo, hi = params[-2:]
            calls.append((lo, hi))
            part = [r for r in rows if lo <= r['occurred_at'] < hi]
            return part[:10], len(part) > 10

        got = fs.fetch_window(query, 'x', ('m',), at(45), at(0) + timedelta(days=1))
        self.assertEqual(len(got), 40)
        self.assertGreater(len(calls), 1)

    def test_one_full_day_cannot_be_split_and_fails_loudly(self):
        def query(sql, params, limit):
            return [{}] * 10, True

        with self.assertRaises(RuntimeError):
            fs.fetch_window(query, 'x', ('m',), at(3), at(3) + timedelta(hours=20))

    def test_id_chunks_are_halved_when_full(self):
        ids = [f'a{i}' for i in range(10)]

        def query(sql, params, limit):
            part = params[0]
            return [{'id': i} for i in part][:3], len(part) > 3

        self.assertEqual(sorted(r['id'] for r in fs.fetch_by_ids(query, 'x', ids, chunk=10)), sorted(ids))

    def test_open_tasks_skip_legacy_outreach_rows(self):
        tasks = [{'id': f't{i:02d}', 'account_id': f'a{i}', 'task_type': kind, 'status': 'open'}
                 for i, kind in enumerate(['follow_up', 'outreach_15天', 'outreach_60天', 'research_stale'])]
        rows = fs.load_open_tasks(FakeDb({}, tasks=tasks))
        self.assertEqual(fc.open_task_account_ids(rows), {'a0', 'a3'})

    def test_totals_are_read_per_member(self):
        totals = fs.load_totals(FakeDb({}, totals={'amy': {'accounts': 5, 'archived': 1}}))
        self.assertEqual(totals, {'amy': {'accounts': 5, 'archived': 1}})


class PeerTest(unittest.TestCase):
    def data(self):
        return {
            'amy': {'events': [event('a1', 30, '报价', 'outbound')], 'accounts': [account('a1', 'Same  Name Ltd.')]},
            'hamid': {'events': [event('h1', 3, '聊了', 'outbound')], 'accounts': [account('h1', 'same name ltd')]},
        }

    def test_peers_only_contain_other_members_and_use_the_newest_day(self):
        index = fs.peer_index(self.data(), TODAY)
        self.assertEqual(fs.peers_of('amy', index), {'same name ltd': TODAY - timedelta(days=3)})
        self.assertEqual(fs.peers_of('hamid', index), {'Same  Name Ltd.': TODAY - timedelta(days=30)})

    def test_a_newer_record_with_another_member_holds_the_candidate_back(self):
        data = self.data()
        index = fs.peer_index(data, TODAY)
        amy = {**data['amy'], 'member': 'amy', 'mails': {}, 'inbox': {}}
        scans = fs.run_scans(amy, TODAY, fs.peers_of('amy', index), set())
        self.assertEqual(scans['steady'].released, [])
        self.assertEqual(len(scans['steady'].suppressed), 1)
        scans = fs.run_scans(amy, TODAY, {}, set())
        self.assertEqual(len(scans['steady'].released), 1)


class ChecksTest(unittest.TestCase):
    def test_batch_days_are_counted_among_live_customers_only(self):
        events = [event('a1', 9, f'补录 {i}', 'outbound') for i in range(25)] + [event('a2', 9, 'x', 'outbound')]
        accounts = [account('a1', 'One'), account('a2', 'Two', deleted_at=at(1))]
        self.assertEqual(fs.batch_day_stats(events, accounts, TODAY), {'days': 1, 'records': 25})

    def test_bounce_helpers(self):
        events = [
            event('a1', 9, '已发报价', 'outbound'),
            event('a1', 2, 'x', 'outbound', 'outreach_bounced', 'sela_reply_engine'),
            event('a2', 5, 'Delivery Status Notification: undeliverable', 'inbound', 'follow_up', 'sela_reply_engine'),
            event('a2', 8, '正常来信', 'inbound', 'customer_reply'),
            event('a3', 4, 'Our budget is 550 per ton', 'inbound', 'customer_reply'),
        ]
        self.assertEqual(set(fs.bounced_last(events)), {'a1', 'a2'})
        self.assertEqual(len(fs.bounce_event_ids(events)), 2)

    def test_sela_only_needs_no_customer_voice(self):
        events = [
            event('a1', 9, '开发信', 'outbound', 'follow_up', 'sela_reply_engine'),
            event('a2', 9, '开发信', 'outbound', 'follow_up', 'sela_reply_engine'),
            customer_reply('a2', 3),
            event('a3', 9, '开发信', 'outbound', 'follow_up', 'sela_reply_engine'),
            event('a3', 2, '电话聊了', 'outbound'),
        ]
        self.assertEqual(fs.sela_only_accounts(events), {'a1'})

    def _candidate(self, account_id, evidence='e-x'):
        return fc.Candidate(account_id=account_id, rule='R2', certainty='certain', evidence_event_id=evidence,
                            evidence_event_ids=(evidence,), facts={}, reason='r')

    def test_self_check_catches_every_planted_violation(self):
        events = [
            event('arch', 9, 'x', 'outbound'),
            event('sela', 9, '开发信', 'outbound', 'follow_up', 'sela_reply_engine'),
            event('bnc', 9, 'bounce', 'outbound', 'outreach_bounced'),
            event('task', 9, 'x', 'outbound'),
        ]
        accounts = [account('arch', 'A', deleted_at=at(1)), account('sela', 'B'), account('bnc', 'C'), account('task', 'D')]
        bounce_id = next(e['event_id'] for e in events if e['account_id'] == 'bnc')
        pool = [self._candidate('arch'), self._candidate('sela'), self._candidate('bnc', bounce_id), self._candidate('task'),
                self._candidate('task')]
        checks = {c['name']: c for c in fs.self_check(pool, accounts, events, {'task'}, pool[:4], 3, pool)}
        for name, check in checks.items():
            self.assertFalse(check['passed'], name)
        self.assertEqual(len(checks), 6)

    def test_self_check_passes_a_clean_list(self):
        events = [event('a1', 20, '已发报价', 'outbound')]
        accounts = [account('a1', 'One')]
        pool = [self._candidate('a1')]
        self.assertTrue(all(c['passed'] for c in fs.self_check(pool, accounts, events, set(), pool, 3, pool)))


class EndToEndTest(unittest.TestCase):
    def db(self):
        amy_events = [
            customer_reply('c1', 5),                                       # R1: wrote 5 days ago, nobody answered
            event('c2', 20, '已发报价 单价 3.2 USD/kg', 'outbound'),         # R2
            event('c3', 20, '已发报价 单价 3.2 USD/kg', 'outbound'),         # R2 but has an open task
            event('c4', 20, '已发报价 单价 3.2 USD/kg', 'outbound'),         # R2 but archived
            event('c5', 9, '开发信', 'outbound', 'follow_up', 'sela_reply_engine'),   # Sela-only, nothing to say
            event('c6', 1, '聊了一下', 'outbound'),                          # nothing fires
        ]
        accounts = [account('c1', 'Alpha Trading'), account('c2', 'Beta Plastics'), account('c3', 'Gamma Films'),
                    account('c4', 'Delta Sheets', deleted_at=at(2)), account('c5', 'Epsilon Dev'), account('c6', 'Zeta Co')]
        return FakeDb({'amy': {'accounts': accounts, 'events': amy_events},
                       'emma': {'accounts': [], 'events': []}},
                      tasks=[{'id': 't1', 'account_id': 'c3', 'task_type': 'follow_up', 'status': 'open'},
                             {'id': 't2', 'account_id': 'c2', 'task_type': 'outreach_30天', 'status': 'open'}],
                      totals={'amy': {'accounts': 8, 'archived': 1}, 'emma': {'accounts': 0, 'archived': 0}})

    def test_run_writes_lists_outside_the_repo_and_summaries_have_no_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = fs.run(self.db(), TODAY, ['amy', 'emma'], Path(tmp))
            amy = next(s for s in result['summaries'] if s['member'] == 'amy')
            self.assertEqual(amy['accounts_total'], 8)
            self.assertEqual(amy['accounts_without_events'], 2)
            self.assertEqual(amy['fired'], {'R1': 1, 'R2': 1})   # the open-task customer never reaches the rules
            self.assertEqual(amy['excluded'].get('has_open_task'), 1)
            self.assertEqual(amy['excluded'].get('archived'), 1)
            self.assertEqual(amy['candidates_total'], 2)
            self.assertEqual(amy['first_day_released'], 2)
            self.assertTrue(all(c['passed'] for c in amy['checks']), amy['checks'])
            self.assertEqual(result['outreach_open_rows_skipped'], 1)

            out = Path(result['out_dir'])
            text = (out / 'Amy.txt').read_text(encoding='utf-8')
            self.assertIn('Alpha Trading', text)              # the file may carry names ...
            self.assertIn('Beta Plastics', text)
            self.assertNotIn('Gamma Films', text)             # ... but not excluded customers
            self.assertNotIn('Delta Sheets', text)
            self.assertEqual(stat.S_IMODE((out / 'Amy.txt').stat().st_mode), 0o600)
            summary = (out / '汇总（只有数字）.txt').read_text(encoding='utf-8')
            for name in ('Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon', 'Zeta'):
                self.assertNotIn(name, summary)
            printed = '\n'.join('\n'.join(fs.summary_lines(s)) for s in result['summaries'])
            self.assertNotIn('Alpha', printed)

    def test_every_query_is_a_plain_select_the_channel_accepts(self):
        import trosa_maint_read as maint
        for sql in (fs._EVENT_SQL, fs._FIRST_EVENT_SQL, fs._ACCOUNT_SQL, fs._MAIL_SQL, fs._INBOX_SQL,
                    fs._TOTALS_SQL, fs._TASK_SQL):
            maint.check_read_only_sql(sql)

    def test_lists_cannot_be_written_inside_the_repository(self):
        with self.assertRaises(SystemExit):
            fs.safe_out_dir(ROOT / 'docs', TODAY)
        self.assertEqual(fs.safe_out_dir(Path('/tmp/shadow-test'), TODAY).name, TODAY.isoformat())


if __name__ == '__main__':
    unittest.main()
