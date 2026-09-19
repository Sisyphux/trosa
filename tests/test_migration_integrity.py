"""Migration directory integrity regressions.

Guards the parallel-development contract: ``migrations/`` is the single source
of truth, its numbering is unique (gaps are expected when parallel tasks merge
out of order), and both the runtime (``db.py``) and the rehearsal/apply tool see
exactly the same files.  These
tests need no database and run in the fast release gate, so a task cannot merge
a migration that the formal apply path would silently miss.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import check_migrations  # noqa: E402
import db  # noqa: E402
from tools import unified_postgres_migration  # noqa: E402


class MigrationDirectoryIntegrityTest(unittest.TestCase):
    def test_repository_migrations_are_unique_and_contiguous(self):
        problems = check_migrations.check_directory(str(ROOT / "migrations"))
        self.assertEqual(problems, [])

    def test_runtime_registry_equals_directory(self):
        self.assertEqual(
            [Path(path).name for path in db._postgres_migration_paths()],
            check_migrations.migration_files(str(ROOT / "migrations")),
        )

    def test_rehearsal_registry_equals_directory(self):
        self.assertEqual(
            [Path(path).name for path in unified_postgres_migration.SCHEMA_PATHS],
            check_migrations.migration_files(str(ROOT / "migrations")),
        )

    def test_runtime_and_rehearsal_registries_agree(self):
        self.assertEqual(
            [Path(path).name for path in db._postgres_migration_paths()],
            [Path(path).name for path in unified_postgres_migration.SCHEMA_PATHS],
        )


class MigrationCheckerRejectionTest(unittest.TestCase):
    def _write(self, directory, name):
        Path(directory, name).write_text("BEGIN; SELECT 1; COMMIT;\n", encoding="utf-8")

    def test_duplicate_number_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "0001_a.sql")
            self._write(tmp, "0002_b.sql")
            self._write(tmp, "0002_c.sql")
            problems = check_migrations.check_directory(tmp)
            self.assertTrue(any("duplicate migration number 0002" in p for p in problems), problems)

    def test_gap_is_reported_but_not_fatal(self):
        # Parallel tasks reserve numbers from a shared pool and may merge out
        # of order; a gap is expected and must not block a tree.
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "0001_a.sql")
            self._write(tmp, "0003_c.sql")
            self.assertEqual(check_migrations.check_directory(tmp), [])
            self.assertEqual(check_migrations.missing_numbers(tmp), ["0002"])

    def test_real_repository_has_no_duplicates_or_invalid_names(self):
        # 并行任务会预留编号、可能乱序合并，因此编号空档是预期且非致命
        # （见 README 与 test_gap_is_reported_but_not_fatal）；这里只要求目录
        # 本身合法：文件名规范、编号唯一。
        problems = check_migrations.check_directory(str(ROOT / "migrations"))
        self.assertEqual(problems, [])

    def test_invalid_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "1_bad.sql")
            self._write(tmp, "0002_Not_Snake.sql")
            problems = check_migrations.check_directory(tmp)
            self.assertEqual(len([p for p in problems if "invalid migration filename" in p]), 2, problems)

    def test_clean_directory_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "0001_a.sql")
            self._write(tmp, "0002_b.sql")
            self.assertEqual(check_migrations.check_directory(tmp), [])

    def test_cli_reports_failure_for_missing_directory(self):
        status = check_migrations.main(["check_migrations.py", "--dir", "/nonexistent-trosa-root"])
        self.assertEqual(status, 1)


if __name__ == "__main__":
    unittest.main()
