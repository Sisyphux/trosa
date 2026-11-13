"""tools/trosa_mcp.py: MCP protocol, tool allow-list and argument mapping (offline)."""

import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))

import trosa_mcp  # noqa: E402
from test_agent_cli import FakeGateway  # noqa: E402

READS = {'customers', 'customer', 'contacts', 'activity', 'tasks', 'today', 'inbox', 'snapshot',
         'brief', 'workspace', 'timeline', 'search', 'whoami'}
WRITES = {'create_customer', 'update_customer', 'create_contact', 'update_contact', 'record_communication',
          'create_task', 'update_task', 'complete_task', 'reschedule', 'archive_customer',
          'restore_customer', 'undo'}


def rpc(method, params=None, request_id=1):
    message = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
    if params is not None:
        message['params'] = params
    return message


def call(name, arguments=None, gateway=None):
    reply = trosa_mcp.handle_message(rpc('tools/call', {'name': name, 'arguments': arguments or {}}), gateway)
    return reply['result']


def payload(result):
    return json.loads(result['content'][0]['text'])


class ProtocolTest(unittest.TestCase):
    def test_initialize_negotiates_version_and_advertises_tools(self):
        result = trosa_mcp.handle_message(rpc('initialize', {'protocolVersion': '2024-11-05'}))['result']
        self.assertEqual(result['protocolVersion'], '2024-11-05')
        self.assertIn('tools', result['capabilities'])
        self.assertEqual(result['serverInfo']['name'], 'trosa')
        unknown = trosa_mcp.handle_message(rpc('initialize', {'protocolVersion': '1999-01-01'}))['result']
        self.assertEqual(unknown['protocolVersion'], trosa_mcp.PROTOCOL_VERSIONS[0])

    def test_notifications_get_no_reply_and_unknown_methods_error(self):
        self.assertIsNone(trosa_mcp.handle_message({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
        self.assertEqual(trosa_mcp.handle_message(rpc('nope'))['error']['code'], -32601)
        self.assertEqual(trosa_mcp.handle_message(rpc('ping'))['result'], {})

    def test_stdio_loop_writes_one_json_line_per_request(self):
        lines = '\n'.join([
            json.dumps(rpc('initialize', {'protocolVersion': '2025-06-18'}, 1)),
            json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}),
            'not json',
            json.dumps(rpc('tools/list', {}, 2)),
        ]) + '\n'
        out = io.StringIO()
        trosa_mcp.serve(io.StringIO(lines), out)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([r.get('id') for r in replies], [1, None, 2])
        self.assertEqual(replies[1]['error']['code'], -32700)

    def test_real_process_handshake_keeps_stdout_clean(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('TROSA_')}
        env['HOME'] = '/nonexistent-home-for-test'
        stdin = '\n'.join([
            json.dumps(rpc('initialize', {'protocolVersion': '2025-06-18'}, 1)),
            json.dumps(rpc('tools/call', {'name': 'today', 'arguments': {}}, 2)),
        ]) + '\n'
        proc = subprocess.run([sys.executable, str(ROOT / 'tools' / 'trosa_mcp.py')], input=stdin,
                              capture_output=True, text=True, timeout=30, env=env)
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(len(replies), 2)
        self.assertTrue(replies[1]['result']['isError'])
        self.assertIn('AGENT_ACCESS', replies[1]['result']['content'][0]['text'])


class AllowListTest(unittest.TestCase):
    def test_exposes_exactly_the_agreed_tools(self):
        names = {tool['name'] for tool in trosa_mcp.tool_definitions()}
        self.assertEqual(names, READS | WRITES)

    def test_token_management_is_never_exposed(self):
        commands = {spec[0] for spec in trosa_mcp.TOOLS.values()}
        self.assertFalse(commands & {'issue-token', 'revoke-token', 'batch'})
        for name in ('issue_token', 'revoke_token', 'issue-token', 'revoke-token'):
            reply = trosa_mcp.handle_message(rpc('tools/call', {'name': name, 'arguments': {}}))
            self.assertEqual(reply['error']['code'], -32602)

    def test_source_never_mentions_release_credentials(self):
        source = (ROOT / 'tools' / 'trosa_mcp.py').read_text(encoding='utf-8')
        code = source.split('"""', 2)[2]  # skip the docstring, which explains the rule
        self.assertNotIn('workbench.env', code)

    def test_read_annotations(self):
        for tool in trosa_mcp.tool_definitions():
            self.assertEqual(tool['annotations']['readOnlyHint'], tool['name'] in READS, tool['name'])
            self.assertFalse(tool['inputSchema']['additionalProperties'])


class ArgumentMappingTest(unittest.TestCase):
    def test_reads_route_to_gateway(self):
        gateway = FakeGateway(customers=3)
        self.assertFalse(call('today', gateway=gateway)['isError'])
        self.assertEqual(gateway.calls[-1][0], '/today')
        call('customers', {'query': 'acme', 'limit': 10}, gateway)
        self.assertEqual(gateway.calls[-1], ('/customers', {'limit': 10, 'offset': 0, 'query': 'acme'}))
        call('timeline', {'customer_id': 7, 'limit': 5}, gateway)
        self.assertEqual(gateway.calls[-1], ('/customers/7/timeline', {'limit': 5}))
        call('search', {'query': 'quote', 'direction': 'inbound', 'from_date': '2026-10-01'}, gateway)
        self.assertEqual(gateway.calls[-1][1]['from_date'], '2026-10-01')
        self.assertEqual(gateway.calls[-1][1]['direction'], 'inbound')

    def test_snapshot_pages_everything(self):
        gateway = FakeGateway(customers=60, tasks=[{'id': 1, 'customer_id': 2}])
        data = payload(call('snapshot', gateway=gateway))['data']
        self.assertEqual(len(data['customers']), 60)
        self.assertEqual(data['open_task_count'], 1)

    def test_write_result_carries_action_id_and_undo_hint(self):
        gateway = FakeGateway()
        result = payload(call('create_task', {'customer_id': 12, 'title': '寄样', 'due': '2026-11-20'}, gateway))
        self.assertEqual(gateway.actions[-1][:3], ('create_task', 12, {'title': '寄样', 'due_date': '2026-11-20'}))
        self.assertTrue(result['action_id'].startswith('agact_'))
        self.assertIn(result['action_id'], result['undo_hint'])
        self.assertIn('undo', result['undo_hint'])

    def test_every_write_tool_returns_action_id(self):
        samples = {
            'create_customer': {'name': 'ACME', 'email': 'buyer@acme.test'},
            'update_customer': {'customer_id': 1, 'notes': 'x'},
            'create_contact': {'customer_id': 1, 'name': 'Bo'},
            'update_contact': {'contact_id': 3, 'title': 'Buyer'},
            'record_communication': {'customer_id': 1, 'content': '寄了样品', 'direction': 'outbound'},
            'create_task': {'customer_id': 1, 'title': 'a', 'due': '2026-11-20'},
            'update_task': {'task_id': 4, 'due': '2026-11-21'},
            'complete_task': {'task_id': 4, 'result': 'done'},
            'reschedule': {'task_id': 4, 'date': '2026-11-22'},
            'archive_customer': {'customer_id': 1},
            'restore_customer': {'customer_id': 1},
        }
        self.assertEqual(set(samples) | {'undo'}, WRITES)
        for name, arguments in samples.items():
            result = call(name, arguments, FakeGateway())
            self.assertFalse(result['isError'], (name, result))
            self.assertIn('action_id', payload(result), name)
            self.assertIn('undo_hint', payload(result), name)

    def test_values_starting_with_dash_stay_values(self):
        gateway = FakeGateway()
        call('record_communication', {'customer_id': 5, 'content': '--all 不是参数', 'date': '2026-11-12'}, gateway)
        self.assertEqual(gateway.actions[-1][2]['content'], '--all 不是参数')
        self.assertEqual(gateway.actions[-1][2]['follow_date'], '2026-11-12')

    def test_extra_object_merges_into_payload(self):
        gateway = FakeGateway()
        call('update_customer', {'customer_id': 9, 'extra': {'stage': 'sample'}}, gateway)
        self.assertEqual(gateway.actions[-1][:3], ('update_customer', 9, {'stage': 'sample'}))

    def test_undo_validates_action_id(self):
        gateway = FakeGateway()
        good = 'agact_' + 'x' * 24
        self.assertFalse(call('undo', {'action_id': good}, gateway)['isError'])
        self.assertEqual(gateway.undos, [good])
        for bad in ('../tokens', 'agact_short', 'agact_' + 'x' * 20 + '/../x'):
            result = call('undo', {'action_id': bad}, gateway)
            self.assertTrue(result['isError'], bad)
        self.assertEqual(gateway.undos, [good])

    def test_bad_arguments_are_tool_errors_not_crashes(self):
        gateway = FakeGateway()
        cases = [
            ('customer', {}),                                     # missing required
            ('customer', {'customer_id': '7'}),                   # wrong type
            ('customer', {'customer_id': True}),                  # bool is not an id
            ('customers', {'user_id': 2}),                        # unknown argument
            ('search', {'direction': 'sideways'}),                # enum
            ('create_task', {'customer_id': 1, 'title': 'a', 'due': 'tomorrow'}),  # CLI validation
            ('update_customer', {'customer_id': 1}),              # nothing to change
        ]
        for name, arguments in cases:
            result = call(name, arguments, gateway)
            self.assertTrue(result['isError'], (name, arguments))
            self.assertTrue(result['content'][0]['text'].startswith('错误'))
        self.assertEqual(gateway.actions, [])

    def test_server_errors_surface_with_cli_code(self):
        class Failing(FakeGateway):
            def get(self, path, **params):
                raise trosa_mcp.trosa_cli.CliError('当前 token 没有这项权限（403）：x', 4)
        result = call('today', gateway=Failing())
        self.assertTrue(result['isError'])
        self.assertIn('（4）', result['content'][0]['text'])


if __name__ == '__main__':
    unittest.main()
