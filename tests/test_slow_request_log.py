import importlib.util
import itertools
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_slow_request_log_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


class SlowRequestLogTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.init_all_dbs()
        self.module = load_app()
        self.client = self.module.app.test_client()

    def tearDown(self):
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.original_db_dir
        self.tempdir.cleanup()

    def slow_lines(self, threshold, path='/api/network/ping?secret=needle'):
        # A fake clock that moves one second per reading makes every request "slow"
        # deterministically, so the threshold alone decides whether a line is written.
        clock = itertools.count(0, 1.0)
        with mock.patch.dict(os.environ, {'TRADE_OS_SLOW_REQUEST_LOG_MS': threshold}), \
                mock.patch.object(self.module.time, 'monotonic', side_effect=lambda: next(clock)):
            with self.assertLogs(self.module.logger, level=logging.WARNING) as captured:
                self.module.logger.warning('sentinel')
                self.client.get(path)
        return [line for line in captured.output if 'slow request' in line]

    def test_a_request_over_the_threshold_is_logged_without_query_values(self):
        lines = self.slow_lines('1000')
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('GET /api/network/ping', lines[0])
        self.assertIn('status=200', lines[0])
        self.assertIn('args=secret', lines[0])
        self.assertNotIn('needle', lines[0])

    def test_zero_disables_and_a_high_threshold_logs_nothing(self):
        self.assertEqual(self.slow_lines('0'), [])
        self.assertEqual(self.slow_lines('86400000'), [])

    def test_unusable_threshold_falls_back_to_one_second(self):
        with mock.patch.dict(os.environ, {'TRADE_OS_SLOW_REQUEST_LOG_MS': 'abc'}):
            self.assertEqual(self.module._slow_request_threshold_ms(), 1000)


if __name__ == '__main__':
    unittest.main()
