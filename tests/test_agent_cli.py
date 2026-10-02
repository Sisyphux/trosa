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
        self.actions, self.batches, self.undos = [], [], []
        self.whoami_payload = {'success': True, 'data': {
            'user': 'hamid', 'scopes': ['crm:read', 'crm:write'], 'expires_at': '',
            'days_remaining': None, 'renew_hint': ''}}

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

    def agent_get(self, path, **params):
        self.calls.append((path, params))
        return {'success': True, 'data': {'path': path, 'params': params}}

    def propose(self, action, customer_id, payload, key=None):
        self.proposals.append((action, customer_id, payload))
        return {'success': True}

    def act(self, action, customer_id, payload, key=None):
        self.actions.append((action, customer_id, payload, key))
        return {'success': True, 'data': {'action': {'id': 'agact_' + 'a' * 20, 'type': action,
                                                     'undo_token': 'undo-token'}}}

    def batch(self, actions, key=None):
        self.batches.append((actions, key))
        return {'success': True, 'data': {'action': {'id': 'agact_' + 'b' * 20, 'type': 'batch',
                                                     'undo_token': ''}}}

    def undo(self, action_id):
        self.undos.append(action_id)
        return {'success': True, 'data': {'action': {'id': action_id, 'status': 'undone'}}}

    def whoami(self):
        return self.whoami_payload


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

    def test_cli_exposes_reversible_writes_and_no_delete_command(self):
        names = set(trosa_cli.build_parser()._subparsers._group_actions[0].choices)
        self.assertFalse([n for n in names if 'delete' in n or n == 'permanent'])
        for expected in ('create-customer', 'update-customer', 'create-contact', 'update-contact',
                         'record-communication', 'update-communication', 'create-task', 'update-task',
                         'complete-task', 'reschedule', 'archive-customer', 'restore-customer', 'batch', 'undo'):
            self.assertIn(expected, names)

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


class DirectWriteCliTest(unittest.TestCase):
    def parse(self, *argv):
        return trosa_cli.build_parser().parse_args(argv)

    def test_create_customer_sends_payload_and_prints_undo(self):
        gateway = FakeGateway()
        result = trosa_cli.run(self.parse(
            'create-customer', '--name', 'Acme', '--company', 'Acme Co', '--email', 'a@acme.test'), gateway)
        action, customer_id, payload = gateway.actions[0][:3]
        self.assertEqual(action, 'create_customer')
        self.assertIsNone(customer_id)
        self.assertEqual(payload['contacts'][0]['email'], 'a@acme.test')
        self.assertIn('undo', result['undo_hint'])

    def test_reschedule_is_update_task_on_the_same_date(self):
        gateway = FakeGateway()
        trosa_cli.run(self.parse('update-task', '5', '--due', '2026-10-09'), gateway)
        trosa_cli.run(self.parse('reschedule', '5', '--date', '2026-10-09'), gateway)
        self.assertEqual([call[0] for call in gateway.actions], ['update_task', 'update_task'])
        self.assertEqual(gateway.actions[0][2]['remind_date'], '2026-10-09')
        self.assertEqual(gateway.actions[1][2]['remind_date'], '2026-10-09')

    def test_update_communication_sends_strip_quotes(self):
        gateway = FakeGateway()
        result = trosa_cli.run(self.parse('update-communication', '42', '--strip-quotes'), gateway)
        action, customer_id, payload = gateway.actions[0][:3]
        self.assertEqual(action, 'update_communication')
        self.assertIsNone(customer_id)
        self.assertEqual(payload['log_id'], 42)
        self.assertTrue(payload['strip_quotes'])
        self.assertIn('undo', result['undo_hint'])

    def test_write_idempotency_key_is_deterministic(self):
        sent = []
        original = trosa_cli.request_json
        trosa_cli.request_json = lambda *a, **k: sent.append(k['headers']['Idempotency-Key']) or {}
        try:
            gateway = trosa_cli.Gateway(TOKEN, 'https://example.test')
            gateway.act('create_task', 1, {'title': 'a', 'due_date': '2026-10-01'})
            gateway.act('create_task', 1, {'due_date': '2026-10-01', 'title': 'a'})
            gateway.act('create_task', 1, {'title': 'b', 'due_date': '2026-10-01'})
        finally:
            trosa_cli.request_json = original
        self.assertEqual(sent[0], sent[1])
        self.assertNotEqual(sent[0], sent[2])

    def test_batch_reads_actions_from_a_json_file(self):
        gateway = FakeGateway()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'batch.json')
            Path(path).write_text(json.dumps({'actions': [
                {'action': 'create_task', 'customer_id': 1, 'payload': {'title': 't', 'due_date': '2026-10-09'}},
                {'action': 'archive_customer', 'customer_id': 2, 'payload': {}},
            ]}))
            result = trosa_cli.run(self.parse('batch', path), gateway)
        self.assertEqual(len(gateway.batches[0][0]), 2)
        self.assertIn('undo', result['undo_hint'])

    def test_undo_posts_the_action_id(self):
        gateway = FakeGateway()
        trosa_cli.run(self.parse('undo', 'agact_abcdabcdabcdabcd'), gateway)
        self.assertEqual(gateway.undos, ['agact_abcdabcdabcdabcd'])

    def test_agent_read_commands_use_the_agent_surface(self):
        gateway = FakeGateway()
        trosa_cli.run(self.parse('workspace', '7'), gateway)
        trosa_cli.run(self.parse('search', '--query', '报价', '--offset', '50'), gateway)
        self.assertEqual(gateway.calls[0][0], '/customers/7/workspace')
        self.assertEqual(gateway.calls[1][0], '/messages/search')
        self.assertEqual(gateway.calls[1][1]['query'], '报价')
        self.assertEqual(gateway.calls[1][1]['offset'], 50)

    def test_whoami_surfaces_renew_hint(self):
        gateway = FakeGateway()
        gateway.whoami_payload = {'success': True, 'data': {
            'user': 'hamid', 'scopes': ['crm:read'], 'expires_at': '2026-10-02 00:00:00',
            'days_remaining': 0, 'renew_hint': '令牌将在 7 天内过期'}}
        result = trosa_cli.run(self.parse('whoami'), gateway)
        self.assertTrue(result['data']['authenticated'])
        self.assertIn('7 天内过期', result['renew_hint'])


class HumanOnlyTest(unittest.TestCase):
    def test_issue_token_refuses_without_interactive_terminal(self):
        args = trosa_cli.build_parser().parse_args(['issue-token', '--user', 'hamid'])
        with self.assertRaises(trosa_cli.CliError) as ctx:
            trosa_cli.run(args, root='https://example.test')
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn('交互式终端', str(ctx.exception))

    def test_issue_token_defaults_to_read_write_and_90_days(self):
        args = trosa_cli.build_parser().parse_args(['issue-token', '--user', 'hamid'])
        self.assertEqual(args.scopes, 'crm:read,crm:write')
        self.assertEqual(args.expires_days, 90)

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
