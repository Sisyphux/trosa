import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

import db


ROOT = Path(__file__).resolve().parents[1]


class TeamMembersApiTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        self.original_users = dict(db.USERS)
        self.original_users_list = list(db.USERS_LIST)
        db.DB_DIR = self.tempdir.name
        db.init_all_dbs()
        spec = importlib.util.spec_from_file_location('trosa_team_members_test_app', ROOT / 'app.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.module.app.config.update(TESTING=True)
        self.module.schedule_safety_backup = lambda *_args, **_kwargs: None
        self.client = self.module.app.test_client()
        self.assertEqual(self.client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)

    def tearDown(self):
        db.DB_DIR = self.original_db_dir
        db.USERS.clear()
        db.USERS.update(self.original_users)
        db.USERS_LIST[:] = self.original_users_list
        self.tempdir.cleanup()

    def test_legacy_users_are_migrated_without_losing_identity(self):
        conn = sqlite3.connect(Path(db.DB_DIR) / 'system.db')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(users)')}
        self.assertTrue({'username', 'password_hash', 'name', 'role', 'created_by', 'created_at', 'active'} <= columns)
        hamid = conn.execute("SELECT username, name, role, active FROM users WHERE id='hamid'").fetchone()
        self.assertEqual(hamid, ('hamid', 'Hamid', 'admin', 1))
        conn.close()

    def test_invitation_page_uses_root_static_asset_paths(self):
        response = self.client.get('/invite/example-token')
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/style.css?', body)
        self.assertIn('href="/visual-v2.css?', body)
        self.assertIn('src="/app.js?', body)

    def test_member_accepts_one_time_invitation_and_uses_name_as_account(self):
        created = self.client.post('/api/team/invitations', json={})
        self.assertEqual(created.status_code, 201, created.get_json())
        invitation_url = created.get_json()['invitation']['url']
        token = urlparse(invitation_url).path.rsplit('/', 1)[-1]

        invalid_password = self.client.post(f'/api/invitations/{token}/accept', json={
            'name': '李 雷', 'password': 'password-1234'
        })
        self.assertEqual(invalid_password.status_code, 400, invalid_password.get_json())

        accepted = self.client.post(f'/api/invitations/{token}/accept', json={
            'name': '李 雷', 'password': '123456'
        })
        self.assertEqual(accepted.status_code, 201, accepted.get_json())
        self.assertEqual(accepted.get_json()['user']['id'], '李 雷')
        self.assertTrue(Path(db.get_user_db_path('李 雷')).exists())

        self.client.post('/api/auth/logout')
        logged_in = self.client.post('/api/auth/login', json={
            'user': '李 雷', 'password': '123456'
        })
        self.assertEqual(logged_in.status_code, 200, logged_in.get_json())
        self.client.post('/api/auth/logout')
        self.client.post('/api/auth/login', json={'user': 'hamid'})
        disabled = self.client.post('/api/team/members/%E6%9D%8E%20%E9%9B%B7/disable')
        self.assertEqual(disabled.status_code, 200)
        self.client.post('/api/auth/logout')
        self.assertEqual(self.client.post('/api/auth/login', json={
            'user': '李 雷', 'password': '123456'
        }).status_code, 400)
        self.assertEqual(self.client.post(f'/api/invitations/{token}/accept', json={
            'name': '另一位', 'password': '123456'
        }).status_code, 404)

    def test_operation_log_records_authenticated_user(self):
        self.assertEqual(self.client.post('/api/team/invitations', json={}).status_code, 201)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        row = conn.execute("SELECT user_id FROM operation_logs WHERE target_type='team_invitation' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row[0], 'hamid')
        conn.close()


if __name__ == '__main__':
    unittest.main()
