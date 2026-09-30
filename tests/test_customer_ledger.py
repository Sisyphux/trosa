"""Read-only customer ledger endpoints: counts, segment subtotals, ruler index, rows.

The ledger room (design/rooms/customers.md) only *reads* the relationship facts
that ``GET /api/customers`` already projects.  These tests pin the aggregation
(paging window, per-view counts, segment buckets), the ruler flags, the field
whitelist and per-user isolation, and that the legacy list keeps its shape.
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db

ROW_KEYS = {
    'id', 'company', 'person', 'country', 'field', 'website', 'event_date', 'days', 'flags',
    'has_contact', 'waiting_reply', 'activity_kind', 'activity_snippet', 'next_task_title',
    'next_task_date', 'next_task_days', 'match',
}


class CustomerLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        self.original_demo = os.environ.get('CRM_SEED_DEMO_DATA')
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.init_all_dbs()
        spec = importlib.util.spec_from_file_location('crm_app_customer_ledger_test', ROOT / 'app.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.today = self.module._calendar_today()
        self.client = self.module.app.test_client()
        self.assertEqual(self.client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)

    def tearDown(self):
        db.cancel_safety_backup()
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    # -- fixtures ---------------------------------------------------------
    def _day(self, offset):
        return (self.today - timedelta(days=offset)).isoformat()

    def _add(self, user, company, *, country='德国', field='包装', reply_days=None, sent_days=None,
             reply_status='pending', next_in=None, snippet='讨论了报价'):
        """Seed one customer. ``reply_days`` = inbound communication that many days ago,
        ``sent_days`` = outreach email that many days ago, ``next_in`` = open task in N days."""
        conn = sqlite3.connect(db.get_user_db_path(user))
        try:
            cursor = conn.execute(
                "INSERT INTO customers (name, company, country, field, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (company + ' Buyer', company, country, field, self._day(400), self._day(400)))
            customer_id = cursor.lastrowid
            if reply_days is not None:
                conn.execute(
                    """INSERT INTO follow_up_logs (customer_id, content, follow_date, activity_type, direction, source, created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (customer_id, snippet, self._day(reply_days), 'whatsapp', 'inbound', 'test', self._day(reply_days) + ' 09:00:00'))
            if sent_days is not None:
                conn.execute(
                    """INSERT INTO outreach_emails (customer_id, subject, sent_date, reply_status, created_at)
                       VALUES (?,?,?,?,?)""",
                    (customer_id, '开发邮件', self._day(sent_days), reply_status, self._day(sent_days) + ' 09:00:00'))
            if next_in is not None:
                due = (self.today + timedelta(days=next_in)).isoformat()
                conn.execute(
                    """INSERT INTO reminders (customer_id, title, remind_date, is_done, reminder_type, created_at)
                       VALUES (?,?,?,0,'follow_up',?)""",
                    (customer_id, '发送新款目录', due, self._day(1)))
            conn.commit()
            return customer_id
        finally:
            conn.close()

    def _seed_spread(self):
        """One customer per segment plus one without any event."""
        ids = {}
        ids['today'] = self._add('hamid', 'Aaa Today', reply_days=0, next_in=-2)      # overdue
        ids['yesterday'] = self._add('hamid', 'Bbb Yesterday', reply_days=1, next_in=3)
        ids['week'] = self._add('hamid', 'Ccc Week', reply_days=5)                     # no next
        ids['month'] = self._add('hamid', 'Ddd Month', sent_days=20, next_in=10)       # waiting, uncontacted
        ids['quarter'] = self._add('hamid', 'Eee Quarter', reply_days=60, next_in=1)
        ids['older'] = self._add('hamid', 'Fff Older', reply_days=200)
        ids['none'] = self._add('hamid', 'Ggg Nothing')
        return ids

    def _ledger(self, query=''):
        response = self.client.get('/api/customers/ledger' + query)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    # -- tests ------------------------------------------------------------
    def test_requires_login(self):
        anonymous = self.module.app.test_client()
        self.assertEqual(anonymous.get('/api/customers/ledger').status_code, 401)
        self.assertEqual(anonymous.get('/api/customers/ledger/rows?ids=1').status_code, 401)

    def test_empty_ledger_has_zero_counts_and_no_segments(self):
        payload = self._ledger()
        self.assertEqual(payload['total'], 0)
        self.assertEqual(payload['index'], [])
        self.assertEqual(payload['segments'], [])
        self.assertEqual(set(payload['counts'].values()), {0})
        rows = self.client.get('/api/customers/ledger/rows?ids=1,2,3')
        self.assertEqual(rows.status_code, 200)
        self.assertEqual(rows.get_json()['rows'], [])

    def test_index_is_ordered_by_latest_event_with_segment_subtotals(self):
        ids = self._seed_spread()
        payload = self._ledger()
        self.assertEqual(payload['total'], 7)
        self.assertEqual([entry[0] for entry in payload['index']],
                         [ids[key] for key in ('today', 'yesterday', 'week', 'month', 'quarter', 'older', 'none')])
        self.assertEqual([entry[1] for entry in payload['index']], [0, 1, 5, 20, 60, 200, None])
        self.assertEqual([(s['key'], s['start'], s['count']) for s in payload['segments']],
                         [('today', 0, 1), ('yesterday', 1, 1), ('week', 2, 1), ('month', 3, 1),
                          ('quarter', 4, 1), ('older', 5, 1), ('none', 6, 1)])
        by_key = {segment['key']: segment for segment in payload['segments']}
        self.assertEqual(by_key['today']['overdue'], 1)
        self.assertEqual(by_key['week']['no_next'], 1)
        self.assertEqual(by_key['month']['waiting'], 1)
        self.assertEqual(by_key['yesterday']['overdue'], 0)
        self.assertEqual(payload['today'], self.today.isoformat())

    def test_segment_boundaries_and_multiple_customers_share_one_segment(self):
        for offset in (2, 7, 8, 30, 31, 90, 91):
            self._add('hamid', f'Edge {offset:03d}', reply_days=offset)
        segments = {s['key']: s['count'] for s in self._ledger()['segments']}
        self.assertEqual(segments, {'week': 2, 'month': 2, 'quarter': 2, 'older': 1})

    def test_counts_cover_every_view_and_ignore_the_selected_view(self):
        self._seed_spread()
        everything = self._ledger()
        waiting_view = self._ledger('?view=waiting')
        self.assertEqual(everything['counts'], waiting_view['counts'])
        counts = everything['counts']
        self.assertEqual(counts['all'], 7)
        self.assertEqual(counts['communicated'], 5)      # inbound communications
        self.assertEqual(counts['uncontacted'], 2)       # outreach only + nothing at all
        self.assertEqual(counts['waiting'], 1)
        self.assertEqual(counts['no_next'], 3)           # week, older, none
        self.assertEqual(counts['communicated'] + counts['uncontacted'], counts['all'])
        self.assertEqual(waiting_view['total'], 1)
        self.assertEqual(len(waiting_view['index']), 1)

    def test_view_filters_reuse_the_list_definitions(self):
        ids = self._seed_spread()
        no_next = self._ledger('?view=no_next')
        self.assertEqual({entry[0] for entry in no_next['index']}, {ids['week'], ids['older'], ids['none']})
        legacy = self.client.get('/api/customers?view=no_next').get_json()
        self.assertEqual({item['id'] for item in legacy['customers']}, {entry[0] for entry in no_next['index']})
        uncontacted = self._ledger('?view=uncontacted')
        legacy = self.client.get('/api/customers?view=uncontacted').get_json()
        self.assertEqual({item['id'] for item in legacy['customers']}, {entry[0] for entry in uncontacted['index']})
        self.assertEqual(self.client.get('/api/customers/ledger?view=archived').status_code, 400)
        self.assertEqual(self.client.get('/api/customers/ledger?view=bogus').status_code, 400)

    def test_ruler_flags_express_urgency(self):
        ids = self._seed_spread()
        flags = {entry[0]: entry[2] for entry in self._ledger()['index']}
        overdue, no_next, waiting, contact = (
            self.module.LEDGER_FLAG_OVERDUE, self.module.LEDGER_FLAG_NO_NEXT,
            self.module.LEDGER_FLAG_WAITING, self.module.LEDGER_FLAG_CONTACT)
        self.assertTrue(flags[ids['today']] & overdue)
        self.assertFalse(flags[ids['today']] & no_next)
        self.assertTrue(flags[ids['today']] & contact)
        self.assertTrue(flags[ids['week']] & no_next)
        self.assertTrue(flags[ids['month']] & waiting)
        self.assertFalse(flags[ids['month']] & contact)
        self.assertFalse(flags[ids['yesterday']] & (overdue | no_next))

    def test_search_narrows_counts_and_index_together(self):
        self._seed_spread()
        self._add('hamid', 'Zeta Plastics', country='日本', reply_days=4, next_in=2)
        payload = self._ledger('?search=Zeta')
        self.assertEqual(payload['total'], 1)
        self.assertEqual(payload['counts']['all'], 1)
        self.assertEqual(payload['counts']['communicated'], 1)
        self.assertEqual(self._ledger('?search=没有这个客户')['total'], 0)

    def test_rows_return_only_whitelisted_fields_in_requested_order(self):
        ids = self._seed_spread()
        wanted = [ids['older'], ids['today'], ids['none']]
        response = self.client.get('/api/customers/ledger/rows?ids=' + ','.join(map(str, wanted)))
        self.assertEqual(response.status_code, 200)
        rows = response.get_json()['rows']
        self.assertEqual([row['id'] for row in rows], wanted)
        for row in rows:
            self.assertEqual(set(row), ROW_KEYS)
        today_row = rows[1]
        self.assertEqual(today_row['company'], 'Aaa Today')
        self.assertEqual(today_row['days'], 0)
        self.assertEqual(today_row['activity_kind'], 'WhatsApp')
        self.assertEqual(today_row['activity_snippet'], '讨论了报价')
        self.assertEqual(today_row['next_task_title'], '发送新款目录')
        self.assertEqual(today_row['next_task_days'], -2)
        self.assertEqual(rows[2]['days'], None)
        self.assertEqual(rows[2]['next_task_date'], '')

    def test_rows_reject_bad_or_oversized_id_lists(self):
        for query in ('', '?ids=', '?ids=abc', '?ids=1,,x', '?ids=' + ','.join(str(i) for i in range(1, 102))):
            response = self.client.get('/api/customers/ledger/rows' + query)
            self.assertEqual(response.status_code, 400, query)
        exactly_max = ','.join(str(i) for i in range(1, 101))
        self.assertEqual(self.client.get('/api/customers/ledger/rows?ids=' + exactly_max).status_code, 200)

    def test_rows_page_through_a_long_ledger(self):
        for number in range(120):
            self._add('hamid', f'Bulk {number:03d}', reply_days=number % 40)
        payload = self._ledger()
        self.assertEqual(payload['total'], 120)
        ordered = [entry[0] for entry in payload['index']]
        fetched = []
        for start in range(0, len(ordered), 100):
            chunk = ordered[start:start + 100]
            rows = self.client.get('/api/customers/ledger/rows?ids=' + ','.join(map(str, chunk))).get_json()['rows']
            self.assertEqual([row['id'] for row in rows], chunk)
            fetched.extend(rows)
        self.assertEqual([row['days'] for row in fetched], sorted(row['days'] for row in fetched))

    def test_long_company_names_and_snippets_are_bounded(self):
        long_name = '超长公司名 ' * 40
        customer_id = self._add('hamid', long_name, reply_days=1, snippet='很长的沟通内容 ' * 50)
        row = self.client.get(f'/api/customers/ledger/rows?ids={customer_id}').get_json()['rows'][0]
        self.assertEqual(row['company'].strip(), long_name.strip())
        self.assertLessEqual(len(row['activity_snippet']), 91)
        self.assertTrue(row['activity_snippet'].endswith('…'))

    def test_other_users_customers_never_appear(self):
        mine = self._add('hamid', 'Mine Co', reply_days=1)
        theirs = self._add('amy', 'Amy Secret Co', reply_days=1, next_in=1)
        payload = self._ledger()
        self.assertEqual([entry[0] for entry in payload['index']], [mine])
        self.assertEqual(payload['counts']['all'], 1)
        rows = self.client.get(f'/api/customers/ledger/rows?ids={mine},{theirs}').get_json()['rows']
        self.assertEqual([row['company'] for row in rows], ['Mine Co'])
        self.assertEqual(self._ledger('?search=Amy+Secret')['total'], 0)
        amy = self.module.app.test_client()
        self.assertEqual(amy.post('/api/auth/login', json={'user': 'amy'}).status_code, 200)
        amy_payload = amy.get('/api/customers/ledger').get_json()
        self.assertEqual(amy_payload['counts']['all'], 1)
        self.assertEqual(amy.get(f'/api/customers/ledger/rows?ids={mine},{theirs}').get_json()['rows'][0]['company'],
                         'Amy Secret Co')

    def test_search_rows_explain_hidden_matches(self):
        customer_id = self._add('hamid', 'Quiet Co', reply_days=3, snippet='独特唯一暗号 已报价')
        row = self.client.get(f'/api/customers/ledger/rows?ids={customer_id}&search=独特唯一暗号').get_json()['rows'][0]
        self.assertIn('独特唯一暗号', row['match'])

    def test_legacy_customer_list_keeps_its_shape(self):
        self._seed_spread()
        payload = self.client.get('/api/customers?page=1&per_page=5').get_json()
        for key in ('customers', 'total', 'page', 'per_page', 'pages', 'interpreted_filters'):
            self.assertIn(key, payload)
        self.assertEqual(payload['total'], 7)
        self.assertEqual(len(payload['customers']), 5)
        unpaged = self.client.get('/api/customers').get_json()
        self.assertEqual(len(unpaged['customers']), 7)
        for key in ('next_task_date', 'has_contact', 'waiting_reply', 'days_since_contact', 'match_reasons'):
            self.assertIn(key, unpaged['customers'][0])


