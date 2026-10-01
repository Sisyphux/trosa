"""tools/trosa_cli.py: credential loading, safe defaults and paging (offline)."""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import trosa_cli  # noqa: E402

TOKEN = 'trosa_pat_' + 'a' * 16 + '_' + 'b' * 48


class FakeGateway:
    root = 'https://example.test'

    def __init__(self, customers=0, tasks=()):
        self.customers, self.tasks, self.calls, self.proposals = customers, list(tasks), [], []

    def get(self, path, **params):
        self.calls.append((path, params))
        offset, limit = params.get('offset', 0), params.get('limit', 25)
        if path == '/customers':
            rows = [{'id': i, 'name': f'c{i}'} for i in range(1, self.customers + 1)]
        elif path == '/tasks':
            rows = self.tasks
        else:
            rows = []
        key = 'customers' if path == '/customers' else 'tasks'
        return {'success': True, 'data': {key: rows[offset:offset + limit]}}

    def page_all(self, path, key, max_items=5000, **params):
        return trosa_cli.Gateway.page_all(self, path, key, max_items, **params)

    def propose(self, action, customer_id, payload, key=None):
        self.proposals.append((action, customer_id, payload))
        return {'success': True}


class LoadTokenTest(unittest.TestCase):
    def test_env_token_wins_and_is_validated(self):
        self.assertEqual(trosa_cli.load_token({'TROSA_AGENT_TOKEN': TOKEN}, '/nonexistent'), TOKEN)
        with self.assertRaises(trosa_cli.CliError) as ctx:
            trosa_cli.load_token({'TROSA_AGENT_TOKEN': 'not-a-token'}, '/nonexistent')
        self.assertEqual(ctx.exception.code, 2)

    def test_missing_token_points_to_docs(self):
        with self.assertRaises(trosa_cli.CliError) as ctx:
            trosa_cli.load_token({}, '/nonexistent/agent.token')
        self.assertIn('AGENT_ACCESS', str(ctx.exception))

    def test_token_file_requires_private_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'agent.token')
            Path(path).write_text(TOKEN + '\n')
            os.chmod(path, 0o644)
            with self.assertRaises(trosa_cli.CliError):
                trosa_cli.load_token({}, path)
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            self.assertEqual(trosa_cli.load_token({}, path), TOKEN)

    def test_base_url_defaults_to_production_and_rejects_garbage(self):
        self.assertEqual(trosa_cli.base_url({}), 'https://app.trosa.space')
        self.assertEqual(trosa_cli.base_url({'TROSA_BASE_URL': 'http://127.0.0.1:5000/'}), 'http://127.0.0.1:5000')
        with self.assertRaises(trosa_cli.CliError):
            trosa_cli.base_url({'TROSA_BASE_URL': 'ftp://x'})


class ErrorMappingTest(unittest.TestCase):
    def test_auth_errors_have_distinct_exit_code(self):
        body = json.dumps({'error': {'code': 'permission', 'message': '当前 token 没有 crm:propose 权限'}}).encode()
        message, code = trosa_cli._error_message(403, body)
        self.assertEqual(code, 4)
        self.assertIn('crm:propose', message)
        self.assertEqual(trosa_cli._error_message(401, b'{}')[1], 4)
        self.assertEqual(trosa_cli._error_message(500, b'oops')[1], 1)


class PagingTest(unittest.TestCase):
    def parse(self, *argv):
        return trosa_cli.build_parser().parse_args(argv)

    def test_all_follows_offset_until_short_page(self):
        gateway = FakeGateway(customers=120)
        result = trosa_cli.run(self.parse('customers', '--all'), gateway)
        self.assertEqual(len(result['data']['customers']), 120)
        self.assertEqual([c[1]['offset'] for c in gateway.calls], [0, 50, 100])

    def test_snapshot_flags_customers_without_open_task(self):
        gateway = FakeGateway(customers=3, tasks=[{'id': 9, 'customer_id': 2, 'title': 't', 'due_date': '2026-10-05'}])
        data = trosa_cli.run(self.parse('snapshot'), gateway)['data']
        self.assertEqual(data['customers_without_open_task'], [1, 3])
        self.assertEqual(data['open_task_count'], 1)

    def test_proposals_validate_dates_and_never_write_directly(self):
        gateway = FakeGateway()
        trosa_cli.run(self.parse('propose-task', '7', '--title', '发样品', '--due', '2026-10-08'), gateway)
        self.assertEqual(gateway.proposals[0][0], 'create_task')
        with self.assertRaises(trosa_cli.CliError):
            trosa_cli.run(self.parse('propose-task', '7', '--title', 'x', '--due', '10月8日'), gateway)
        trosa_cli.run(self.parse('propose-communication', '7', '--content', '客户回复', '--direction', 'inbound'), gateway)
        self.assertEqual(gateway.proposals[1][2]['direction'], 'inbound')

    def test_cli_exposes_no_direct_write_command(self):
        names = trosa_cli.build_parser()._subparsers._group_actions[0].choices
        self.assertFalse([n for n in names if n.startswith(('write', 'delete', 'update', 'undo'))])

    def test_proposal_idempotency_key_is_deterministic(self):
        sent = []
        original = trosa_cli.request_json
        trosa_cli.request_json = lambda *a, **k: sent.append(k['headers']['Idempotency-Key']) or {}
        try:
            gateway = trosa_cli.Gateway(TOKEN, 'https://example.test')
            gateway.propose('create_task', 1, {'title': 'a', 'due_date': '2026-10-01'})
            gateway.propose('create_task', 1, {'due_date': '2026-10-01', 'title': 'a'})
            gateway.propose('create_task', 1, {'title': 'b', 'due_date': '2026-10-01'})
        finally:
            trosa_cli.request_json = original
        self.assertEqual(sent[0], sent[1])
        self.assertNotEqual(sent[0], sent[2])


class HumanOnlyTest(unittest.TestCase):
    def test_issue_token_refuses_without_interactive_terminal(self):
        args = trosa_cli.build_parser().parse_args(['issue-token', '--user', 'hamid'])
        with self.assertRaises(trosa_cli.CliError) as ctx:
            trosa_cli.run(args, root='https://example.test')
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn('交互式终端', str(ctx.exception))

    def test_issue_token_rejects_unknown_scope(self):
        args = trosa_cli.build_parser().parse_args(['issue-token', '--user', 'hamid', '--scopes', 'crm:read,admin'])
        with self.assertRaises(trosa_cli.CliError):
            trosa_cli.run(args, root='https://example.test')

    def test_token_file_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = trosa_cli.write_token_file(TOKEN, os.path.join(tmp, 'sub', 'agent.token'))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(trosa_cli.load_token({}, path), TOKEN)


if __name__ == '__main__':
    unittest.main()
