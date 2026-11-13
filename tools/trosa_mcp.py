#!/usr/bin/env python3
"""Stdio MCP server that lets chat AIs (Claude Desktop, Claude Code, ...) use the
Trosa Agent Gateway.

It is a thin adapter over ``tools/trosa_cli.py``: every MCP tool is translated
into the matching CLI command line and run through the CLI's own parser,
validation, HTTP client and token loading, so the two can never drift apart.

What is exposed (a fixed allow-list, see ``TOOLS``):
  * reads   -- customers, customer, contacts, activity, tasks, today, inbox,
               snapshot, brief, workspace, timeline, search, whoami
  * reversible writes (need ``crm:write``) -- create/update customer and
               contact, record communication, create/update/complete task,
               reschedule, archive/restore customer, undo.
Every write result carries ``action_id`` and ``undo_hint``.  Token management
(``issue-token`` / ``revoke-token``) is never exposed, nothing here reads
``workbench.env``, and there is no delete tool (the gateway refuses deletes for
any scope anyway).

Credentials and base URL are the CLI's: ``TROSA_AGENT_TOKEN`` or
``~/.config/trosa/agent.token``; ``TROSA_BASE_URL`` (default production).
Setup: ``docs/AGENT_ACCESS.md``.  Pure standard library; speaks newline
delimited JSON-RPC 2.0 on stdin/stdout (MCP stdio transport).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trosa_cli  # noqa: E402

SERVER_NAME = 'trosa'
SERVER_VERSION = '1.0.0'
PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
ACTION_ID_PATTERN = re.compile(r'agact_[A-Za-z0-9_-]{16,64}')
DATE_HINT = '日期 YYYY-MM-DD'


def _p(name, kind='str', desc='', required=False, flag=None, enum=None, positional=False):
    return {'name': name, 'kind': kind, 'desc': desc, 'required': required,
            'flag': flag or '--' + name.replace('_', '-'), 'enum': enum, 'positional': positional}


def _paging(default_limit=25):
    return [_p('limit', 'int', f'每页条数（最多 50，默认 {default_limit}）'),
            _p('offset', 'int', '翻页偏移'),
            _p('all', 'bool', '自动翻页取全部')]


CUSTOMER_ID = _p('customer_id', 'int', '客户 ID', required=True, positional=True)
DIRECTION = _p('direction', 'str', '沟通方向', enum=list(trosa_cli.DIRECTIONS))
EXTRA_JSON = _p('extra', 'object', '附加字段（直接合并进写入内容，字段名同网关）', flag='--json')

# name -> (cli command, read_only, description, params)
TOOLS = {
    'whoami': ('whoami', True, '检查令牌是否有效、属于哪个成员、有哪些权限。', []),
    'today': ('today', True, '今天及逾期的待办。', []),
    'customers': ('customers', True,
                  '搜索客户。last_contact/next_follow_up 只是派生日期摘要：是否联系过看 activity，'
                  '有没有下一步看 tasks。',
                  [_p('query', 'str', '关键词（公司名、邮箱、国家等）')] + _paging()),
    'customer': ('customer', True, '单个客户详情。', [CUSTOMER_ID]),
    'contacts': ('contacts', True, '客户的联系人。', [CUSTOMER_ID]),
    'activity': ('activity', True, '沟通记录（可按客户、关键词筛选），看 type 与 direction 判断谁联系了谁。',
                 [_p('customer_id', 'int', '只看这个客户', flag='--customer'),
                  _p('query', 'str', '关键词')] + _paging()),
    'tasks': ('tasks', True, '未完成待办。',
              [_p('customer_id', 'int', '只看这个客户', flag='--customer')] + _paging()),
    'inbox': ('inbox', True, '待整理的 Inbox 条目。', []),
    'snapshot': ('snapshot', True,
                 '全部客户 + 全部未完成待办；customers_without_open_task 即没有下一步的客户。', []),
    'brief': ('brief', True, '今天的工作简报。', []),
    'workspace': ('workspace', True, '单个客户工作区：承诺、最近事实、信息缺口。', [CUSTOMER_ID]),
    'timeline': ('timeline', True, '单个客户完整沟通时间线。',
                 [CUSTOMER_ID, _p('limit', 'int', '条数（默认 50）')]),
    'search': ('search', True, '跨客户消息搜索。',
               [_p('query', 'str', '关键词'), _p('country', 'str', '国家'), DIRECTION,
                _p('from_date', 'str', '起始' + DATE_HINT, flag='--from'),
                _p('to_date', 'str', '结束' + DATE_HINT, flag='--to')] + _paging(50)),
    'create_customer': ('create-customer', False,
                        '新建客户（可撤销：撤销=归档）。命中已有客户的唯一邮箱/手机号时不写入，返回 duplicate_candidate。',
                        [_p('name'), _p('company'), _p('country'), _p('website'),
                         _p('source', 'str', '客户来源（固定选项之一）'), _p('notes'),
                         _p('email', 'str', '联系人邮箱'), _p('phone', 'str', '联系人电话'),
                         _p('next_follow_up', 'str', '首个待办' + DATE_HINT),
                         _p('task_title', 'str', '首个待办的明确动作'), EXTRA_JSON]),
    'update_customer': ('update-customer', False, '修改客户资料（可撤销）。',
                        [CUSTOMER_ID, _p('name'), _p('company'), _p('country'), _p('website'),
                         _p('field'), _p('industry'), _p('company_size'), _p('annual_revenue'),
                         _p('profile'), _p('notes'), _p('tags'), EXTRA_JSON]),
    'create_contact': ('create-contact', False, '给客户新增联系人（可撤销）。',
                       [CUSTOMER_ID, _p('name'), _p('title', 'str', '职位'), _p('email'), _p('phone'),
                        _p('whatsapp'), _p('linkedin'), EXTRA_JSON]),
    'update_contact': ('update-contact', False, '修改联系人（可撤销）。',
                       [_p('contact_id', 'int', '联系人 ID', required=True, positional=True),
                        _p('name'), _p('title', 'str', '职位'), _p('email'), _p('phone'),
                        _p('whatsapp'), _p('linkedin'), _p('notes'), EXTRA_JSON]),
    'record_communication': ('record-communication', False,
                             '记录一条已经发生的沟通事实（可撤销）。只写实际发生的事，不写推测。',
                             [CUSTOMER_ID,
                              _p('content', 'str', '实际发生的事实', required=True), DIRECTION,
                              _p('date', 'str', '沟通发生' + DATE_HINT),
                              _p('activity_type', 'str', '渠道，如 email / whatsapp / call', flag='--type'),
                              _p('result'), _p('contact_id', 'int', '联系人 ID', flag='--contact'),
                              _p('next_task', 'str', '下一步明确动作'),
                              _p('next_follow_up', 'str', '下一步' + DATE_HINT), EXTRA_JSON]),
    'create_task': ('create-task', False, '新建待办（可撤销）。必须有明确动作和日期。',
                    [CUSTOMER_ID, _p('title', 'str', '明确动作', required=True),
                     _p('due', 'str', DATE_HINT, required=True), _p('reason'), EXTRA_JSON]),
    'update_task': ('update-task', False, '修改待办（可撤销）。',
                    [_p('task_id', 'int', '待办 ID', required=True, positional=True),
                     _p('title'), _p('content'), _p('reason'),
                     _p('due', 'str', '新' + DATE_HINT), EXTRA_JSON]),
    'complete_task': ('complete-task', False, '完成待办并记录结果（可撤销）。',
                      [_p('task_id', 'int', '待办 ID', required=True, positional=True),
                       _p('result'), DIRECTION, _p('next_task', 'str', '下一步明确动作'),
                       _p('next_follow_up', 'str', '下一步' + DATE_HINT), EXTRA_JSON]),
    'reschedule': ('reschedule', False, '调整待办日期（可撤销）。',
                   [_p('task_id', 'int', '待办 ID', required=True, positional=True),
                    _p('date', 'str', '新' + DATE_HINT, required=True)]),
    'archive_customer': ('archive-customer', False, '把客户归档到回收站（可撤销，可用 restore_customer 恢复）。',
                         [CUSTOMER_ID]),
    'restore_customer': ('restore-customer', False, '从回收站恢复客户（可撤销）。', [CUSTOMER_ID]),
    'undo': ('undo', False, '撤销之前的一次写入；action_id 来自写入结果。',
             [_p('action_id', 'str', '写入结果里的 action_id（agact_ 开头）', required=True, positional=True)]),
}

FORBIDDEN_COMMANDS = frozenset({'issue-token', 'revoke-token'})
assert not FORBIDDEN_COMMANDS & {spec[0] for spec in TOOLS.values()}


class ToolError(Exception):
    pass


def _schema(param):
    if param['kind'] == 'int':
        prop = {'type': 'integer'}
    elif param['kind'] == 'bool':
        prop = {'type': 'boolean'}
    elif param['kind'] == 'object':
        prop = {'type': 'object'}
    else:
        prop = {'type': 'string'}
    if param['enum']:
        prop['enum'] = param['enum']
    if param['desc']:
        prop['description'] = param['desc']
    return prop


def tool_definitions():
    tools = []
    for name, (_command, read_only, desc, params) in TOOLS.items():
        tools.append({
            'name': name,
            'description': desc + ('' if read_only else ' 结果里带 action_id 与 undo_hint。'),
            'inputSchema': {
                'type': 'object',
                'properties': {p['name']: _schema(p) for p in params},
                'required': [p['name'] for p in params if p['required']],
                'additionalProperties': False,
            },
            'annotations': {'readOnlyHint': read_only, 'destructiveHint': False,
                            'idempotentHint': True, 'openWorldHint': False},
        })
    return tools


def build_argv(name, arguments):
    """Translate MCP arguments into a trosa_cli command line (allow-listed)."""
    if name not in TOOLS:
        raise ToolError(f'未知工具 {name}')
    command, _read_only, _desc, params = TOOLS[name]
    arguments = arguments or {}
    if not isinstance(arguments, dict):
        raise ToolError('arguments 必须是对象')
    known = {p['name']: p for p in params}
    unknown = sorted(set(arguments) - set(known))
    if unknown:
        raise ToolError('不支持的参数：' + ', '.join(unknown))
    options, positionals = [], []
    for param in params:
        value = arguments.get(param['name'])
        if value is None or value == '':
            if param['required']:
                raise ToolError(f'缺少必填参数 {param["name"]}')
            continue
        kind = param['kind']
        if kind == 'int':
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolError(f'{param["name"]} 必须是整数')
            text = str(value)
        elif kind == 'bool':
            if not isinstance(value, bool):
                raise ToolError(f'{param["name"]} 必须是 true/false')
            if value:
                options.append(param['flag'])
            continue
        elif kind == 'object':
            if not isinstance(value, dict):
                raise ToolError(f'{param["name"]} 必须是 JSON 对象')
            text = json.dumps(value, ensure_ascii=False)
        else:
            if not isinstance(value, str):
                raise ToolError(f'{param["name"]} 必须是字符串')
            text = value
        if param['enum'] and text not in param['enum']:
            raise ToolError(f'{param["name"]} 只能是 ' + ' / '.join(param['enum']))
        if param['positional']:
            positionals.append(text)
        else:
            # "--flag=value" keeps values that start with "-" from being read as options.
            options.append(f'{param["flag"]}={text}')
    if name == 'undo' and not ACTION_ID_PATTERN.fullmatch(positionals[0]):
        raise ToolError('action_id 格式不正确（应为写入结果里的 agact_...）')
    return [command] + options + (['--'] + positionals if positionals else [])


def _parse(argv):
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stderr):
            return trosa_cli.build_parser().parse_args(argv)
    except SystemExit:
        message = stderr.getvalue().strip().splitlines()
        raise ToolError('参数不正确：' + (message[-1] if message else ' '.join(argv))) from None


def with_action_id(result):
    """Lift ``action_id`` to the top and rewrite the undo hint for MCP callers."""
    if not isinstance(result, dict):
        return result
    data = result.get('data') if isinstance(result.get('data'), dict) else {}
    action = data.get('action') if isinstance(data.get('action'), dict) else {}
    enriched = dict(result)
    if action.get('id') and not result.get('duplicate_candidate'):
        enriched['action_id'] = action['id']
        enriched['undo_hint'] = (f'撤销：调用 undo 工具，action_id={action["id"]}'
                                 f'（命令行等价：python3 tools/trosa_cli.py undo {action["id"]}）')
    return enriched


def call_tool(name, arguments, gateway=None):
    """Run one tool; returns the MCP ``tools/call`` result object."""
    try:
        argv = build_argv(name, arguments)
        args = _parse(argv)
        if args.command in FORBIDDEN_COMMANDS:  # defence in depth; build_argv already allow-lists
            raise ToolError('该命令只能由成员本人在终端运行')
        result = trosa_cli.run(args, gateway=gateway)
        if not TOOLS[name][1]:
            result = with_action_id(result)
    except (ToolError, trosa_cli.CliError) as error:
        code = getattr(error, 'code', None)
        prefix = f'错误（{code}）：' if code else '错误：'
        return {'content': [{'type': 'text', 'text': prefix + str(error)}], 'isError': True}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return {'content': [{'type': 'text', 'text': text}], 'isError': False}


def _response(request_id, result=None, error=None):
    message = {'jsonrpc': '2.0', 'id': request_id}
    if error is not None:
        message['error'] = error
    else:
        message['result'] = result
    return message


def handle_message(message, gateway=None):
    """Handle one JSON-RPC message; returns the reply dict or None for notifications."""
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
        return _response(None, error={'code': -32600, 'message': 'Invalid Request'})
    method = message.get('method')
    is_request = 'id' in message
    request_id = message.get('id')
    params = message.get('params') or {}
    if not is_request:
        return None  # notifications (initialized, cancelled, ...) need no reply
    if method == 'initialize':
        requested = params.get('protocolVersion') if isinstance(params, dict) else None
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _response(request_id, {
            'protocolVersion': version,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
            'instructions': ('Trosa CRM。读操作随意用；写操作只在成员明确要求时做，每次写入都返回 action_id，'
                             '做错了用 undo 撤销。不能删除数据、不能对外发消息或承诺价格交期。'),
        })
    if method == 'ping':
        return _response(request_id, {})
    if method == 'tools/list':
        return _response(request_id, {'tools': tool_definitions()})
    if method == 'tools/call':
        if not isinstance(params, dict) or not isinstance(params.get('name'), str):
            return _response(request_id, error={'code': -32602, 'message': 'tools/call 需要 name'})
        if params['name'] not in TOOLS:
            return _response(request_id, error={'code': -32602, 'message': f'未知工具 {params["name"]}'})
        return _response(request_id, call_tool(params['name'], params.get('arguments'), gateway))
    return _response(request_id, error={'code': -32601, 'message': f'Method not found: {method}'})


def serve(stdin=None, stdout=None, gateway=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply = _response(None, error={'code': -32700, 'message': 'Parse error'})
        else:
            if isinstance(message, list):  # JSON-RPC batch (older clients)
                replies = [r for r in (handle_message(m, gateway) for m in message) if r is not None]
                reply = replies or None
            else:
                reply = handle_message(message, gateway)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + '\n')
            stdout.flush()


def main():
    # stdout is the protocol channel: anything else printed there would corrupt it.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        serve(sys.stdin, real_stdout)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
