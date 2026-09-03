import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import db
from tools.postgres_source_inventory import discover_trosa_db_names


class PostgresSourceInventoryTests(unittest.TestCase):
    def _source_dir(self, users):
        directory = TemporaryDirectory()
        root = Path(directory.name)
        connection = sqlite3.connect(root / "system.db")
        connection.execute("CREATE TABLE users (id TEXT, username TEXT, name TEXT, active INTEGER)")
        connection.executemany(
            "INSERT INTO users VALUES (?, ?, ?, ?)",
            [(user_id, username, username or user_id, active) for user_id, username, active in users],
        )
        connection.commit()
        connection.close()
        for user in {username or user_id for user_id, username, _active in users}:
            sqlite3.connect(root / f"{user}.db").close()
        return directory, root

    def test_discovers_active_and_inactive_members_from_system_db(self):
        directory, root = self._source_dir([
            ("hamid", "hamid", 1),
            ("emma", "emma", 1),
            ("kelly", "kelly", 0),
        ])
        try:
            names, errors = discover_trosa_db_names(root)
        finally:
            directory.cleanup()
        self.assertEqual(names, ["system.db", "hamid.db", "emma.db", "kelly.db"])
        self.assertEqual(errors, [])

    def test_fails_closed_for_missing_and_unregistered_databases(self):
        directory, root = self._source_dir([
            ("hamid", "hamid", 1),
            ("emma", "emma", 1),
        ])
        try:
            (root / "emma.db").unlink()
            (root / "forgotten.db").touch()
            names, errors = discover_trosa_db_names(root)
        finally:
            directory.cleanup()
        self.assertEqual(names, ["system.db", "hamid.db", "emma.db"])
        self.assertIn(f"missing Trosa source: {root / 'emma.db'}", errors)
        self.assertIn(f"unregistered Trosa source database: {root / 'forgotten.db'}", errors)

    def test_backup_includes_registered_invited_member_database(self):
        directory, root = self._source_dir([
            ("hamid", "hamid", 1),
            ("amy", "amy", 1),
            ("kelley", "kelley", 1),
            ("emma", "emma", 1),
        ])
        try:
            with patch.object(db, "DB_DIR", str(root)):
                result = db.backup_database("dynamic-user-test")
            self.assertEqual(result["failed"], [])
            self.assertEqual(
                set(result["backed_up"]),
                {"system.db", "hamid.db", "amy.db", "kelley.db", "emma.db"},
            )
        finally:
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()
