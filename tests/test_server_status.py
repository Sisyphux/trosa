"""/api/server-status: reads the status file the server's self-check writes; a stale file is never "all fine"."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db  # noqa: E402


class ServerStatusTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.init_all_dbs()
        spec = importlib.util.spec_from_file_location('crm_app_server_status_test', ROOT / 'app.py')
        self.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app)
        self.path = Path(self.tempdir.name) / 'status.json'
        self.app.SERVER_STATUS_FILE = str(self.path)

    def tearDown(self):
        db.cancel_safety_backup()
        db.DB_DIR = self.original_db_dir
        self.tempdir.cleanup()

    def _call(self):
        view = self.app.server_status.__wrapped__
        with self.app.app.test_request_context('/api/server-status'):
            return view().get_json()

    def _write(self, minutes_ago, ok=True):
        stamp = (datetime.now(timezone(timedelta(hours=8))) - timedelta(minutes=minutes_ago)).isoformat(timespec='seconds')
        self.path.write_text(json.dumps({
            'checked_at': stamp, 'ok': ok,
            'checks': [{'name': '硬盘', 'group': '机器', 'ok': ok, 'detail': '已用 24%'}]}))

    def test_missing_file_is_not_available(self):
        self.assertEqual(self._call(), {'available': False})

    def test_fresh_result_passes_through(self):
        self._write(2)
        data = self._call()
        self.assertTrue(data['available'] and data['ok'] and not data['stale'])
        self.assertEqual(data['checks'][0]['group'], '机器')

    def test_old_result_is_stale_and_not_ok(self):
        self._write(40)
        data = self._call()
        self.assertTrue(data['stale'])
        self.assertFalse(data['ok'])

    def test_problem_is_reported(self):
        self._write(1, ok=False)
        self.assertFalse(self._call()['ok'])


if __name__ == '__main__':
    unittest.main()
