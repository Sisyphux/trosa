"""Unit tests for the SQLite-source Customer boundary classifier."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.sqlite_boundary_audit import compare


class CompareTest(unittest.TestCase):
    def test_matching_bindings_are_ok(self):
        sqlite_rows = [{'id': 1, 'customer_id': 10}, {'id': 2, 'customer_id': 11}]
        pg_rows = {1: {'bound': 10, 'row_id': 'a'}, 2: {'bound': 11, 'row_id': 'b'}}
        buckets = compare(sqlite_rows, pg_rows)
        self.assertEqual(len(buckets['ok']), 2)
        self.assertEqual(buckets['fix'], [])

    def test_wrong_binding_is_a_deterministic_fix(self):
        buckets = compare(
            [{'id': 1, 'customer_id': 10}],
            {1: {'bound': 20, 'row_id': 'a'}},
        )
        self.assertEqual(len(buckets['fix']), 1)
        self.assertEqual(buckets['fix'][0]['id'], 1)

    def test_missing_pg_ref_goes_to_manual_review(self):
        buckets = compare([{'id': 1, 'customer_id': 10}], {})
        self.assertEqual(buckets['fix'], [])
        self.assertEqual(len(buckets['manual_missing_ref']), 1)

    def test_unbound_postgres_row_goes_to_manual_review(self):
        buckets = compare(
            [{'id': 1, 'customer_id': 10}],
            {1: {'bound': None, 'row_id': 'a'}},
        )
        self.assertEqual(len(buckets['manual_missing_ref']), 1)

    def test_legacy_row_without_customer_is_manual_only(self):
        buckets = compare(
            [{'id': 1, 'customer_id': None}],
            {1: {'bound': 10, 'row_id': 'a'}},
        )
        self.assertEqual(len(buckets['manual_no_sqlite_customer']), 1)
        self.assertEqual(buckets['fix'], [])

    def test_runtime_rows_without_sqlite_counterpart_are_counted_not_flagged(self):
        buckets = compare([], {5: {'bound': 10, 'row_id': 'a'}})
        self.assertEqual(len(buckets['pg_only']), 1)
        self.assertEqual(buckets['fix'], [])


if __name__ == '__main__':
    unittest.main()
