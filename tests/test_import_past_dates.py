import importlib.util
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('CRM_DB_PATH', os.path.join(tempfile.mkdtemp(), 'import_dates.db'))
os.environ.setdefault('CRM_ENV', 'development')
os.environ.setdefault('TRADE_OS_DEV_SQLITE', '1')
_spec = importlib.util.spec_from_file_location('crm_app_import_dates_test', ROOT / 'app.py')
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)


class ImportPastDateTests(unittest.TestCase):
    today = datetime(2026, 9, 3)

    def test_future_year_is_rolled_back_keeping_month_and_day(self):
        self.assertEqual(app._past_import_date('2027-11-28', self.today), '2025-11-28')
        self.assertEqual(app._past_import_date('2026-10-18', self.today), '2025-10-18')

    def test_valid_dates_are_untouched(self):
        self.assertEqual(app._past_import_date('2026-09-03', self.today), '2026-09-03')
        self.assertEqual(app._past_import_date('2025-05-23', self.today), '2025-05-23')

    def test_leap_day_and_unparseable_text(self):
        self.assertEqual(app._past_import_date('2028-02-29', self.today), '2026-02-28')
        self.assertEqual(app._past_import_date('', self.today), '')


if __name__ == '__main__':
    unittest.main()
