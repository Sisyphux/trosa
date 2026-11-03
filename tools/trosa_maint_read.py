#!/usr/bin/env python3
"""Read-only, all-member data channel for the maintenance / development agent.

Why this exists: the Agent Gateway token only sees one member's data, and the
release config (``workbench.env``) also carries publish power, so it stays
closed to development sessions.  This tool uses a *separate* credential for a
PostgreSQL role that can only SELECT (see ``deploy/postgres-production/
maint-readonly-role.sh``).  It can read every member's data; it cannot write,
publish, or touch release config.

Credentials live outside the repo in ``~/.config/trosa-maint/readonly.json``
(mode 600), never in ``workbench.env``::

    {"dsn": "postgresql://trosa_maint_ro:...@127.0.0.1:15432/tradeos",
     "ssh_host": "trosa-new",          # optional: open the tunnel for you
     "remote_port": 5432, "local_port": 15432}

The production host is the Lighthouse machine ``trosa-new``.  The retired
``trosa-ecs`` host keeps an old rollback copy; pointing this channel there
silently returns days-old data, so ``whoami``/``check`` report a data watermark
and refuse stale snapshots.  ``check`` exits 3 when the channel is stale or the
configured SSH host is retired.

Usage::

    python3 tools/trosa_maint_read.py whoami
    python3 tools/trosa_maint_read.py check
    python3 tools/trosa_maint_read.py users
    python3 tools/trosa_maint_read.py customers --query bold.net.co
    python3 tools/trosa_maint_read.py sql "SELECT ..." [--as-user kelley]

``--as-user`` sets ``trade_os.user`` so the ``trosa.*`` projection views show
that member's own rows exactly as the application would.  Every call is
appended to ``~/.config/trosa-maint/audit.log``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH = Path(os.environ.get('TROSA_MAINT_CONFIG') or Path.home() / '.config' / 'trosa-maint' / 'readonly.json')
AUDIT_PATH = CONFIG_PATH.parent / 'audit.log'
MAX_ROWS = 500
STATEMENT_TIMEOUT_MS = 20_000
DEFAULT_MAX_LAG_HOURS = 48.0
RETIRED_SSH_HOSTS = {'trosa-ecs'}

_FORBIDDEN = re.compile(
    r'\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|do|'
    r'vacuum|reindex|cluster|refresh|lock|listen|notify|set|reset|comment|'
    r'pg_terminate_backend|pg_cancel_backend|pg_read_file|pg_ls_dir|lo_import|lo_export|dblink)\b',
    re.IGNORECASE,
)
_ALLOWED_START = re.compile(r'^\s*(select|with|explain|show|values|table)\b', re.IGNORECASE)


class MaintError(RuntimeError):
    pass


def _strip_sql_noise(sql: str) -> str:
    """Remove comments and string literals so keyword checks see only syntax."""
    sql = re.sub(r'--[^\n]*', ' ', sql)
    sql = re.sub(r'/\*.*?\*/', ' ', sql, flags=re.DOTALL)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    sql = re.sub(r'"(?:[^"]|"")*"', '""', sql)
    return sql


def check_read_only_sql(sql: str) -> str:
    """Defence in depth only: the DB role and read-only transaction are the real fence."""
    text = str(sql or '').strip().rstrip(';').strip()
    if not text:
        raise MaintError('SQL 为空')
    bare = _strip_sql_noise(text)
    if ';' in bare:
        raise MaintError('只允许单条语句')
    if not _ALLOWED_START.match(bare):
        raise MaintError('只允许 SELECT / WITH / EXPLAIN / SHOW 查询')
    hit = _FORBIDDEN.search(bare)
    if hit:
        raise MaintError(f'只读通道不允许关键字：{hit.group(1).upper()}')
    return text


def _load_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise MaintError(
            f'没有维护通道凭据：{CONFIG_PATH}\n'
            '请按 docs/MAINTAINER_DATA_ACCESS.md 创建只读角色并写入该文件（权限 600）。'
        )
    mode = CONFIG_PATH.stat().st_mode & 0o077
    if mode:
        raise MaintError(f'{CONFIG_PATH} 权限过宽，请 chmod 600')
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise MaintError(f'无法读取维护通道配置：{exc}') from exc
    if not str(config.get('dsn') or '').strip():
        raise MaintError('配置缺少 dsn')
    return config


def _audit(action: str, detail: str, user: str = '') -> None:
    try:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'action': action, 'as_user': user, 'detail': detail[:2000],
        }, ensure_ascii=False)
        with AUDIT_PATH.open('a', encoding='utf-8') as handle:
            handle.write(line + '\n')
        os.chmod(AUDIT_PATH, 0o600)
    except OSError:
        pass


def _listener_pids(local_port: int):
    """PIDs listening on ``local_port``; None when lsof is unavailable."""
    try:
        result = subprocess.run(
            ['lsof', '-nP', '-t', f'-iTCP:{local_port}', '-sTCP:LISTEN'],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return [int(token) for token in result.stdout.split() if token.isdigit()]


def _process_command(pid: int) -> str:
    try:
        result = subprocess.run(['ps', '-o', 'command=', '-p', str(pid)],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ''
    return result.stdout.strip()


def _ssh_tunnel_target(command: str) -> str:
    """Destination host of a plain ``ssh ... -L ... host`` command, else ''."""
    parts = str(command or '').split()
    if not parts or parts[0].rsplit('/', 1)[-1] != 'ssh':
        return ''
    for part in reversed(parts[1:]):
        if part.startswith('-'):
            return ''
        return part.split('@')[-1]
    return ''


def check_existing_tunnel(host: str, local_port: int):
    """Classify a listener already bound to ``local_port``.

    Returns ``(status, detail)`` where status is ``missing`` (nothing listening),
    ``ok`` (an ssh tunnel to ``host``), ``mismatch`` (an ssh tunnel to another
    host) or ``unknown`` (cannot inspect).  A stale tunnel to the retired ECS
    host must not be reused just because the port happens to be open.
    """
    pids = _listener_pids(local_port)
    if pids is None:
        return 'unknown', '找不到 lsof，无法确认现有隧道目标'
    if not pids:
        return 'missing', ''
    unknown = []
    for pid in pids:
        command = _process_command(pid)
        target = _ssh_tunnel_target(command)
        if not target:
            if command:
                unknown.append(command)
            continue
        if target == host:
            return 'ok', f'pid {pid}'
        return 'mismatch', f'pid {pid} -> {target}'
    return 'unknown', unknown[0] if unknown else ''


@contextlib.contextmanager
def _tunnel(config: dict):
    host = str(config.get('ssh_host') or '').strip()
    if not host:
        yield
        return
    local_port = int(config.get('local_port') or 15432)
    remote_port = int(config.get('remote_port') or 5432)
    with contextlib.suppress(OSError):
        with socket.create_connection(('127.0.0.1', local_port), timeout=0.5):
            status, detail = check_existing_tunnel(host, local_port)
            if status == 'mismatch':
                raise MaintError(
                    f'本地端口 {local_port} 已被另一条 SSH 隧道占用（{detail}），'
                    f'与配置的 {host} 不一致；请先结束该隧道，或修改 readonly.json 的 local_port。'
                )
            if status == 'unknown':
                print(f'trosa_maint_read: 警告：本地端口 {local_port} 已有监听，'
                      f'无法确认隧道目标（{detail or "未知进程"}）；若数据过期请先结束该隧道。',
                      file=sys.stderr)
            yield  # tunnel already up
            return
    proc = subprocess.Popen(
        ['ssh', '-N', '-o', 'ExitOnForwardFailure=yes', '-o', 'BatchMode=yes',
         '-L', f'127.0.0.1:{local_port}:127.0.0.1:{remote_port}', host],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            if proc.poll() is not None:
                err = (proc.stderr.read() or b'').decode('utf-8', 'replace').strip()
                raise MaintError(f'SSH 隧道建立失败：{err or proc.returncode}')
            with contextlib.suppress(OSError):
                with socket.create_connection(('127.0.0.1', local_port), timeout=0.3):
                    break
            time.sleep(0.2)
        else:
            raise MaintError('SSH 隧道 10 秒内未就绪')
        yield
    finally:
        proc.terminate()


def run_query(sql: str, params=(), as_user: str = '', limit: int = MAX_ROWS):
    """Run one guarded SELECT in a read-only transaction; return (columns, rows)."""
    import psycopg  # imported lazily so the SQL guard is testable without a driver

    statement = check_read_only_sql(sql)
    config = _load_config()
    with _tunnel(config):
        with psycopg.connect(str(config['dsn']), connect_timeout=10) as conn:
            conn.read_only = True
            conn.execute("SELECT set_config('search_path', 'trosa,core,identity,audit,sela,public', false)")
            conn.execute("SELECT set_config('TimeZone', 'Asia/Shanghai', false)")
            conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            if as_user:
                conn.execute("SELECT set_config('trade_os.user', %s, false)", (as_user,))
            with conn.cursor() as cur:
                cur.execute(statement, params)
                columns = [col.name for col in cur.description or []]
                rows = cur.fetchmany(limit + 1)
    truncated = len(rows) > limit
    return columns, [list(row) for row in rows[:limit]], truncated


def _channel_snapshot() -> dict:
    """Role, database and data watermark for the maintenance channel."""
    columns, rows, _ = run_query(
        '''SELECT current_user AS db_role,
                  current_database() AS db_name,
                  current_setting('transaction_read_only') AS read_only,
                  now() AS server_time,
                  wm.watermark AS data_watermark,
                  ROUND(EXTRACT(EPOCH FROM (now() - wm.watermark)) / 3600.0, 1) AS data_lag_hours
             FROM (SELECT GREATEST(
                       (SELECT max(created_at::timestamptz) FROM trosa.customer_interactions),
                       (SELECT max(created_at::timestamptz) FROM trosa.customer_contacts),
                       (SELECT max(updated_at::timestamptz) FROM trosa.tasks),
                       (SELECT max(updated_at::timestamptz) FROM trosa.accounts)
                   ) AS watermark) wm''')
    return dict(zip(columns, rows[0])) if rows else {}


def assess_channel(config: dict, row: dict, max_lag_hours: float = DEFAULT_MAX_LAG_HOURS) -> list:
    """Problems that mean this channel is not reading the live production data."""
    issues = []
    host = str(config.get('ssh_host') or '').strip()
    if host in RETIRED_SSH_HOSTS:
        issues.append(f'ssh_host={host} 已退役，只剩旧快照；请改为正式主机 trosa-new')
    db_name = row.get('db_name')
    if db_name and db_name != 'tradeos':
        issues.append(f'当前数据库是 {db_name}，不是正式库 tradeos')
    lag = row.get('data_lag_hours')
    if lag is not None and float(lag) > float(max_lag_hours):
        issues.append(
            f'数据最后更新于 {row.get("data_watermark")}，滞后 {float(lag):.1f} 小时'
            f'（阈值 {float(max_lag_hours):g} 小时），可能连到了旧快照'
        )
    return issues


def _emit(columns, rows, truncated, extra=None) -> None:
    payload = {
        'columns': columns, 'row_count': len(rows), 'truncated': truncated,
        'rows': [dict(zip(columns, row)) for row in rows],
    }
    if extra:
        payload.update(extra)
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write('\n')


def _warn(issues) -> None:
    for issue in issues:
        print(f'trosa_maint_read: 警告：{issue}', file=sys.stderr)


def _cmd_whoami(args) -> int:
    config = _load_config()
    row = _channel_snapshot()
    issues = assess_channel(config, row, args.max_lag_hours)
    _audit('whoami', '; '.join(issues))
    _warn(issues)
    _emit(list(row), [list(row.values())], False, extra={'issues': issues, 'ok': not issues})
    return 0


def _cmd_check(args) -> int:
    config = _load_config()
    row = _channel_snapshot()
    issues = assess_channel(config, row, args.max_lag_hours)
    _audit('check', '; '.join(issues))
    _warn(issues)
    _emit(list(row), [list(row.values())], False, extra={'issues': issues, 'ok': not issues})
    if issues:
        print('trosa_maint_read: 维护通道不可用：' + '；'.join(issues), file=sys.stderr)
        return 3
    return 0


def _cmd_users(args) -> int:
    columns, rows, truncated = run_query(
        'SELECT legacy_user_id AS username, display_name, status FROM identity.users ORDER BY legacy_user_id')
    _audit('users', '')
    _emit(columns, rows, truncated)
    return 0


def _cmd_customers(args) -> int:
    """Search one or every member's customers by name / company / website / contact email."""
    like = f'%{args.query.strip().lower()}%'
    members = [args.as_user] if args.as_user else None
    if members is None:
        _, user_rows, _ = run_query("SELECT legacy_user_id FROM identity.users WHERE status='active' AND legacy_user_id IS NOT NULL ORDER BY legacy_user_id")
        members = [row[0] for row in user_rows]
    out_columns, out_rows = None, []
    for member in members:
        columns, rows, _ = run_query(
            '''SELECT c.id, c.name, c.company, c.country, c.website, c.status,
                      c.updated_at,
                      (SELECT count(*) FROM trosa.customer_interactions i
                        WHERE i.customer_id=c.id) AS interactions,
                      (SELECT string_agg(DISTINCT i.kind || ':' || COALESCE(i.delivery_status,''), ',')
                         FROM trosa.customer_interactions i WHERE i.customer_id=c.id) AS kinds
                 FROM trosa.customer_records c
                WHERE c.deleted_at IS NULL
                  AND (lower(c.name) LIKE %s OR lower(c.company) LIKE %s OR lower(c.website) LIKE %s
                       OR EXISTS (SELECT 1 FROM trosa.customer_contacts k
                                   WHERE k.customer_id=c.id AND lower(k.email) LIKE %s))
                ORDER BY c.id''',
            (like, like, like, like), as_user=member,
        )
        out_columns = ['member'] + columns
        out_rows.extend([member] + row for row in rows)
    _audit('customers', args.query, args.as_user)
    _emit(out_columns or ['member'], out_rows, False)
    return 0


