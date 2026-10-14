"""Web requests share one outbound budget, so a slow model or website read falls
back before Cloudflare's ~100 second origin timeout (error 524)."""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests

import app.engine as engine
import db


class OutboundBudgetTest(unittest.TestCase):
    def test_timeouts_pass_through_without_a_budget(self):
        self.assertEqual(engine.budget_timeout(180), 180)

    def test_budget_caps_timeouts_and_nested_blocks_only_tighten(self):
        with engine.outbound_budget(60):
            self.assertLessEqual(engine.budget_timeout(180), 60)
            self.assertGreater(engine.budget_timeout(180), 50)
            with engine.outbound_budget(500):
                self.assertLessEqual(engine.budget_timeout(180), 60)
            with engine.outbound_budget(5):
                self.assertLessEqual(engine.budget_timeout(180), 5)
            self.assertGreater(engine.budget_timeout(180), 50)
        self.assertEqual(engine.budget_timeout(180), 180)

    def test_exhausted_budget_fails_fast(self):
        with engine.outbound_budget(0):
            self.assertLessEqual(engine.budget_timeout(180), 0.1)

    def test_model_call_inside_a_budget_uses_the_capped_timeout(self):
        response = mock.Mock()
        response.json.return_value = {'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}]}
        with mock.patch.object(engine, 'DEEPSEEK_API_KEY', 'budget-test-key'), \
                mock.patch.object(engine.requests, 'post', return_value=response) as post:
            with engine.outbound_budget(60):
                self.assertEqual(engine._call_deepseek('hello'), 'ok')
            budgeted = post.call_args.kwargs['timeout']
            # Background callers (scheduler, Gmail workers) open no budget and keep 180s.
            engine._call_deepseek('hello')
            background = post.call_args.kwargs['timeout']
        self.assertLessEqual(budgeted, 60)
        self.assertEqual(background, 180)

    def test_website_read_is_capped_inside_a_budget(self):
        timeouts = []

        def refuse(request, timeout=None, context=None):
            timeouts.append(timeout)
            raise TimeoutError('timed out')

        with mock.patch.object(engine.urllib.request, 'urlopen', side_effect=refuse), \
                mock.patch.object(engine, '_browser_tools_content', return_value=('', {'attempted': False})), \
                mock.patch.object(engine, 'exa_fetch', return_value=('', {'error': 'skipped'})):
            with engine.outbound_budget(5):
                content, meta = engine.fetch_website_content('https://slow.example.test', return_meta=True)
        self.assertEqual(content, '')
        self.assertEqual(meta['error_code'], 'timeout')
        self.assertTrue(timeouts)
        self.assertTrue(all(timeout <= 5 for timeout in timeouts), timeouts)


class WebBudgetRouteTest(unittest.TestCase):
    """The web routes that wait on a model or a website run inside the budget."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        self.original_demo = os.environ.get('CRM_SEED_DEMO_DATA')
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.init_all_dbs()
        env_patch = mock.patch.dict(os.environ, {
            'LLM_BACKEND': 'deepseek',
            'DEEPSEEK_API_KEY': 'budget-test-key',
            'CRM_AI_CONFIG_FILE': os.path.join(self.tempdir.name, 'ai-config.env'),
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.addCleanup(engine._runtime_ai_config)

        spec = importlib.util.spec_from_file_location('crm_app_outbound_budget_test', ROOT / 'app.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.client = self.module.app.test_client()
        login = self.client.post('/api/auth/login', json={'user': 'hamid'})
        self.assertEqual(login.status_code, 200, login.get_json())

    def tearDown(self):
        db.cancel_safety_backup()
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    def test_analyze_reply_falls_back_when_the_model_is_slow(self):
        timeouts = []

        def slow_model(*args, **kwargs):
            timeouts.append(kwargs['timeout'])
            raise requests.exceptions.Timeout('read timed out')

        with mock.patch.object(engine.requests, 'post', side_effect=slow_model):
            response = self.client.post('/api/inbox/analyze-reply', json={
                'content': 'Hi, please send a quote for 500 pcs acrylic signs.',
                'direction': 'inbound',
            })
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertFalse(response.get_json()['analysis']['ai_available'])
        self.assertEqual(len(timeouts), 1)
        self.assertLessEqual(timeouts[0], self.module.WEB_OUTBOUND_BUDGET_SECONDS)

    def test_screenshot_recognition_gives_up_within_the_budget(self):
        timeouts = []

        def slow_vision(*args, **kwargs):
            timeouts.append(kwargs['timeout'])
            raise requests.exceptions.Timeout('read timed out')

        with mock.patch.object(engine.requests, 'post', side_effect=slow_vision):
            response = self.client.post('/api/inbox/extract-image', json={
                'image': 'data:image/png;base64,AAAA',
            })
        self.assertEqual(response.status_code, 503, response.get_json())
        self.assertEqual(len(timeouts), 1)
        self.assertLessEqual(timeouts[0], self.module.WEB_OUTBOUND_BUDGET_SECONDS)

    def test_smart_import_reads_the_website_inside_the_budget(self):
        seen = []

        def probe_website(url, return_meta=False, deep=False):
            seen.append(engine.budget_timeout(100))
            return '', {'ok': False, 'error_message': '连接网站超时'}

        with mock.patch.object(self.module, 'fetch_website_content', side_effect=probe_website), \
                mock.patch.object(self.module, 'exa_search', return_value=([], {'enabled': False})):
            response = self.client.post('/api/customers/smart-import', json={
                'company': 'Acme Signs', 'website': 'https://acme.example.test',
            })
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(seen[0], self.module.WEB_OUTBOUND_BUDGET_SECONDS)

    def test_raw_communication_summary_is_bounded_on_the_save_path(self):
        seen = []

        def probe_chat(prompt):
            seen.append(engine.budget_timeout(180))
            return ''

        with mock.patch.object(self.module, 'quick_chat', side_effect=probe_chat):
            fact = self.module._summarize_raw_communication(
                'Hi, please send a quote for 500 pcs acrylic signs.', 'inbound', 'follow_up',
            )
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(seen[0], self.module.WEB_OUTBOUND_BUDGET_SECONDS)
        self.assertTrue(fact)


if __name__ == '__main__':
    unittest.main()
