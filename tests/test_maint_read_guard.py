import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location('trosa_maint_read', ROOT / 'tools' / 'trosa_maint_read.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


maint = _load()


class ReadOnlySqlGuardTest(unittest.TestCase):
    def test_accepts_plain_reads(self):
        for sql in (
            'SELECT 1',
            'select * from trosa.customer_records where company ilike \'%bold%\';',
            'WITH x AS (SELECT 1) SELECT * FROM x',
            'EXPLAIN SELECT 1',
            "SELECT 'drop table x; update y' AS text",   # keywords inside a literal
            'SELECT 1 -- delete everything',             # keyword inside a comment
        ):
            with self.subTest(sql=sql):
                self.assertTrue(maint.check_read_only_sql(sql))

    def test_rejects_writes_and_multi_statement(self):
        for sql in (
            '', '   ',
            'DELETE FROM trosa.customers',
            'UPDATE trosa.customers SET name=1',
            'INSERT INTO x VALUES (1)',
            'DROP TABLE x',
            'SELECT 1; DROP TABLE x',
            'SELECT 1; SELECT 2',
            'WITH d AS (DELETE FROM x RETURNING *) SELECT * FROM d',
            'SET ROLE postgres',
            'COPY x TO PROGRAM \'id\'',
            'SELECT pg_read_file(\'/etc/passwd\')',
            'SELECT pg_terminate_backend(1)',
            'CREATE TABLE x(a int)',
        ):
            with self.subTest(sql=sql):
                with self.assertRaises(maint.MaintError):
                    maint.check_read_only_sql(sql)


class ConfigBoundaryTest(unittest.TestCase):
    def test_missing_config_points_to_docs_and_not_release_env(self):
        original = maint.CONFIG_PATH
        try:
            maint.CONFIG_PATH = Path(tempfile.gettempdir()) / 'trosa-maint-missing' / 'readonly.json'
            with self.assertRaises(maint.MaintError) as ctx:
                maint._load_config()
            self.assertIn('MAINTAINER_DATA_ACCESS', str(ctx.exception))
        finally:
            maint.CONFIG_PATH = original

    def test_world_readable_config_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'readonly.json'
            path.write_text(json.dumps({'dsn': 'postgresql://x'}))
            os.chmod(path, 0o644)
            original = maint.CONFIG_PATH
            try:
                maint.CONFIG_PATH = path
                with self.assertRaises(maint.MaintError):
                    maint._load_config()
                os.chmod(path, 0o600)
                self.assertEqual(maint._load_config()['dsn'], 'postgresql://x')
            finally:
                maint.CONFIG_PATH = original


class RoleScriptTest(unittest.TestCase):
    def test_role_script_is_read_only_and_keeps_secrets_out(self):
        text = (ROOT / 'deploy' / 'postgres-production' / 'maint-readonly-role.sh').read_text()
        self.assertIn('default_transaction_read_only = on', text)
        self.assertNotRegex(text, r'GRANT\s+(INSERT|UPDATE|DELETE|ALL PRIVILEGES|TRUNCATE)')
        self.assertIn('REVOKE ALL ON ALL TABLES IN SCHEMA identity', text)


if __name__ == '__main__':
    unittest.main()