def _cmd_sql(args) -> int:
    columns, rows, truncated = run_query(args.sql, as_user=args.as_user, limit=args.limit)
    _audit('sql', args.sql, args.as_user)
    _emit(columns, rows, truncated)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('whoami', help='确认只读角色、正式库与数据新鲜度')
    p.add_argument('--max-lag-hours', type=float, default=DEFAULT_MAX_LAG_HOURS,
                   help='数据滞后超过该小时数即警告（默认 48）')
    p.set_defaults(func=_cmd_whoami)
    p = sub.add_parser('check', help='严格版 whoami：通道过期、指向退役主机或非正式库时返回 3')
    p.add_argument('--max-lag-hours', type=float, default=DEFAULT_MAX_LAG_HOURS,
                   help='数据滞后超过该小时数即失败（默认 48）')
    p.set_defaults(func=_cmd_check)
    sub.add_parser('users', help='列出成员').set_defaults(func=_cmd_users)
    p = sub.add_parser('customers', help='按名称/公司/网站/联系人邮箱跨成员搜客户')
    p.add_argument('--query', required=True)
    p.add_argument('--as-user', default='', help='只看某位成员；缺省遍历全部启用成员')
    p.set_defaults(func=_cmd_customers)
    p = sub.add_parser('sql', help='执行一条只读 SELECT')
    p.add_argument('sql')
    p.add_argument('--as-user', default='', help='把 trade_os.user 设为该成员，视图按其视角投影')
    p.add_argument('--limit', type=int, default=MAX_ROWS)
    p.set_defaults(func=_cmd_sql)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except MaintError as exc:
        print(f'trosa_maint_read: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
