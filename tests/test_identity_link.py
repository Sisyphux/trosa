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
import identity_link
import gmail_sync


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_identity_link_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


def capture_content(sender='Ana <ana@acrilicos.example>', thread='thread-1', text='Hello from Ana'):
    return json.dumps({
        'channel': 'gmail',
        'platform': 'Gmail',
        'conversation_identity': sender,
        'thread_id': thread,
        'direction': 'inbound',
        'messages': [{
            'time': '2026-09-01 10:00:00',
            'sender': sender,
            'direction': 'inbound',
            'text': text,
            'raw_text': text,
        }],
    }, ensure_ascii=False)


class IdentityLinkTest(unittest.TestCase):
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
        db.set_db_user('hamid')

    def tearDown(self):
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    # -- helpers -----------------------------------------------------------
    def conn(self):
        db.set_db_user('hamid')
        return db.get_db()

    def add_customer(self, name, **fields):
        conn = self.conn()
        try:
            columns = {'name': name, 'company': name, **fields}
            conn.execute(
                'INSERT INTO customers (' + ', '.join(columns) + ') VALUES ('
                + ', '.join('?' for _ in columns) + ')',
                tuple(columns.values()),
            )
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.commit()
            return customer_id
        finally:
            conn.close()

    def add_contact(self, customer_id, email, name='Contact'):
        conn = self.conn()
        try:
            conn.execute(
                'INSERT INTO contacts (customer_id, name, email) VALUES (?, ?, ?)',
                (customer_id, name, email),
            )
            conn.commit()
        finally:
            conn.close()

    def add_capture(self, content, item_type='gmail_capture', dedupe='test-capture-1'):
        conn = self.conn()
        try:
            conn.execute(
                '''INSERT INTO inbox_items (item_type, customer_id, title, content, dedupe_key, status, created_at)
                   VALUES (?, NULL, ?, ?, ?, 'open', '2026-09-01 10:00:00')''',
                (item_type, '待归属', content, dedupe),
            )
            item_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.commit()
            return item_id
        finally:
            conn.close()

    def inbox_row(self, item_id):
        conn = self.conn()
        try:
            return dict(conn.execute('SELECT * FROM inbox_items WHERE id=?', (item_id,)).fetchone())
        finally:
            conn.close()

    def resolve(self, evidence):
        conn = self.conn()
        try:
            return identity_link.resolve_identity(conn, evidence)
        finally:
            conn.close()

    # -- resolver ----------------------------------------------------------
    def test_exact_contact_email_matches_deterministically(self):
        customer_id = self.add_customer('Acrilicos', website='https://acrilicos.example')
        self.add_contact(customer_id, 'ana@acrilicos.example')
        decision = self.resolve({'emails': ['ANA@acrilicos.example']})
        self.assertEqual(decision['status'], 'matched')
        self.assertEqual(decision['customer_id'], customer_id)
        self.assertIn('contact_email', decision['methods'])
        self.assertIsNotNone(decision['contact_id'])

    def test_unique_website_domain_matches(self):
        customer_id = self.add_customer('Acrilicos', website='https://www.acrilicos.example/')
        decision = self.resolve({'emails': ['sales@acrilicos.example']})
        self.assertEqual(decision['status'], 'matched')
        self.assertEqual(decision['customer_id'], customer_id)
        self.assertIn('website_domain', decision['methods'])

    def test_shared_domain_is_a_conflict_not_a_guess(self):
        first = self.add_customer('Acrilicos A', website='shared.example')
        second = self.add_customer('Acrilicos B', website='shared.example')
        decision = self.resolve({'emails': ['sales@shared.example']})
        self.assertEqual(decision['status'], 'conflict')
        self.assertIsNone(decision['customer_id'])
        self.assertEqual({entry['customer_id'] for entry in decision['candidates']}, {first, second})

    def test_public_email_domain_never_matches_by_domain(self):
        self.add_customer('Acrilicos', website='gmail.com')
        decision = self.resolve({'emails': ['someone@gmail.com']})
        self.assertEqual(decision['status'], 'unmatched')

    def test_persisted_fact_is_reused_and_conflict_surfaces(self):
        customer_id = self.add_customer('Newco', website='newco.example')
        conn = self.conn()
        try:
            identity_link.record_identity_fact(
                conn, identifier_type='email', identifier_value='bob@newco.example',
                customer_id=customer_id, origin='human_confirmed')
            conn.commit()
        finally:
            conn.close()
        decision = self.resolve({'emails': ['bob@newco.example']})
        self.assertEqual(decision['status'], 'matched')
        self.assertEqual(decision['customer_id'], customer_id)
        self.assertIn('confirmed_email', decision['methods'])

        # A conflicting contact owner must force a conflict, not silently win.
        other = self.add_customer('Other', website='other.example')
        self.add_contact(other, 'bob@newco.example')
        conflict = self.resolve({'emails': ['bob@newco.example']})
        self.assertEqual(conflict['status'], 'conflict')
        self.assertEqual({entry['customer_id'] for entry in conflict['candidates']}, {customer_id, other})

    def test_thread_history_matches(self):
        customer_id = self.add_customer('Acrilicos', website='acrilicos.example')
        conn = self.conn()
        try:
            conn.execute(
                '''INSERT INTO gmail_message_states
                   (provider_message_id, provider_thread_id, match_status, customer_id)
                   VALUES ('m-1', 'thread-42', 'matched', ?)''', (customer_id,))
            conn.commit()
        finally:
            conn.close()
        decision = self.resolve({'emails': ['stranger@elsewhere.test'], 'thread_id': 'thread-42'})
        self.assertEqual(decision['status'], 'matched')
        self.assertEqual(decision['customer_id'], customer_id)
        self.assertIn('thread_history', decision['methods'])

    def test_source_external_identity_matches(self):
        customer_id = self.add_customer('Sela Co', external_source='sela', external_id='p-1')
        decision = self.resolve({'source': 'sela', 'external_id': 'p-1'})
        self.assertEqual(decision['status'], 'matched')
        self.assertEqual(decision['customer_id'], customer_id)
        self.assertIn('source_identity', decision['methods'])

    def test_fact_correction_updates_single_active_row(self):
        first = self.add_customer('First', website='first.example')
        second = self.add_customer('Second', website='second.example')
        conn = self.conn()
        try:
            identity_link.record_identity_fact(
                conn, identifier_type='email', identifier_value='x@co.example',
                customer_id=first, origin='human_confirmed')
            identity_link.record_identity_fact(
                conn, identifier_type='email', identifier_value='x@co.example',
                customer_id=second, origin='human_confirmed')
            conn.commit()
            rows = identity_link.active_facts_for_identifier(conn, 'email', 'x@co.example')
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]['customer_id']), second)

    def test_evidence_from_capture_reads_only_explicit_fields(self):
        evidence = identity_link.evidence_from_capture(capture_content(thread='t-7'), source='gmail')
        self.assertIn('ana@acrilicos.example', evidence['emails'])
        self.assertEqual(evidence['thread_id'], 't-7')

    # -- gmail ingestion ---------------------------------------------------
    def test_gmail_match_uses_domain_when_contact_email_missing(self):
        customer_id = self.add_customer('Acrilicos', website='acrilicos.example')
        conn = self.conn()
        try:
            match = gmail_sync._match_message(conn, {
                'external_emails': ['sales@acrilicos.example'], 'thread_id': 't-1',
            })
        finally:
            conn.close()
        self.assertEqual(match['status'], 'matched')
        self.assertEqual(match['customer_id'], customer_id)
        self.assertIn('官网域名', match.get('identity_reason', ''))

    # -- API auto-attribute -------------------------------------------------
    def test_auto_attribute_resolves_domain_match_and_is_undoable(self):
        customer_id = self.add_customer('Acrilicos', website='acrilicos.example')
        item_id = self.add_capture(capture_content())
        response = self.client.post('/api/inbox/auto-attribute', json={})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(len(payload['resolved']), 1)
        resolved = payload['resolved'][0]
        self.assertEqual(resolved['customer_id'], customer_id)
        self.assertEqual(resolved['item_id'], item_id)
        self.assertTrue(resolved['undo_token'])
        self.assertEqual(self.inbox_row(item_id)['status'], 'resolved')

        conn = self.conn()
        try:
            count = conn.execute('SELECT COUNT(*) FROM follow_up_logs WHERE customer_id=?', (customer_id,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

        undo = self.client.post(f"/api/undo/{resolved['undo_token']}", json={})
        self.assertEqual(undo.status_code, 200, undo.get_data(as_text=True))
        self.assertEqual(self.inbox_row(item_id)['status'], 'open')

    def test_auto_attribute_leaves_conflicting_capture_for_human(self):
        self.add_customer('A', website='shared.example')
        self.add_customer('B', website='shared.example')
        item_id = self.add_capture(capture_content(sender='sales@shared.example'), dedupe='conflict-1')
        response = self.client.post('/api/inbox/auto-attribute', json={})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['resolved'], [])
        self.assertEqual(len(payload['held']), 1)
        self.assertEqual(payload['held'][0]['status'], 'conflict')
        self.assertEqual(self.inbox_row(item_id)['status'], 'open')

    def test_human_confirmation_persists_reusable_facts(self):
        customer_id = self.add_customer('Newco', website='newco.example')
        content = capture_content(sender='Dana <dana@newco2.example>', thread='thread-new')
        item_id = self.add_capture(content, dedupe='human-confirm-1')
        response = self.client.post(f'/api/customers/{customer_id}/follow_history', json={
            'activity_content': 'Dana 询问报价',
            'activity_type': 'email',
            'direction': 'inbound',
            'follow_date': '2026-09-02',
            'inbox_item_id': item_id,
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.inbox_row(item_id)['status'], 'resolved')

        conn = self.conn()
        try:
            facts = identity_link.facts_for_customer(conn, customer_id)
        finally:
            conn.close()
        fact_types = {(row['identifier_type'], row['identifier_value']) for row in facts}
        self.assertIn(('email', 'dana@newco2.example'), fact_types)
        self.assertIn(('domain', 'newco2.example'), fact_types)
        self.assertIn(('thread', 'thread-new'), fact_types)

        # A later fact from the same domain now resolves without asking again.
        later = self.add_capture(capture_content(sender='Sam <sam@newco2.example>', thread='thread-later'),
                                 dedupe='human-confirm-2')
        second = self.client.post('/api/inbox/auto-attribute', json={}).get_json()
        self.assertEqual([entry['item_id'] for entry in second['resolved']], [later])
        self.assertEqual(second['resolved'][0]['customer_id'], customer_id)


if __name__ == '__main__':
    unittest.main()
