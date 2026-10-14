import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import postgres_compat


class StatementTimeoutSettingTest(unittest.TestCase):
    def read(self, value):
        env = {} if value is None else {postgres_compat.STATEMENT_TIMEOUT_ENV: value}
        with mock.patch.dict(os.environ, env, clear=False):
            if value is None:
                os.environ.pop(postgres_compat.STATEMENT_TIMEOUT_ENV, None)
            return postgres_compat.statement_timeout_ms()

    def test_default_is_sixty_seconds(self):
        self.assertEqual(self.read(None), 60_000)
        self.assertEqual(self.read(''), 60_000)

    def test_explicit_value_and_zero_to_disable(self):
        self.assertEqual(self.read('15000'), 15_000)
        self.assertEqual(self.read('0'), 0)

    def test_unusable_values_fall_back_to_the_default(self):
        for bad in ('abc', '-5', '1.5', '10s'):
            self.assertEqual(self.read(bad), 60_000, bad)


if __name__ == '__main__':
    unittest.main()
