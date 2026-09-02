import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

import db


ROOT = Path(__file__).resolve().parents[1]


class TeamMembersApiTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
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
        self.tempdir.cleanup()

    def test_legacy_users_are_migrated_without_losing_identity(self):
        conn = sqlite3.connect(Path(db.DB_DIR) / 'system.db')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(users)')}
        self.assertTrue({'username', 'password_hash', 'name', 'role', 'created_by', 'created_at', 'active'} <= columns)
        hamid = conn.execute("SELECT username, name, role, active FROM users WHERE id='hamid'").fetchone()
        self.assertEqual(hamid, ('hamid', 'Hamid', 'admin', 1))
        conn.close()

    def test_admin_can_create_login_and_disable_member(self):
        created = self.client.post('/api/team/members', json={
            'username': 'alice', 'name': 'Alice', 'password': 'correct horse battery staple'
        })
        self.assertEqual(created.status_code, 201, created.get_json())
        self.assertTrue(Path(db.get_user_db_path('alice')).exists())

        self.client.post('/api/auth/logout')
        logged_in = self.client.post('/api/auth/login', json={
            'user': 'alice', 'password': 'correct horse battery staple'
        })
        self.assertEqual(logged_in.status_code, 200, logged_in.get_json())
        self.client.post('/api/auth/logout')
        self.client.post('/api/auth/login', json={'user': 'hamid'})
        disabled = self.client.post('/api/team/members/alice/disable')
        self.assertEqual(disabled.status_code, 200)
        self.client.post('/api/auth/logout')
        self.assertEqual(self.client.post('/api/auth/login', json={
            'user': 'alice', 'password': 'correct horse battery staple'
        }).status_code, 400)

    def test_operation_log_records_authenticated_user(self):
        self.assertEqual(self.client.post('/api/team/members', json={
            'username': 'bob', 'name': 'Bob', 'password': 'password-1234'
        }).status_code, 201)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        row = conn.execute("SELECT user_id FROM operation_logs WHERE target_type='team_member' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row[0], 'hamid')
        conn.close()


if __name__ == '__main__':
    unittest.main()
