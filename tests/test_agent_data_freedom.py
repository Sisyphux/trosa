"""Agent data freedom: policy table, reversible writes, read scopes, CLI end to end.

Every test runs against an isolated SQLite directory; no real customer data or
release credentials are read.  The CLI test drives the real Flask app through its
test client, so it exercises the same routes the production CLI would call.
"""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

import db  # noqa: E402
import trosa_cli  # noqa: E402


class AgentDataFreedomTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        self.original_demo = os.environ.get('CRM_SEED_DEMO_DATA')
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.init_all_dbs()

    def tearDown(self):
        db.cancel_safety_backup()
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    # ---------------------------------------------------------------- helpers
    def _load_module(self, name='crm_app_agent_data_freedom_test'):
        spec = importlib.util.spec_from_file_location(name, ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _login(self, module, user='hamid'):
        client = module.app.test_client()
        self.assertEqual(client.post('/api/auth/login', json={'user': user}).status_code, 200)
        return client

    def _mint(self, module, scopes=('crm:read', 'crm:write'), **extra):
        session = self._login(module)
        response = session.post('/api/agent-gateway/tokens', json={'scopes': list(scopes), **extra})
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()['data']

    @staticmethod
    def _insert_customer(user, name, company, email=''):
        conn = sqlite3.connect(db.get_user_db_path(user))
        try:
            conn.execute('INSERT INTO customers (name, company) VALUES (?, ?)', (name, company))
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            if email:
                conn.execute('INSERT INTO contacts (customer_id, name, email) VALUES (?, ?, ?)',
                             (customer_id, name, email))
            conn.commit()
        finally:
            conn.close()
        return customer_id

    def _action(self, gateway, token, action, customer_id, payload, key):
        return gateway.post('/api/gateway/actions', headers={
            'Authorization': 'Bearer ' + token, 'Idempotency-Key': key,
        }, json={'action': action, 'customer_id': customer_id, 'payload': payload})

    # --------------------------------------------------------- policy table
    def test_policy_table_is_the_single_gateway_contract(self):
        module = self._load_module()
        policy = module._GATEWAY_ACTION_POLICY
        self.assertTrue(policy)
        for action, entry in policy.items():
            self.assertIn('handler', entry, action)
            self.assertIn('reversible', entry, action)
            self.assertIn('scope', entry, action)
            if entry['reversible']:
                self.assertTrue(callable(entry['handler']), f'{action} 可逆但缺少处理器')
            else:
                self.assertIsNone(entry['handler'], f'{action} 不可逆但带了处理器')
        for name in ('delete_customer', 'delete_contact', 'delete_task', 'delete_timeline',
                     'delete_inbox', 'delete_attachment', 'bulk_update', 'restore_database',
                     'manage_tokens'):
            self.assertIn(name, policy)
            self.assertFalse(policy[name]['reversible'])
        self.assertIn('create_customer', policy)
        self.assertIn('archive_customer', policy)
        self.assertIn('restore_customer', policy)
        self.assertIn('batch', policy)

    def test_unregistered_and_irreversible_actions_are_refused(self):
        module = self._load_module()
        token = self._mint(module, ('crm:write',))['token']
        gateway = module.app.test_client()
        unregistered = self._action(gateway, token, 'totally_new_action', None, {}, 'unregistered-1')
        self.assertEqual(unregistered.status_code, 409, unregistered.get_json())
        for name in ('delete_customer', 'delete_contact', 'delete_task', 'delete_timeline',
                     'delete_inbox', 'delete_attachment', 'bulk_update', 'restore_database',
                     'manage_tokens'):
            response = self._action(gateway, token, name, None, {}, 'forbidden-' + name)
            self.assertEqual(response.status_code, 409, (name, response.get_json()))

    # ----------------------------------------------------------- create
    def test_create_customer_dedupe_candidate_and_undo_archives(self):
        module = self._load_module()
        existing_id = self._insert_customer('hamid', 'Existing', 'Existing Co', email='dup@example.test')
        token = self._mint(module, ('crm:read', 'crm:write'))['token']
        gateway = module.app.test_client()

        duplicate = self._action(gateway, token, 'create_customer', None, {
            'name': 'Duplicate', 'company': 'Duplicate Co', 'contacts': [{'email': 'dup@example.test'}],
        }, 'create-dup-1')
        self.assertEqual(duplicate.status_code, 200, duplicate.get_json())
        body = duplicate.get_json()['data']
        self.assertFalse(body['created'])
        self.assertEqual(body['duplicate_candidate']['customer_id'], existing_id)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM customers WHERE company='Duplicate Co'").fetchone()[0], 0)
        finally:
            conn.close()

        created = self._action(gateway, token, 'create_customer', None, {
            'name': 'Fresh', 'company': 'Fresh Co', 'contacts': [{'email': 'fresh@example.test'}],
        }, 'create-fresh-1')
        self.assertEqual(created.status_code, 201, created.get_json())
        action = created.get_json()['data']['action']
        new_id = action['customer_id']
        self.assertTrue(new_id)
        replay = self._action(gateway, token, 'create_customer', None, {
            'name': 'Fresh', 'company': 'Fresh Co', 'contacts': [{'email': 'fresh@example.test'}],
        }, 'create-fresh-1')
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertEqual(replay.get_json()['data']['action']['id'], action['id'])

        undone = gateway.post('/api/gateway/actions/' + action['id'] + '/undo',
                              headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(undone.status_code, 200, undone.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            row = conn.execute('SELECT is_deleted FROM customers WHERE id=?', (new_id,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 1)  # archived, not destroyed
        finally:
            conn.close()

    # ------------------------------------------------------ archive/restore
    def test_archive_and_restore_round_trip_with_undo(self):
        module = self._load_module()
        customer_id = self._insert_customer('hamid', 'Archive Round', 'Archive Round Co')
        token = self._mint(module, ('crm:write',))['token']
        gateway = module.app.test_client()

        archived = self._action(gateway, token, 'archive_customer', customer_id, {}, 'archive-1')
        self.assertEqual(archived.status_code, 201, archived.get_json())
        archive_action = archived.get_json()['data']['action']
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute('SELECT is_deleted FROM customers WHERE id=?',
                                          (customer_id,)).fetchone()[0], 1)
        finally:
            conn.close()
        read_back = gateway.post('/api/gateway/actions/' + archive_action['id'] + '/undo',
                                 headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(read_back.status_code, 200, read_back.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute('SELECT is_deleted FROM customers WHERE id=?',
                                          (customer_id,)).fetchone()[0], 0)
        finally:
            conn.close()

        # Archive again, then restore, then attempt a redundant restore.
        self._action(gateway, token, 'archive_customer', customer_id, {}, 'archive-2')
        restored = self._action(gateway, token, 'restore_customer', customer_id, {}, 'restore-1')
        self.assertEqual(restored.status_code, 201, restored.get_json())
        restore_action = restored.get_json()['data']['action']
        redundant = self._action(gateway, token, 'restore_customer', customer_id, {}, 'restore-2')
        self.assertEqual(redundant.status_code, 409, redundant.get_json())
        undone = gateway.post('/api/gateway/actions/' + restore_action['id'] + '/undo',
                              headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(undone.status_code, 200, undone.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute('SELECT is_deleted FROM customers WHERE id=?',
                                          (customer_id,)).fetchone()[0], 1)
        finally:
            conn.close()

    # -------------------------------------------------------------- batch
    def test_batch_success_rollback_and_whole_undo(self):
        module = self._load_module()
        customer_id = self._insert_customer('hamid', 'Batch', 'Batch Co')
        token = self._mint(module, ('crm:write',))['token']
        gateway = module.app.test_client()

        def post_batch(actions, key):
            return gateway.post('/api/gateway/actions', headers={
                'Authorization': 'Bearer ' + token, 'Idempotency-Key': key,
            }, json={'action': 'batch', 'payload': {'actions': actions}})

        good = post_batch([
            {'action': 'create_task', 'customer_id': customer_id,
             'payload': {'title': '第一件', 'due_date': '2026-10-09'}},
            {'action': 'create_task', 'customer_id': customer_id,
             'payload': {'title': '第二件', 'due_date': '2026-10-10'}},
        ], 'batch-good-1')
        self.assertEqual(good.status_code, 201, good.get_json())
        batch_action = good.get_json()['data']['action']
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM reminders WHERE customer_id=? AND is_done=0',
                (customer_id,)).fetchone()[0], 2)
        finally:
            conn.close()
        undone = gateway.post('/api/gateway/actions/' + batch_action['id'] + '/undo',
                              headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(undone.status_code, 200, undone.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM reminders WHERE customer_id=? AND is_done=0',
                (customer_id,)).fetchone()[0], 0)
        finally:
            conn.close()

        # Second child targets a missing customer: the first must roll back.
        failed = post_batch([
            {'action': 'create_task', 'customer_id': customer_id,
             'payload': {'title': '不应留下', 'due_date': '2026-10-11'}},
            {'action': 'create_task', 'customer_id': 999999,
             'payload': {'title': '不存在客户', 'due_date': '2026-10-12'}},
        ], 'batch-fail-1')
        self.assertEqual(failed.status_code, 404, failed.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM reminders WHERE customer_id=? AND title='不应留下'",
                (customer_id,)).fetchone()[0], 0)
        finally:
            conn.close()

        forbidden_child = post_batch([
            {'action': 'delete_task', 'customer_id': customer_id, 'payload': {}},
        ], 'batch-forbidden-1')
        self.assertEqual(forbidden_child.status_code, 409, forbidden_child.get_json())
        too_many = post_batch([
            {'action': 'create_task', 'customer_id': customer_id,
             'payload': {'title': f't{i}', 'due_date': '2026-10-20'}}
            for i in range(51)
        ], 'batch-too-many-1')
        self.assertEqual(too_many.status_code, 409, too_many.get_json())

    # ------------------------------------------------------- read scopes
    def test_agent_reads_open_to_token_but_writes_stay_closed(self):
        module = self._load_module()
        customer_id = self._insert_customer('hamid', 'Read Token', 'Read Token Co', email='read@example.test')
        read_token = self._mint(module, ('crm:read',))['token']
        write_only = self._mint(module, ('crm:write',))['token']
        gateway = module.app.test_client()

        no_token = gateway.get('/api/agent/brief/today')
        self.assertEqual(no_token.status_code, 401, no_token.get_json())
        read_headers = {'Authorization': 'Bearer ' + read_token}
        self.assertEqual(gateway.get('/api/agent/brief/today', headers=read_headers).status_code, 200)
        self.assertEqual(gateway.get(f'/api/agent/customers/{customer_id}/workspace',
                                     headers=read_headers).status_code, 200)
        self.assertEqual(gateway.get(f'/api/agent/customers/{customer_id}/timeline',
                                     headers=read_headers).status_code, 200)
        self.assertEqual(gateway.get('/api/agent/messages/search?query=read',
                                     headers=read_headers).status_code, 200)
        # A write-only token is not a reader.
        self.assertEqual(gateway.get('/api/agent/brief/today',
                                     headers={'Authorization': 'Bearer ' + write_only}).status_code, 403)
        # The token must never reach the non-GET Agent routes or other logins.
        self.assertEqual(gateway.post('/api/agent/proposals', headers=read_headers, json={
            'type': 'task', 'customer_id': customer_id, 'payload': {'title': 'x', 'due_date': '2026-10-09'},
        }).status_code, 403)
        self.assertEqual(gateway.get('/api/customers', headers=read_headers).status_code, 401)

    def test_agent_reads_stay_isolated_to_the_token_member(self):
        module = self._load_module()
        amy_customer = self._insert_customer('amy', 'Amy Secret', 'Amy Secret Co', email='amy@secret.test')
        conn = sqlite3.connect(db.get_user_db_path('amy'))
        try:
            conn.execute('''INSERT INTO follow_up_logs
                            (customer_id, content, follow_date, activity_type, direction, source, created_at)
                            VALUES (?, 'amy-secret-content', '2026-09-01', 'follow_up', 'inbound', 'test',
                                    '2026-09-01 09:00:00')''', (amy_customer,))
            conn.commit()
        finally:
            conn.close()
        token = self._mint(module, ('crm:read',))['token']
        gateway = module.app.test_client()
        headers = {'Authorization': 'Bearer ' + token}
        self.assertEqual(gateway.get(f'/api/agent/customers/{amy_customer}/workspace',
                                     headers=headers).status_code, 404)
        search = gateway.get('/api/agent/messages/search?query=amy-secret-content', headers=headers)
        self.assertEqual(search.status_code, 200, search.get_json())
        self.assertEqual(search.get_json()['items'], [])

    def test_gateway_task_list_pages_with_offset(self):
        module = self._load_module()
        customer_id = self._insert_customer('hamid', 'Page', 'Page Co')
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            for index in range(5):
                conn.execute('''INSERT INTO reminders (customer_id, title, remind_date, is_done, reminder_type)
                                VALUES (?, ?, ?, 0, 'follow_up')''',
                             (customer_id, f'page-{index}', f'2026-10-{10 + index:02d}'))
            conn.commit()
        finally:
            conn.close()
        token = self._mint(module, ('crm:read',))['token']
        gateway = module.app.test_client()
        headers = {'Authorization': 'Bearer ' + token}
        pages = []
        for offset in (0, 2, 4):
            response = gateway.get(f'/api/gateway/tasks?customer_id={customer_id}&limit=2&offset={offset}',
                                   headers=headers)
            self.assertEqual(response.status_code, 200, response.get_json())
            pages.append([row['id'] for row in response.get_json()['data']['tasks']])
        flat = [item for page in pages for item in page]
        self.assertEqual(len(flat), 5)
        self.assertEqual(len(set(flat)), 5)

    def test_token_expiry_whoami_and_revocation(self):
        module = self._load_module()
        created = self._mint(module, ('crm:read',), expires_in_days=1)
        token, token_id = created['token'], created['id']
        gateway = module.app.test_client()
        headers = {'Authorization': 'Bearer ' + token}
        whoami = gateway.get('/api/gateway/whoami', headers=headers)
        self.assertEqual(whoami.status_code, 200, whoami.get_json())
        data = whoami.get_json()['data']
        self.assertEqual(data['user'], 'hamid')
        self.assertIn('crm:read', data['scopes'])
        self.assertLess(data['days_remaining'], 7)
        self.assertTrue(data['renew_hint'])

        # Force the stored record into the past: the token must stop working.
        conn = sqlite3.connect(os.path.join(db.DB_DIR, 'system.db'))
        try:
            key = 'agent_gateway_token:' + token_id
            record = json.loads(conn.execute('SELECT value FROM app_settings WHERE key=?',
                                             (key,)).fetchone()[0])
            record['expires_at'] = '2000-01-01 00:00:00'
            conn.execute('UPDATE app_settings SET value=? WHERE key=?',
                         (json.dumps(record), key))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(gateway.get('/api/gateway/whoami', headers=headers).status_code, 401)
        self.assertEqual(gateway.get('/api/gateway/customers', headers=headers).status_code, 401)

    # ------------------------------------------------------------ CLI E2E
    def test_cli_end_to_end_lifecycle(self):
        module = self._load_module()
        token = self._mint(module, ('crm:read', 'crm:write'))['token']
        gateway = trosa_cli.Gateway(token, 'http://isolated.test')
        parser = trosa_cli.build_parser()

        def run(*argv):
            return trosa_cli.run(parser.parse_args(list(argv)), gateway)

        # Route the CLI's HTTP calls into this app's isolated test client.
        original = trosa_cli.request_json

        def routed_request_json(method, path, params=None, body=None, headers=None,
                                token=None, root=None, timeout=30):
            if params:
                query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, '')})
                if query:
                    path = path + '?' + query
            final_headers = dict(headers or {})
            if token:
                final_headers['Authorization'] = 'Bearer ' + token
            response = module.app.test_client().open(path, method=method, json=body, headers=final_headers)
            payload = response.get_json()
            if response.status_code >= 400:
                message = (payload or {}).get('error')
                if isinstance(message, dict):
                    message = message.get('message')
                raise trosa_cli.CliError(str(message or response.status_code),
                                         4 if response.status_code in (401, 403) else 1)
            return payload

        trosa_cli.request_json = routed_request_json
        try:
            created = run('create-customer', '--name', 'CLI Life', '--company', 'CLI Life Co',
                          '--email', 'cli-life@example.test')
            self.assertIn('undo_hint', created)
            customer_id = created['data']['action']['customer_id']
            self.assertTrue(customer_id)

            updated = run('update-customer', str(customer_id), '--notes', 'CLI 更新资料')
            update_action = updated['data']['action']['id']
            undo = run('undo', update_action)
            self.assertEqual(undo['data']['action']['status'], 'undone')
            conn = sqlite3.connect(db.get_user_db_path('hamid'))
            try:
                self.assertEqual(conn.execute('SELECT notes FROM customers WHERE id=?',
                                              (customer_id,)).fetchone()[0], '')
            finally:
                conn.close()

            task = run('create-task', str(customer_id), '--title', 'CLI 跟进', '--due', '2026-10-20')
            task_id = task['data']['action']['related_id']
            self.assertTrue(task_id)

            rescheduled = run('reschedule', str(task_id), '--date', '2026-11-01')
            reschedule_action = rescheduled['data']['action']['id']
            self.assertTrue(reschedule_action)
            run('undo', reschedule_action)
            conn = sqlite3.connect(db.get_user_db_path('hamid'))
            try:
                self.assertEqual(conn.execute('SELECT remind_date FROM reminders WHERE id=?',
                                              (task_id,)).fetchone()[0], '2026-10-20')
            finally:
                conn.close()

            run('archive-customer', str(customer_id))
            conn = sqlite3.connect(db.get_user_db_path('hamid'))
            try:
                self.assertEqual(conn.execute('SELECT is_deleted FROM customers WHERE id=?',
                                              (customer_id,)).fetchone()[0], 1)
            finally:
                conn.close()

            run('restore-customer', str(customer_id))
            conn = sqlite3.connect(db.get_user_db_path('hamid'))
            try:
                self.assertEqual(conn.execute('SELECT is_deleted FROM customers WHERE id=?',
                                              (customer_id,)).fetchone()[0], 0)
            finally:
                conn.close()
        finally:
            trosa_cli.request_json = original


if __name__ == '__main__':
    unittest.main()
