import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import sys

sys.path.insert(0, str(ROOT))

import db


class PrivacyPageTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        db.DB_DIR = self.tempdir.name
        db.init_all_dbs()
        spec = importlib.util.spec_from_file_location('trosa_privacy_page_test', ROOT / 'app.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.module.app.config.update(TESTING=True)
        self.module.schedule_safety_backup = lambda *_args, **_kwargs: None

    def tearDown(self):
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.original_db_dir
        self.tempdir.cleanup()

    def test_privacy_page_is_public_html(self):
        response = self.module.app.test_client().get('/privacy')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.mimetype.startswith('text/html'))
        body = response.get_data(as_text=True)
        self.assertIn('sela', body)
        self.assertIn('Google API Services User Data Policy', body)
        response.close()


if __name__ == '__main__':
    unittest.main()