class CustomerLedgerContractTest(unittest.TestCase):
    """Static guards for the ledger room (design/rooms/customers.md)."""

    @classmethod
    def setUpClass(cls):
        static = ROOT / 'app' / 'static'
        cls.js = (static / 'app.js').read_text(encoding='utf-8')
        cls.css = (static / 'visual-v5.css').read_text(encoding='utf-8')
        cls.html = (static / 'index.html').read_text(encoding='utf-8')
        start = cls.js.index('// ========== CUSTOMER LEDGER (账页) ==========')
        cls.ledger_js = cls.js[start:cls.js.index('function renderCustomerPagination(data) {')]
        css_start = cls.css.index('6b. CUSTOMER LEDGER')
        cls.ledger_css = cls.css[css_start:cls.css.index('7. FORMS')]

    def test_ledger_routes_are_get_only(self):
        spec = importlib.util.spec_from_file_location('crm_app_ledger_contract_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        tempdir = tempfile.TemporaryDirectory()
        original = db.DB_DIR
        db.DB_DIR = tempdir.name
        try:
            spec.loader.exec_module(module)
            rules = [rule for rule in module.app.url_map.iter_rules() if rule.rule.startswith('/api/customers/ledger')]
            self.assertEqual({rule.rule for rule in rules}, {'/api/customers/ledger', '/api/customers/ledger/rows'})
            for rule in rules:
                self.assertEqual(set(rule.methods) - {'HEAD', 'OPTIONS'}, {'GET'}, rule.rule)
        finally:
            db.DB_DIR = original
            tempdir.cleanup()

    def test_ledger_front_end_only_reads(self):
        for verb in ("method: 'POST'", "method: 'PUT'", "method: 'DELETE'", "method: 'PATCH'"):
            self.assertNotIn(verb, self.ledger_js)
        self.assertIn("/api/customers/ledger?", self.ledger_js)
        self.assertIn("/api/customers/ledger/rows?", self.ledger_js)

    def test_ledger_is_windowed_with_fixed_rows_and_keyboard_activedescendant(self):
        self.assertIn("aria-activedescendant", self.ledger_js)
        self.assertIn('role="listbox"', self.html)
        self.assertIn('id="ledgerScroll" tabindex="0"', self.html)
        self.assertIn('LEDGER_BLOCK = 50', self.ledger_js)
        self.assertIn('function ledgerFind(px)', self.ledger_js)
        # Rows are absolutely positioned at computed offsets, never one node per customer.
        self.assertIn("node.style.top = LD.offs[i] + 'px'", self.ledger_js)
        self.assertIn("case 'Enter': ledgerOpen(LD.cur)", self.ledger_js)

    def test_ledger_states_tell_failure_from_emptiness(self):
        for kind in ("'loading'", "'empty'", "'error'", "'none'", "'none-view'"):
            self.assertIn(kind, self.ledger_js)
        for phrase in ('这不是「没有客户」', '网络 · 无法连接服务', '解析 · 服务返回的数据不完整', '权限 · HTTP 403', '服务 · 服务暂无响应'):
            self.assertIn(phrase, self.ledger_js)
        self.assertIn('data-ld-act="retry"', self.ledger_js)

    def test_ledger_css_uses_tokens_and_the_focus_token(self):
        self.assertNotRegex(self.ledger_css, r'#[0-9a-fA-F]{3,8}\b')
        self.assertIn('outline: 2px solid var(--dl-focus)', self.ledger_css)
        self.assertNotIn('var(--dl-gold)', self.ledger_css.replace('text-decoration-color: var(--dl-gold)', ''))
        # Only one solid ink control: the open action.
        self.assertEqual(self.ledger_css.count('background: var(--dl-ink)'), 1)

    def test_narrow_screens_shrink_the_ruler_and_move_open_to_the_dock(self):
        narrow = self.ledger_css[self.ledger_css.index('@media (max-width: 900px)'):]
        self.assertIn('--ld-rw: 26px', narrow)
        self.assertIn('.ld-dock { display: flex', narrow)
        self.assertIn('.ld-row-acts { display: none !important; }', narrow)
        self.assertIn('data-ld-act="open"', self.ledger_js)

    def test_customer_time_zone_filter_is_not_built(self):
        self.assertNotIn('此刻可联系', self.ledger_js)
        self.assertNotIn('此刻可联系', self.html)


if __name__ == '__main__':
    unittest.main()
