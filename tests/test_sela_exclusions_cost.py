"""GET /api/integrations/sela/exclusions: constant read cost and conditional reads.

Production measured 518 SQL statements for one call because the PostgreSQL
snapshot read the latest outreach of every contacted Customer on its own.
These tests pin (a) the statement count of the snapshot build does not grow with
the Customer count, (b) the response body is the same facts as before, and
(c) If-None-Match returns 304 without a body and every kind of fact the
snapshot is built from invalidates it.  The PostgreSQL twin lives in
tests/test_postgres_rehearsal.py.
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

URL = '/api/integrations/sela/exclusions'


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_sela_exclusions_cost_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


class SelaExclusionsCostTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        self.original_demo = os.environ.get('CRM_SEED_DEMO_DATA')
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.init_all_dbs()
        self.module = load_app()
        self.client = self.module.app.test_client()
        self.assertEqual(self.client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)

    def tearDown(self):
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    # -- fixtures ----------------------------------------------------------
    def db(self):
        db.set_db_user('hamid')
        return db.get_db()

    def add_customer(self, n, *, contacted=False, email=None, reply=None):
        conn = self.db()
        try:
            cursor = conn.execute(
                "INSERT INTO customers (name, company, country, website, updated_at) "
                "VALUES (?, ?, 'Brazil', ?, datetime('now', 'localtime'))",
                (f'Buyer {n}', f'Excl Co {n}', f'https://excl{n}.example/'))
            customer_id = cursor.lastrowid
            if email:
                conn.execute(
                    'INSERT INTO contacts (customer_id, name, email, is_primary) VALUES (?, ?, ?, 1)',
                    (customer_id, f'Buyer {n}', email))
            if contacted:
                conn.execute(
                    "INSERT INTO follow_up_logs (customer_id, content, follow_date, direction) "
                    "VALUES (?, 'sent a quote', '2026-10-01', 'outbound')", (customer_id,))
            if reply:
                conn.execute(
                    "INSERT INTO outreach_emails (customer_id, subject, sent_date, reply_status) "
                    "VALUES (?, 'Hi', '2026-10-02', ?)", (customer_id, reply))
            conn.commit()
            return customer_id
        finally:
            conn.close()

    def snapshot_statements(self):
        conn = self.db()
        seen = []
        try:
            conn.set_trace_callback(seen.append)
            with self.module.app.test_request_context():
                records = self.module._sela_exclusion_snapshot_records(conn)
        finally:
            conn.close()
        return len(seen), records

    def get(self, etag=None):
        headers = {'If-None-Match': etag} if etag else {}
        return self.client.get(URL, headers=headers)

    def source_ids(self, response):
        return {r.get('source_id') for r in response.get_json()['records']}

    # -- cost --------------------------------------------------------------
    def test_snapshot_statement_count_does_not_grow_with_customers(self):
        for n in range(3):
            self.add_customer(n, contacted=True, email=f'a{n}@excl{n}.example')
        small, small_records = self.snapshot_statements()
        for n in range(3, 40):
            self.add_customer(n, contacted=True, email=f'a{n}@excl{n}.example', reply='replied')
        large, large_records = self.snapshot_statements()
        self.assertEqual((len(small_records), len(large_records)), (3, 40))
        self.assertEqual(small, large)
        self.assertLess(large, 40)

    # -- conditional reads ---------------------------------------------------
    def test_default_response_shape_and_conditional_304(self):
        customer_id = self.add_customer(1, contacted=True, email='a@excl1.example')
        first = self.get()
        self.assertEqual(first.status_code, 200)
        body = first.get_json()
        self.assertEqual(set(body), {'success', 'service', 'schema_version', 'data_version',
                                     'generated_at', 'records'})
        self.assertEqual(body['records'][0]['source_id'], str(customer_id))
        etag = first.headers['ETag']
        self.assertEqual(etag, '"' + body['data_version'] + '"')
        self.assertEqual(first.headers['X-Trosa-Sync-Version'], body['data_version'])
        # ``limit`` is not a pagination parameter: the full feed is returned.
        self.assertEqual(self.client.get(URL + '?limit=1').get_json()['records'], body['records'])
        for validator in (etag, body['data_version']):
            again = self.get(validator)
            self.assertEqual(again.status_code, 304)
            self.assertEqual(again.get_data(), b'')
            self.assertEqual(again.headers['ETag'], etag)

    def assert_invalidates(self, mutate, expect_source):
        before = self.get()
        etag = before.headers['ETag']
        self.assertEqual(self.get(etag).status_code, 304)
        mutate()
        after = self.get(etag)
        self.assertEqual(after.status_code, 200, 'stale ETag must not 304 after a change')
        self.assertNotEqual(after.headers['ETag'], etag)
        self.assertIn(expect_source, self.source_ids(after))
        self.assertEqual(self.get(after.headers['ETag']).status_code, 304)

    def test_new_communication_invalidates(self):
        cid = self.add_customer(1)
        self.add_customer(2, contacted=True)

        def mutate():
            conn = self.db()
            conn.execute("INSERT INTO follow_up_logs (customer_id, content, follow_date, direction) "
                         "VALUES (?, 'called', '2026-10-05', 'outbound')", (cid,))
            conn.commit()
            conn.close()
        self.assert_invalidates(mutate, str(cid))

    def test_customer_reply_invalidates(self):
        cid = self.add_customer(1, reply='pending')
        self.add_customer(2, contacted=True)

        def mutate():
            conn = self.db()
            conn.execute("UPDATE outreach_emails SET reply_status='replied' WHERE customer_id=?", (cid,))
            conn.commit()
            conn.close()
        self.assert_invalidates(mutate, str(cid))

    def test_contact_matching_an_inbound_capture_invalidates(self):
        cid = self.add_customer(1)
        self.add_customer(2, contacted=True)
        conn = self.db()
        conn.execute(
            "INSERT INTO inbox_items (item_type, title, content, dedupe_key, status) VALUES "
            "('gmail_capture', 'mail', ?, 'cap:1', 'open')",
            (json.dumps({'direction': 'inbound', 'channel': 'gmail', 'messages': [
                {'direction': 'inbound', 'sender': 'Late <late@excl1.example>', 'text': 'Hello'}]}),))
        conn.commit()
        conn.close()

        def mutate():
            conn = self.db()
            conn.execute("INSERT INTO contacts (customer_id, name, email, is_primary) "
                         "VALUES (?, 'Late', 'late@excl1.example', 0)", (cid,))
            conn.commit()
            conn.close()
        self.assert_invalidates(mutate, str(cid))

    def test_customer_edit_and_do_not_contact_invalidate(self):
        cid = self.add_customer(1, contacted=True)

        def rename():
            conn = self.db()
            conn.execute("UPDATE customers SET company='Renamed Co', "
                         "updated_at=datetime('now','localtime','+1 minute') WHERE id=?", (cid,))
            conn.commit()
            conn.close()
        self.assert_invalidates(rename, str(cid))

        before = self.get().headers['ETag']
        conn = self.db()
        conn.execute(
            "INSERT INTO agent_prospect_profiles (customer_id, legacy_user_id, source, source_id, "
            "contact_permission, suppression_reason) VALUES (?, 'hamid', 'sela', 'dnc-1', "
            "'do_not_contact', 'not a buyer')", (cid,))
        conn.commit()
        conn.close()
        after = self.get(before)
        self.assertEqual(after.status_code, 200)
        self.assertIn('dnc-1', self.source_ids(after))


if __name__ == '__main__':
    unittest.main()
