#!/usr/bin/env python3
"""Thin command line client for the Trosa Agent Gateway (``/api/gateway/*``).

It exists so that any agent session can read CRM facts and submit *proposals*
without hand-writing HTTP.  It only uses the standard library and never prints
the token.  Writes are limited to proposals (``crm:propose``): the user confirms
them inside Trosa.  See ``docs/AGENT_ACCESS.md``.

Credentials (first match wins):
  1. ``TROSA_AGENT_TOKEN`` environment variable
  2. ``~/.config/trosa/agent.token`` (one line, mode 600) -- deliberately a
     different file from the release credentials in ``workbench.env``.
Base URL: ``TROSA_BASE_URL`` (default ``https://app.trosa.space``).

Issuing a token is a human step (``issue-token`` / ``revoke-token``): it needs the
member's own access code, so it refuses to run without an interactive terminal and
an agent must never be given the code.

Exit codes: 0 ok, 1 server/validation error, 2 usage or missing token,
3 network error, 4 auth/permission error.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import http.cookiejar
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = 'https://app.trosa.space'
TOKEN_FILE = os.path.join('~', '.config', 'trosa', 'agent.token')
TOKEN_PATTERN = re.compile(r'trosa_pat_[A-Za-z0-9]{8,32}_[A-Za-z0-9_-]{32,256}')
PAGE_SIZE = 50  # server hard cap
DIRECTIONS = ('outbound', 'inbound', 'two_way', 'unknown')
SCOPES = ('crm:read', 'crm:propose', 'crm:write')


class CliError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def load_token(environ=None, token_file=None):
    environ = os.environ if environ is None else environ
    token = str(environ.get('TROSA_AGENT_TOKEN') or '').strip()
    if not token:
        path = os.path.expanduser(token_file or TOKEN_FILE)
        try:
            mode = os.stat(path).st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise CliError(f'{path} 权限过宽，请执行 chmod 600 {path}', 2)
            with open(path, encoding='utf-8') as handle:
                token = handle.readline().strip()
        except FileNotFoundError:
            token = ''
    if not token:
        raise CliError('没有找到 Agent token：设置 TROSA_AGENT_TOKEN，或把令牌写入 '
                       f'{TOKEN_FILE}（权限 600）。领取方式见 docs/AGENT_ACCESS.md。', 2)
    if not TOKEN_PATTERN.fullmatch(token):
        raise CliError('Agent token 格式不正确（应以 trosa_pat_ 开头）。', 2)
    return token


def base_url(environ=None):
    environ = os.environ if environ is None else environ
    url = str(environ.get('TROSA_BASE_URL') or DEFAULT_BASE_URL).strip().rstrip('/')
    if not re.match(r'^https?://', url):
        raise CliError('TROSA_BASE_URL 必须以 http:// 或 https:// 开头', 2)
    return url


def request_json(method, path, params=None, body=None, headers=None, token=None, root=None, timeout=30):
    root = root or base_url()
    url = root + path
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, '')})
        if query:
            url += '?' + query
    data = None
    final_headers = {'Accept': 'application/json', 'User-Agent': 'trosa-cli/1'}
    if token:
        final_headers['Authorization'] = 'Bearer ' + token
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        final_headers['Content-Type'] = 'application/json'
    final_headers.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=final_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        raw = error.read()
        message, code = _error_message(error.code, raw)
        raise CliError(message, code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise CliError(f'无法连接 {root}：{getattr(error, "reason", error)}', 3) from None
    try:
        return json.loads(raw.decode('utf-8'))
    except ValueError:
        raise CliError('服务返回的不是 JSON（可能是网络层拦截页）', 3) from None


def _error_message(status, raw):
    detail = ''
    try:
        payload = json.loads(raw.decode('utf-8'))
        error = payload.get('error')
        detail = error.get('message') if isinstance(error, dict) else str(error or '')
    except (ValueError, AttributeError):
        pass
    if status == 401:
        return 'token 无效、已撤销或缺失（401）。请到 Trosa 重新生成。' + (f' {detail}' if detail else ''), 4
    if status == 403:
        return f'当前 token 没有这项权限（403）：{detail}', 4
    return f'服务返回 {status}：{detail or "无详情"}', 1


def human_session(user, root):
    """Log in as a member with their own access code (interactive terminal only)."""
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise CliError('该命令需要成员本人在交互式终端中输入访问码；Agent 会话不应执行，也不应获得访问码。', 2)
    secret = getpass.getpass(f'{user} 的访问码（输入不显示）：')
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(method, path, body=None):
        data = json.dumps(body).encode('utf-8') if body is not None else None
        req = urllib.request.Request(root + path, data=data, method=method, headers={
            'Accept': 'application/json', 'Content-Type': 'application/json', 'User-Agent': 'trosa-cli/1'})
        try:
            with opener.open(req, timeout=30) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as error:
            raise CliError(_error_message(error.code, error.read())[0], 4 if error.code in (400, 401, 403) else 1) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise CliError(f'无法连接 {root}：{getattr(error, "reason", error)}', 3) from None

    call('POST', '/api/auth/login', {'user': user, 'pin': secret, 'password': secret})
    return call


def write_token_file(token, path=None):
    path = os.path.expanduser(path or TOKEN_FILE)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        handle.write(token + '\n')
    os.chmod(path, 0o600)
    return path


def issue_token(args, root=None):
    scopes = sorted({item.strip() for item in args.scopes.split(',') if item.strip()})
    if not scopes or not set(scopes).issubset(SCOPES):
        raise CliError(f'--scopes 只能是 {", ".join(SCOPES)} 的逗号组合', 2)
    root = root or base_url()
    call = human_session(args.user, root)
    created = call('POST', '/api/agent-gateway/tokens', {'label': args.label, 'scopes': scopes})['data']
    path = write_token_file(created['token'], args.file)
    return {'success': True, 'data': {'id': created['id'], 'scopes': created['scopes'], 'saved_to': path,
                                      'note': '令牌只保存在该文件，不会显示。用完请 revoke-token ' + created['id']}}


def revoke_token(args, root=None):
    root = root or base_url()
    call = human_session(args.user, root)
    if args.id == 'list':
        return call('GET', '/api/agent-gateway/tokens')
    return call('DELETE', f'/api/agent-gateway/tokens/{args.id}')


class Gateway:
    def __init__(self, token, root=None):
        self.token, self.root = token, root or base_url()

    def get(self, path, **params):
        return request_json('GET', '/api/gateway' + path, params=params, token=self.token, root=self.root)

    def page_all(self, path, key, max_items=5000, **params):
        """Follow ``offset`` until the server returns a short page."""
        items, offset = [], 0
        while len(items) < max_items:
            payload = self.get(path, limit=PAGE_SIZE, offset=offset, **params)
            batch = (payload.get('data') or {}).get(key) or []
            items.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return items

    def propose(self, action, customer_id, payload, key=None):
        body = {'action': action, 'customer_id': customer_id, 'payload': payload}
        # Same request => same key, so a retry replays instead of duplicating.
        key = key or 'cli-' + hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:40]
        return request_json('POST', '/api/gateway/proposals', body=body,
                            headers={'Idempotency-Key': key}, token=self.token, root=self.root)


def snapshot(gateway):
    """Every active customer plus every open task, for whole-book reviews."""
    customers = gateway.page_all('/customers', 'customers')
    tasks = gateway.page_all('/tasks', 'tasks')
    by_customer = {}
    for task in tasks:
        by_customer.setdefault(task['customer_id'], []).append(task)
    for customer in customers:
        customer['open_tasks'] = by_customer.get(customer['id'], [])
    return {'customers': customers, 'open_task_count': len(tasks),
            'customers_without_open_task': [c['id'] for c in customers if not c['open_tasks']]}


def build_parser():
    parser = argparse.ArgumentParser(prog='trosa_cli', description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('ping', help='检查正式服务是否可用（不需要 token）')
    p = sub.add_parser('issue-token', help='【成员本人运行】登录并领取令牌，写入 ~/.config/trosa/agent.token')
    p.add_argument('--user', required=True, help='你的 Trosa 账号')
    p.add_argument('--label', default='agent')
    p.add_argument('--scopes', default='crm:read,crm:propose')
    p.add_argument('--file', default='', help='令牌保存位置（默认 ~/.config/trosa/agent.token）')
    p = sub.add_parser('revoke-token', help="【成员本人运行】撤销令牌；id 填 'list' 可列出现有令牌")
    p.add_argument('--user', required=True)
    p.add_argument('id')
    sub.add_parser('whoami', help='检查 token 是否有效（读一条 Today）')
    sub.add_parser('today', help='今天及逾期的待办')
    p = sub.add_parser('customers', help='搜索客户（含最近联系与下一次跟进日期）')
    p.add_argument('--query', default='')
    p.add_argument('--limit', type=int, default=25)
    p.add_argument('--offset', type=int, default=0)
    p.add_argument('--all', action='store_true', help='自动翻页取全部')
    p = sub.add_parser('customer', help='单个客户详情')
    p.add_argument('id', type=int)
    p = sub.add_parser('contacts', help='客户联系人')
    p.add_argument('id', type=int)
    p = sub.add_parser('tasks', help='未完成待办')
    p.add_argument('--customer', type=int)
    p.add_argument('--limit', type=int, default=25)
    p.add_argument('--offset', type=int, default=0)
    p.add_argument('--all', action='store_true')
    p = sub.add_parser('activity', help='沟通记录（可按客户、关键词）')
    p.add_argument('--customer', type=int)
    p.add_argument('--query', default='')
    p.add_argument('--limit', type=int, default=25)
    p.add_argument('--offset', type=int, default=0)
    p.add_argument('--all', action='store_true')
    sub.add_parser('inbox', help='待整理的 Inbox 条目')
    p = sub.add_parser('actions', help='本 token 所属用户最近的 Agent 动作')
    p.add_argument('--limit', type=int, default=25)
    sub.add_parser('snapshot', help='全部客户 + 全部未完成待办（做整体跟进审查用）')
    p = sub.add_parser('propose-task', help='提议新待办（需 crm:propose；你在 Trosa 里确认）')
    p.add_argument('customer', type=int)
    p.add_argument('--title', required=True, help='明确动作')
    p.add_argument('--due', required=True, help='日期 YYYY-MM-DD')
    p.add_argument('--reason', default='')
    p.add_argument('--source-reference', default='')
    p = sub.add_parser('propose-communication', help='提议记录一条已发生的沟通事实')
    p.add_argument('customer', type=int)
    p.add_argument('--content', required=True, help='实际发生的事实，不要写推测')
    p.add_argument('--direction', choices=DIRECTIONS, default='unknown')
    p.add_argument('--date', default='', help='沟通发生日期 YYYY-MM-DD')
    p.add_argument('--type', dest='activity_type', default='')
    p.add_argument('--source-reference', default='')
    return parser


def run(args, gateway=None, root=None):
    if args.command == 'ping':
        return request_json('GET', '/api/network/ping', root=root or base_url())
    if args.command == 'issue-token':
        return issue_token(args, root)
    if args.command == 'revoke-token':
        return revoke_token(args, root)
    gateway = gateway or Gateway(load_token(), root)
    listing = lambda path, key, **extra: (  # noqa: E731
        {'data': {key: gateway.page_all(path, key, **extra)}} if args.all
        else gateway.get(path, limit=args.limit, offset=args.offset, **extra))
    if args.command == 'whoami':
        gateway.get('/today', limit=1)
        return {'success': True, 'data': {'authenticated': True, 'base_url': gateway.root}}
    if args.command == 'today':
        return gateway.get('/today', limit=PAGE_SIZE)
    if args.command == 'customers':
        return listing('/customers', 'customers', query=args.query)
    if args.command == 'customer':
        return gateway.get(f'/customers/{args.id}')
    if args.command == 'contacts':
        return gateway.get(f'/customers/{args.id}/contacts')
    if args.command == 'tasks':
        return listing('/tasks', 'tasks', customer_id=args.customer)
    if args.command == 'activity':
        return listing('/activity', 'activities', customer_id=args.customer, query=args.query)
    if args.command == 'inbox':
        return gateway.get('/inbox', limit=PAGE_SIZE)
    if args.command == 'actions':
        return gateway.get('/actions/recent', limit=args.limit)
    if args.command == 'snapshot':
        return {'success': True, 'data': snapshot(gateway)}
    if args.command == 'propose-task':
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', args.due):
            raise CliError('--due 必须是 YYYY-MM-DD', 2)
        payload = {'title': args.title, 'due_date': args.due}
        if args.reason:
            payload['reason'] = args.reason
        if args.source_reference:
            payload['source_reference'] = args.source_reference
        return gateway.propose('create_task', args.customer, payload)
    if args.command == 'propose-communication':
        payload = {'content': args.content, 'direction': args.direction, 'source': 'agent_cli'}
        if args.date:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', args.date):
                raise CliError('--date 必须是 YYYY-MM-DD', 2)
            payload['follow_date'] = args.date
        if args.activity_type:
            payload['activity_type'] = args.activity_type
        if args.source_reference:
            payload['source_reference'] = args.source_reference
        return gateway.propose('record_communication', args.customer, payload)
    raise CliError(f'未知命令 {args.command}', 2)


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except CliError as error:
        print(f'错误：{error}', file=sys.stderr)
        return error.code
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
