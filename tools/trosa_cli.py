#!/usr/bin/env python3
"""Thin command line client for the Trosa Agent Gateway (``/api/gateway/*``).

It exists so that any agent session can read CRM facts and act on a member's
explicit instruction without hand-writing HTTP.  It only uses the standard
library and never prints the token.  With ``crm:write`` it can create, update,
reschedule, archive/restore and batch reversible actions; every write returns an
``action_id`` that ``undo`` can reverse.  There is deliberately no delete
command: permanent deletion, outreach and commitments stay human-only.  See
``docs/AGENT_ACCESS.md``.

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
    if not 1 <= args.expires_days <= 3650:
        raise CliError('--expires-days 必须在 1-3650 之间', 2)
    root = root or base_url()
    call = human_session(args.user, root)
    created = call('POST', '/api/agent-gateway/tokens', {
        'label': args.label, 'scopes': scopes, 'expires_in_days': args.expires_days})['data']
    path = write_token_file(created['token'], args.file)
    return {'success': True, 'data': {'id': created['id'], 'scopes': created['scopes'], 'saved_to': path,
                                      'expires_at': created.get('expires_at', ''),
                                      'note': '令牌只保存在该文件，不会显示。用完请 revoke-token ' + created['id']}}


def revoke_token(args, root=None):
    root = root or base_url()
    call = human_session(args.user, root)
    if args.id == 'list':
        return call('GET', '/api/agent-gateway/tokens')
    return call('DELETE', f'/api/agent-gateway/tokens/{args.id}')


def _deterministic_key(body):
    """Same request => same Idempotency-Key, so a retry replays instead of duplicating."""
    return 'cli-' + hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:40]


class Gateway:
    def __init__(self, token, root=None):
        self.token, self.root = token, root or base_url()

    def get(self, path, **params):
        return request_json('GET', '/api/gateway' + path, params=params, token=self.token, root=self.root)

    def agent_get(self, path, **params):
        """Read an Agent read surface (``/api/agent/*``) with the same token."""
        return request_json('GET', '/api/agent' + path, params=params, token=self.token, root=self.root)

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
        return request_json('POST', '/api/gateway/proposals', body=body,
                            headers={'Idempotency-Key': key or _deterministic_key(body)},
                            token=self.token, root=self.root)

    def act(self, action, customer_id, payload, key=None):
        """Execute one reversible write; the key makes retries idempotent."""
        body = {'action': action, 'customer_id': customer_id, 'payload': payload}
        return request_json('POST', '/api/gateway/actions', body=body,
                            headers={'Idempotency-Key': key or _deterministic_key(body)},
                            token=self.token, root=self.root)

    def batch(self, actions, key=None):
        """Apply several reversible writes as one all-or-nothing request."""
        body = {'action': 'batch', 'payload': {'actions': actions}}
        return request_json('POST', '/api/gateway/actions', body=body,
                            headers={'Idempotency-Key': key or _deterministic_key(body)},
                            token=self.token, root=self.root)

    def undo(self, action_id):
        return request_json('POST', f'/api/gateway/actions/{action_id}/undo',
                            token=self.token, root=self.root)

    def whoami(self):
        return self.get('/whoami')


def _with_undo_hint(payload):
    """Add the exact undo command (or the duplicate candidate) to a write result."""
    if not isinstance(payload, dict):
        return payload
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    candidate = data.get('duplicate_candidate')
    action = data.get('action') if isinstance(data.get('action'), dict) else {}
    enriched = dict(payload)
    if candidate:
        enriched['duplicate_candidate'] = candidate
        enriched['undo_hint'] = ('未写入：已存在客户 {cid}（duplicate_candidate，未自动合并）'
                                 .format(cid=candidate.get('customer_id')))
    elif action.get('id'):
        enriched['undo_hint'] = f'撤销：python3 tools/trosa_cli.py undo {action["id"]}'
    return enriched


def _merge_json_payload(base, extra_json):
    """Merge an optional ``--json`` object on top of explicit flags."""
    payload = dict(base)
    if not extra_json:
        return payload
    try:
        extra = json.loads(extra_json)
    except ValueError:
        raise CliError('--json 必须是合法 JSON 对象', 2)
    if not isinstance(extra, dict):
        raise CliError('--json 必须是 JSON 对象', 2)
    payload.update(extra)
    return payload


def _read_batch_actions(source):
    """Read the batch action list from a file, ``-`` or stdin."""
    if source and source != '-':
        try:
            with open(source, encoding='utf-8') as handle:
                raw = handle.read()
        except OSError as error:
            raise CliError(f'无法读取批次文件 {source}：{error}', 2)
    else:
        raw = sys.stdin.read()
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise CliError('批次内容必须是合法 JSON', 2)
    if isinstance(parsed, dict):
        parsed = parsed.get('actions')
    if not isinstance(parsed, list) or not parsed:
        raise CliError('批次内容必须是非空的动作数组', 2)
    return parsed


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
    p.add_argument('--scopes', default='crm:read,crm:write')
    p.add_argument('--expires-days', type=int, default=90, help='有效期天数（默认 90）')
    p.add_argument('--file', default='', help='令牌保存位置（默认 ~/.config/trosa/agent.token）')
    p = sub.add_parser('revoke-token', help="【成员本人运行】撤销令牌；id 填 'list' 可列出现有令牌")
    p.add_argument('--user', required=True)
    p.add_argument('id')
    sub.add_parser('whoami', help='检查 token 是否有效（读一条 Today）')
    sub.add_parser('today', help='今天及逾期的待办')
    p = sub.add_parser(
        'customers',
        help='搜索客户（last_contact/next_follow_up 是派生日期摘要，不能单独当结论）',
        description=('搜索客户。last_contact 是该客户最新一条真实沟通记录的日期，'
                     'next_follow_up 是最早一条未完成人工待办的日期；它们由事实投影得出，'
                     '不是客户记录上的缓存值，但日期本身回答不了业务问题。正确口径：'
                     '是否联系过读沟通时间线 activity 并看 type/direction；'
                     '有没有下一步读未完成待办 tasks（或 snapshot 的 open_tasks）。'))
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
    sub.add_parser('brief', help='今天的工作简报（需 crm:read）')
    p = sub.add_parser('workspace', help='单个客户工作区：承诺 / 最近事实 / 信息缺口（需 crm:read）')
    p.add_argument('id', type=int)
    p = sub.add_parser('timeline', help='单个客户完整沟通时间线（需 crm:read）')
    p.add_argument('id', type=int)
    p.add_argument('--limit', type=int, default=50)
    p = sub.add_parser('search', help='跨客户消息搜索（需 crm:read）')
    p.add_argument('--query', default='')
    p.add_argument('--country', default='')
    p.add_argument('--direction', choices=DIRECTIONS, default='')
    p.add_argument('--from', dest='from_date', default='', help='起始日期 YYYY-MM-DD')
    p.add_argument('--to', dest='to_date', default='', help='结束日期 YYYY-MM-DD')
    p.add_argument('--limit', type=int, default=50)
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
    # Direct, reversible writes: no delete command exists on purpose.
    p = sub.add_parser('create-customer', help='新建客户（可逆：撤销=归档；需 crm:write）')
    p.add_argument('--name', default='')
    p.add_argument('--company', default='')
    p.add_argument('--country', default='')
    p.add_argument('--website', default='')
    p.add_argument('--source', default='', help='客户来源（固定选项之一）')
    p.add_argument('--notes', default='')
    p.add_argument('--email', default='')
    p.add_argument('--phone', default='')
    p.add_argument('--next-follow-up', dest='next_follow_up', default='')
    p.add_argument('--task-title', dest='task_title', default='')
    p.add_argument('--json', default='', help='附加字段（JSON 对象）')
    p = sub.add_parser('update-customer', help='修改客户资料（可逆；需 crm:write）')
    p.add_argument('customer', type=int)
    p.add_argument('--name', default='')
    p.add_argument('--company', default='')
    p.add_argument('--country', default='')
    p.add_argument('--website', default='')
    p.add_argument('--field', default='')
    p.add_argument('--industry', default='')
    p.add_argument('--profile', default='')
    p.add_argument('--notes', default='')
    p.add_argument('--tags', default='')
    p.add_argument('--json', default='')
    p = sub.add_parser('create-contact', help='新增联系人（可逆；需 crm:write）')
    p.add_argument('customer', type=int)
    p.add_argument('--name', default='')
    p.add_argument('--title', default='')
    p.add_argument('--email', default='')
    p.add_argument('--phone', default='')
    p.add_argument('--whatsapp', default='')
    p.add_argument('--linkedin', default='')
    p.add_argument('--json', default='')
    p = sub.add_parser('update-contact', help='修改联系人（可逆；需 crm:write）')
    p.add_argument('contact', type=int)
    p.add_argument('--name', default='')
    p.add_argument('--title', default='')
    p.add_argument('--email', default='')
    p.add_argument('--phone', default='')
    p.add_argument('--whatsapp', default='')
    p.add_argument('--linkedin', default='')
    p.add_argument('--notes', default='')
    p.add_argument('--json', default='')
    p = sub.add_parser('record-communication', help='记录已发生的沟通事实（可逆；需 crm:write）')
    p.add_argument('customer', type=int)
    p.add_argument('--content', required=True, help='实际发生的事实，不要写推测')
    p.add_argument('--direction', choices=DIRECTIONS, default='unknown')
    p.add_argument('--date', default='', help='沟通发生日期 YYYY-MM-DD')
    p.add_argument('--type', dest='activity_type', default='')
    p.add_argument('--result', default='')
    p.add_argument('--contact', type=int, default=0)
    p.add_argument('--next-task', dest='next_task', default='')
    p.add_argument('--next-follow-up', dest='next_follow_up', default='')
    p.add_argument('--json', default='')
    p = sub.add_parser('create-task', help='新建待办（可逆；需 crm:write）')
    p.add_argument('customer', type=int)
    p.add_argument('--title', required=True, help='明确动作')
    p.add_argument('--due', required=True, help='日期 YYYY-MM-DD')
    p.add_argument('--reason', default='')
    p.add_argument('--json', default='')
    p = sub.add_parser('update-task', help='修改待办（可逆；需 crm:write）')
    p.add_argument('task', type=int)
    p.add_argument('--title', default='')
    p.add_argument('--content', default='')
    p.add_argument('--reason', default='')
    p.add_argument('--due', default='', help='新日期 YYYY-MM-DD（等同 reschedule）')
    p.add_argument('--json', default='')
    p = sub.add_parser('complete-task', help='完成待办并记录结果（可逆；需 crm:write）')
    p.add_argument('task', type=int)
    p.add_argument('--result', default='')
    p.add_argument('--direction', choices=DIRECTIONS, default='unknown')
    p.add_argument('--next-task', dest='next_task', default='')
    p.add_argument('--next-follow-up', dest='next_follow_up', default='')
    p.add_argument('--json', default='')
    p = sub.add_parser('reschedule', help='调整待办日期（update-task 改日期的等价命令）')
    p.add_argument('task', type=int)
    p.add_argument('--date', required=True, help='新日期 YYYY-MM-DD')
    p.add_argument('--json', default='')
    p = sub.add_parser('archive-customer', help='归档客户到回收站（可逆；需 crm:write）')
    p.add_argument('customer', type=int)
    p = sub.add_parser('restore-customer', help='从回收站恢复客户（可逆；需 crm:write）')
    p.add_argument('customer', type=int)
    p = sub.add_parser('batch', help='一次应用多个可逆动作（最多 50；整体可撤销）')
    p.add_argument('source', nargs='?', default='', help='动作数组 JSON 文件；留空或 - 读 stdin')
    p = sub.add_parser('undo', help='撤销某个 action_id')
    p.add_argument('action_id')
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
        payload = gateway.whoami()
        data = payload.get('data') if isinstance(payload, dict) else {}
        result = {'success': True, 'data': {**(data or {}), 'authenticated': True, 'base_url': gateway.root}}
        hint = (data or {}).get('renew_hint')
        if hint:
            result['renew_hint'] = hint
        return result
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
    if args.command == 'brief':
        return gateway.agent_get('/brief/today')
    if args.command == 'workspace':
        return gateway.agent_get(f'/customers/{args.id}/workspace')
    if args.command == 'timeline':
        return gateway.agent_get(f'/customers/{args.id}/timeline', limit=args.limit)
    if args.command == 'search':
        return gateway.agent_get('/messages/search', query=args.query, country=args.country,
                                 direction=args.direction, from_date=args.from_date,
                                 to_date=args.to_date, limit=args.limit)
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
    if args.command == 'create-customer':
        base = {key: getattr(args, key) for key in ('name', 'company', 'country', 'website', 'source', 'notes')
                if getattr(args, key)}
        if args.email or args.phone:
            contact = {}
            if args.email:
                contact['email'] = args.email
            if args.phone:
                contact['phone'] = args.phone
            base['contacts'] = [contact]
        if args.next_follow_up:
            base['next_follow_up'] = args.next_follow_up
        if args.task_title:
            base['task_title'] = args.task_title
        return _with_undo_hint(gateway.act('create_customer', None, _merge_json_payload(base, args.json)))
    if args.command == 'update-customer':
        base = {key: getattr(args, key) for key in
                ('name', 'company', 'country', 'website', 'field', 'industry', 'profile', 'notes', 'tags')
                if getattr(args, key)}
        payload = _merge_json_payload(base, args.json)
        if not payload:
            raise CliError('请至少提供一个要修改的字段', 2)
        return _with_undo_hint(gateway.act('update_customer', args.customer, payload))
    if args.command == 'create-contact':
        base = {key: getattr(args, key) for key in ('name', 'title', 'email', 'phone', 'whatsapp', 'linkedin')
                if getattr(args, key)}
        return _with_undo_hint(gateway.act('create_contact', args.customer, _merge_json_payload(base, args.json)))
    if args.command == 'update-contact':
        base = {key: getattr(args, key) for key in
                ('name', 'title', 'email', 'phone', 'whatsapp', 'linkedin', 'notes') if getattr(args, key)}
        payload = _merge_json_payload(base, args.json)
        if not payload:
            raise CliError('请至少提供一个要修改的字段', 2)
        payload['contact_id'] = args.contact
        return _with_undo_hint(gateway.act('update_contact', None, payload))
    if args.command == 'record-communication':
        payload = {'content': args.content, 'direction': args.direction}
        if args.date:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', args.date):
                raise CliError('--date 必须是 YYYY-MM-DD', 2)
            payload['follow_date'] = args.date
        if args.activity_type:
            payload['activity_type'] = args.activity_type
        if args.result:
            payload['result'] = args.result
        if args.contact:
            payload['contact_id'] = args.contact
        if args.next_task:
            payload['next_task'] = args.next_task
        if args.next_follow_up:
            payload['next_follow_up'] = args.next_follow_up
        payload = _merge_json_payload(payload, args.json)
        return _with_undo_hint(gateway.act('record_communication', args.customer, payload))
    if args.command == 'create-task':
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', args.due):
            raise CliError('--due 必须是 YYYY-MM-DD', 2)
        payload = {'title': args.title, 'due_date': args.due}
        if args.reason:
            payload['reason'] = args.reason
        return _with_undo_hint(gateway.act('create_task', args.customer, _merge_json_payload(payload, args.json)))
    if args.command == 'update-task':
        payload = {key: getattr(args, key) for key in ('title', 'content', 'reason') if getattr(args, key)}
        if args.due:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', args.due):
                raise CliError('--due 必须是 YYYY-MM-DD', 2)
            payload['remind_date'] = args.due
        payload = _merge_json_payload(payload, args.json)
        if not payload:
            raise CliError('请至少提供一个要修改的字段', 2)
        payload['task_id'] = args.task
        return _with_undo_hint(gateway.act('update_task', None, payload))
    if args.command == 'complete-task':
        payload = {'task_id': args.task, 'direction': args.direction}
        if args.result:
            payload['result'] = args.result
        if args.next_task:
            payload['next_task'] = args.next_task
        if args.next_follow_up:
            payload['next_follow_up'] = args.next_follow_up
        return _with_undo_hint(gateway.act('complete_task', None, _merge_json_payload(payload, args.json)))
    if args.command == 'reschedule':
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', args.date):
            raise CliError('--date 必须是 YYYY-MM-DD', 2)
        payload = _merge_json_payload({'task_id': args.task, 'remind_date': args.date}, args.json)
        return _with_undo_hint(gateway.act('update_task', None, payload))
    if args.command == 'archive-customer':
        return _with_undo_hint(gateway.act('archive_customer', args.customer, {}))
    if args.command == 'restore-customer':
        return _with_undo_hint(gateway.act('restore_customer', args.customer, {}))
    if args.command == 'batch':
        return _with_undo_hint(gateway.batch(_read_batch_actions(args.source)))
    if args.command == 'undo':
        return gateway.undo(args.action_id)
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
