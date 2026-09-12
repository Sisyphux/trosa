import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import db
import serve
import serve_rehearsal


ROOT = Path(__file__).resolve().parents[1]


class RuntimeContractTests(unittest.TestCase):
    def test_formal_runtime_rejects_sqlite_without_touching_storage(self):
        with patch.dict(os.environ, {
            'CRM_ENV': 'production',
            'TRADE_OS_DATA_BACKEND': 'sqlite',
            'TRADE_OS_DATABASE_URL': '',
        }, clear=False):
            status = db.runtime_contract_status()
            self.assertEqual(status['backend'], 'sqlite')
            self.assertFalse(status['valid'])
            with self.assertRaisesRegex(RuntimeError, '必须同时设置'):
                db.require_formal_postgres_runtime()
            with self.assertRaisesRegex(RuntimeError, '必须同时设置'):
                db.init_all_dbs()

    def test_formal_runtime_accepts_only_explicit_postgresql(self):
        with patch.dict(os.environ, {
            'CRM_ENV': 'production',
            'TRADE_OS_DATA_BACKEND': 'postgres',
            'TRADE_OS_DATABASE_URL': 'postgresql://127.0.0.1/trosa',
        }, clear=False):
            status = db.runtime_contract_status()
            self.assertEqual(status['contract'], 'trosa-postgresql-v1')
            self.assertEqual(status['backend'], 'postgresql')
            self.assertTrue(status['valid'])
            db.require_formal_postgres_runtime()

    def test_official_serve_entrypoint_stops_before_initialization(self):
        with patch.dict(os.environ, {
            'CRM_ENV': 'production',
            'TRADE_OS_DATA_BACKEND': 'sqlite',
            'TRADE_OS_DATABASE_URL': '',
        }, clear=False), patch.object(serve, 'init_all_dbs') as initialize:
            with self.assertRaisesRegex(RuntimeError, '必须同时设置'):
                serve.main()
            initialize.assert_not_called()

    def test_desktop_entrypoint_refuses_formal_or_rehearsal_start(self):
        environment = os.environ.copy()
        environment.update({
            'CRM_ENV': 'production',
            'TRADE_OS_DATA_BACKEND': 'postgres',
            'TRADE_OS_DATABASE_URL': 'postgresql://127.0.0.1/trosa',
        })
        result = subprocess.run(
            [sys.executable, str(ROOT / 'desktop.py')],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('不是正式 Trosa 启动入口', result.stderr + result.stdout)

    def test_rehearsal_entrypoint_refuses_non_loopback_database(self):
        with patch.dict(os.environ, {
            'CRM_ENV': 'rehearsal',
            'TROSA_REHEARSAL': '1',
            'TRADE_OS_DATA_BACKEND': 'postgres',
            'TRADE_OS_DATABASE_URL': 'postgresql://db.example/tradeos',
        }, clear=False), patch.object(serve_rehearsal, 'init_all_dbs') as initialize:
            with self.assertRaisesRegex(RuntimeError, 'loopback'):
                serve_rehearsal.main()
            initialize.assert_not_called()

    def test_invalid_formal_ping_is_not_reported_as_healthy(self):
        original = {key: os.environ.get(key) for key in (
            'CRM_ENV', 'CRM_SESSION_SECRET', 'TRADE_OS_DATA_BACKEND', 'TRADE_OS_DATABASE_URL',
        )}
        try:
            os.environ.update({
                'CRM_ENV': 'production',
                'CRM_SESSION_SECRET': 'runtime-contract-test-' + ('x' * 40),
                'TRADE_OS_DATA_BACKEND': 'sqlite',
                'TRADE_OS_DATABASE_URL': '',
            })
            spec = importlib.util.spec_from_file_location('trosa_runtime_contract_app', ROOT / 'app.py')
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            response = module.app.test_client().get('/api/network/ping')
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.get_json()['backend'], 'sqlite')
            self.assertEqual(response.get_json()['runtime_contract'], 'trosa-postgresql-v1')
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_production_entrypoints_advertise_the_same_contract(self):
        self.assertIn('require_formal_postgres_runtime()', (ROOT / 'serve.py').read_text(encoding='utf-8'))
        env_example = (ROOT / 'deploy/macos/env.production.example').read_text(encoding='utf-8')
        self.assertIn('TRADE_OS_DATA_BACKEND=postgres', env_example)
        self.assertNotIn('TRADE_OS_DATA_BACKEND=sqlite', env_example)
        self.assertIn('trosa-postgresql-v1', (ROOT / 'deploy/cloud/status-remote.sh').read_text(encoding='utf-8'))
        self.assertIn('trosa-postgresql-v1', (ROOT / 'deploy/cloud/publish-remote.sh').read_text(encoding='utf-8'))
        self.assertIn('formal_runtime', (ROOT / 'deploy/cloud/status-remote.sh').read_text(encoding='utf-8'))
        self.assertIn('formal_runtime', (ROOT / 'deploy/cloud/publish-remote.sh').read_text(encoding='utf-8'))
        self.assertIn('TRADE_OS_DEV_SQLITE=1', (ROOT / 'desktop.py').read_text(encoding='utf-8'))
        self.assertIn('formal_runtime', (ROOT / 'Mac启动器.command').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
