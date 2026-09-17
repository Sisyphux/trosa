"""Audit (and deterministically repair) the Customer boundary of history rows.

Every communication, outreach email, and task must be attributable to exactly
one Customer per user.  The canonical store keeps facts per *account*, and
``trosa.account_legacy_refs`` may hold several legacy customer aliases for one
account after historical customer merges.  The original customer binding
survives in the row's own payload (``customer_id``), which the importer wrote
verbatim from the legacy tables and the runtime write boundary now sets
explicitly.

This tool answers, for the whole database:

* which history rows are correctly bound, missing a binding, or bound to an id
  that is not one of the row owner's aliases for that account;
* which rows are genuinely ambiguous (missing binding on an account the user
  holds several aliases for) and therefore hidden from customer history;
* whether the business views (``trosa.customer_interactions``,
  ``trosa.customer_tasks``, ``trosa.today_tasks``) agree with that binding,
  with no fan-out and no cross-customer leakage.

With ``--repair`` it applies only unambiguous, reversible fixes:

* a row without a binding whose owner has exactly one alias for the account
  receives that alias id;
* a timeline event without a binding but with a ``related_task_id`` adopts the
  bound customer of that task when task and event share the same owner and
  account.

Everything else is reported, never guessed.  Every change is journaled as
before/after JSON so it can be reviewed and reverted.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# (canonical table, legacy ref table name, payload column, business kind)
KIND_TABLES = (
    ('timeline_events', 'follow_up_logs', 'payload', 'communication'),
    ('outreach_messages', 'outreach_emails', 'legacy_payload', 'email'),
    ('tasks', 'reminders', 'legacy_payload', 'task'),
)

# Service accounts that never own customer history.
SYSTEM_USERS = {'system'}


def _dsn(explicit: str) -> str:
    value = (explicit or os.environ.get('TRADE_OS_DATABASE_URL', '')).strip()
    if not value:
        raise SystemExit(
            'TRADE_OS_DATABASE_URL 未设置：请在正式环境 env 下运行，或使用 --database-url。'
        )
    parsed = urlparse(value)
    if parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        print('警告：非 loopback 数据库地址，请确认这是目标环境。', file=sys.stderr)
    return value


def _connect(dsn: str):
    import psycopg

    connection = psycopg.connect(dsn, row_factory=psycopg.rows.dict_row)
    connection.execute(
        "SELECT set_config('search_path', 'trosa,core,identity,audit,sela,public', false)"
    )
    return connection


def _users(connection) -> list[str]:
    rows = connection.execute(
        '''SELECT DISTINCT r.legacy_user_id
             FROM trosa.account_legacy_refs r
            WHERE coalesce(r.legacy_user_id, '') <> ''
            ORDER BY 1'''
    ).fetchall()
    return [row['legacy_user_id'] for row in rows if row['legacy_user_id'] not in SYSTEM_USERS]


def _alias_index(connection) -> dict[tuple[str, str], list[int]]:
    """(legacy_user_id, account_id) -> sorted alias customer ids of that user."""
    rows = connection.execute(
        '''SELECT legacy_user_id, account_id,
                  array_agg(DISTINCT legacy_customer_id ORDER BY legacy_customer_id) AS ids
             FROM trosa.account_legacy_refs
            GROUP BY legacy_user_id, account_id'''
    ).fetchall()
    return {
        (row['legacy_user_id'], str(row['account_id'])): [int(v) for v in (row['ids'] or [])]
        for row in rows
    }


def _collect_rows(connection) -> list[dict]:
    """Every canonical history row with its owning user and payload binding."""
    results: list[dict] = []
    for table, ref_table, payload_column, kind in KIND_TABLES:
        payload_expr = f"trosa.compat_legacy_bigint(c.{payload_column}->>'customer_id')"
        related_expr = (
            f"trosa.compat_legacy_bigint(c.{payload_column}->>'related_task_id')"
            if table == 'timeline_events' else 'NULL::bigint'
        )
        deleted_expr = (
            f"coalesce(c.{payload_column}->>'is_deleted', '0') in ('1', 'true')"
            if table == 'timeline_events' else 'false'
        )
        rows = connection.execute(
            f'''SELECT r.legacy_user_id AS owner, r.legacy_id AS legacy_id,
                       c.id AS row_id, c.account_id AS account_id,
                       {payload_expr} AS bound,
                       {related_expr} AS related_task_id,
                       {deleted_expr} AS deleted,
                       c.{payload_column} AS payload
                  FROM trosa.legacy_row_refs r
                  JOIN trosa.{table} c ON c.id=r.target_id
                 WHERE r.table_name=%s''',
            (ref_table,),
        ).fetchall()
        for row in rows:
            row['table'] = table
            row['payload_column'] = payload_column
            row['kind'] = kind
            results.append(row)
    return results


def _classify(rows: list[dict], aliases: dict) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {
        'ok': [], 'missing_single': [], 'ambiguous': [], 'mismatch': [],
    }
    for row in rows:
        owners_aliases = aliases.get((row['owner'], str(row['account_id'])), [])
        row['aliases'] = owners_aliases
        bound = row['bound']
        if bound is None:
            if len(owners_aliases) == 1:
                row['target'] = owners_aliases[0]
                buckets['missing_single'].append(row)
            else:
                buckets['ambiguous'].append(row)
        elif bound in owners_aliases:
            row['target'] = bound
            buckets['ok'].append(row)
        else:
            buckets['mismatch'].append(row)
    return buckets


def _apply_task_chain_fix(ambiguous_rows: list[dict], rows: list[dict]) -> list[dict]:
    """Recover an event's binding from its related task (same owner + account)."""
    bound_tasks = {
        (row['owner'], str(row['account_id']), int(row['legacy_id'])): row['bound']
        for row in rows
        if row['table'] == 'tasks' and row['bound'] is not None
    }
    recovered: list[dict] = []
    for row in ambiguous_rows:
        related = row.get('related_task_id')
        if related is None:
            continue
        bound = bound_tasks.get((row['owner'], str(row['account_id']), int(related)))
        if bound is None or bound not in row['aliases']:
            continue
        row['target'] = bound
        row['recovered_from'] = f'tasks:{int(related)}'
        recovered.append(row)
    return recovered


def repair(connection, rows_to_fix: list[dict], journal_path: Path) -> int:
    """Write the deterministic bindings and journal every before/after pair."""
    changed = 0
    journal: list[dict] = []
    for row in rows_to_fix:
        before = row['payload']
        if before is not None and before.get('customer_id') is not None:
            continue
        connection.execute(
            f'''UPDATE trosa.{row['table']}
                   SET {row['payload_column']}=coalesce({row['payload_column']}, '{{}}'::jsonb)
                        || jsonb_build_object('customer_id', %s::bigint)
                 WHERE id=%s::uuid''',
            (int(row['target']), str(row['row_id'])),
        )
        journal.append({
            'table': row['table'],
            'row_id': str(row['row_id']),
            'legacy_id': int(row['legacy_id']),
            'owner': row['owner'],
            'account_id': str(row['account_id']),
            'before_payload': before,
            'customer_id': int(row['target']),
            'recovered_from': row.get('recovered_from', 'single-alias'),
        })
        changed += 1
    if changed:
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_text(
            json.dumps(journal, ensure_ascii=False, indent=2), encoding='utf-8'
        )
    return changed


def _view_rows(connection, user: str) -> dict:
    connection.execute("SELECT set_config('trade_os.user', %s, false)", (user,))
    interactions = connection.execute(
        'SELECT kind, id, customer_id FROM trosa.customer_interactions'
    ).fetchall()
    tasks = connection.execute('SELECT id, customer_id FROM trosa.customer_tasks').fetchall()
    today = connection.execute('SELECT id, customer_id FROM trosa.today_tasks').fetchall()
    return {
        'customer_interactions': [
            {'kind': row['kind'], 'id': int(row['id']), 'customer_id': int(row['customer_id'])}
            for row in interactions
        ],
        'customer_tasks': [
            {'id': int(row['id']), 'customer_id': int(row['customer_id'])} for row in tasks
        ],
        'today_tasks': [
            {'id': int(row['id']), 'customer_id': int(row['customer_id'])} for row in today
        ],
    }


def verify_views(connection, users: list[str]) -> list[dict]:
    """Every view row must equal the canonical binding, exactly once."""
    aliases = _alias_index(connection)
    rows = _collect_rows(connection)
    buckets = _classify(rows, aliases)
    binding: dict[str, dict[str, dict[int, int]]] = {}
    for row in buckets['ok']:
        if row.get('deleted'):
            continue
        per_user = binding.setdefault(row['owner'], {'communication': {}, 'email': {}, 'task': {}})
        per_user[row['kind']][int(row['legacy_id'])] = int(row['target'])

    issues: list[dict] = []
    for user in users:
        view = _view_rows(connection, user)

        expected_interactions = {
            **{('communication', k): v
               for k, v in binding.get(user, {}).get('communication', {}).items()},
            **{('email', k): v
               for k, v in binding.get(user, {}).get('email', {}).items()},
        }
        actual: dict[tuple[str, int], list[int]] = {}
        for item in view['customer_interactions']:
            actual.setdefault((item['kind'], item['id']), []).append(item['customer_id'])
        for key, values in actual.items():
            expected_customer = expected_interactions.get(key)
            if expected_customer is None:
                issues.append({'user': user, 'view': 'customer_interactions', 'kind': key[0],
                               'id': key[1], 'issue': 'unexpected_view_row', 'values': values})
            elif values != [expected_customer]:
                issues.append({'user': user, 'view': 'customer_interactions', 'kind': key[0],
                               'id': key[1], 'issue': 'wrong_or_duplicated_customer',
                               'values': values, 'expected': expected_customer})
        for key, expected_customer in expected_interactions.items():
            if key not in actual:
                issues.append({'user': user, 'view': 'customer_interactions', 'kind': key[0],
                               'id': key[1], 'issue': 'missing_from_view',
                               'expected': expected_customer})

        expected_tasks = binding.get(user, {}).get('task', {})
        actual_tasks: dict[int, list[int]] = {}
        for item in view['customer_tasks']:
            actual_tasks.setdefault(item['id'], []).append(item['customer_id'])
        for legacy_id, values in actual_tasks.items():
            expected_customer = expected_tasks.get(legacy_id)
            if expected_customer is None:
                issues.append({'user': user, 'view': 'customer_tasks', 'id': legacy_id,
                               'issue': 'unexpected_view_row', 'values': values})
            elif values != [expected_customer]:
                issues.append({'user': user, 'view': 'customer_tasks', 'id': legacy_id,
                               'issue': 'wrong_or_duplicated_customer',
                               'values': values, 'expected': expected_customer})
        for legacy_id, expected_customer in expected_tasks.items():
            if legacy_id not in actual_tasks:
                issues.append({'user': user, 'view': 'customer_tasks', 'id': legacy_id,
                               'issue': 'missing_from_view', 'expected': expected_customer})

        actual_today: dict[int, list[int]] = {}
        for item in view['today_tasks']:
            actual_today.setdefault(item['id'], []).append(item['customer_id'])
        for legacy_id, values in actual_today.items():
            if legacy_id not in expected_tasks:
                issues.append({'user': user, 'view': 'today_tasks', 'id': legacy_id,
                               'issue': 'unexpected_view_row', 'values': values})
            elif values != [expected_tasks[legacy_id]]:
                issues.append({'user': user, 'view': 'today_tasks', 'id': legacy_id,
                               'issue': 'wrong_or_duplicated_customer',
                               'values': values, 'expected': expected_tasks[legacy_id]})
    return issues


def audit(connection, *, repair_mode: bool, journal_path: Path, json_output: bool) -> int:
    aliases = _alias_index(connection)
    rows = _collect_rows(connection)
    buckets = _classify(rows, aliases)

    recovered = _apply_task_chain_fix(buckets['ambiguous'], rows)
    still_ambiguous = [row for row in buckets['ambiguous'] if row not in recovered]

    repaired = 0
    if repair_mode:
        try:
            repaired = repair(connection, buckets['missing_single'] + recovered, journal_path)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    planned = len(buckets['missing_single']) + len(recovered)

    multi_alias_accounts = {
        (owner, account): ids for (owner, account), ids in aliases.items() if len(ids) > 1
    }
    pre_fix = {
        'multi_alias_accounts': len(multi_alias_accounts),
        'customers_with_merged_account': sum(len(ids) for ids in multi_alias_accounts.values()),
        'bound_rows_on_multi_alias_accounts': sum(
            1 for row in buckets['ok'] if len(row['aliases']) > 1
        ),
        'wrong_exposure_count': (
            sum(max(0, len(row['aliases']) - 1)
                for row in buckets['ok'] if len(row['aliases']) > 1)
            + sum(len(row['aliases']) for row in buckets['ambiguous'] + buckets['missing_single']
                  if len(row['aliases']) > 1)
        ),
    }

    ambiguous_list = [
        {
            'owner': row['owner'], 'table': row['table'], 'legacy_id': int(row['legacy_id']),
            'row_id': str(row['row_id']), 'account_id': str(row['account_id']),
            'aliases': row['aliases'],
            'related_task_id': int(row['related_task_id']) if row.get('related_task_id') else None,
        }
        for row in still_ambiguous
    ]
    mismatch_list = [
        {
            'owner': row['owner'], 'table': row['table'], 'legacy_id': int(row['legacy_id']),
            'row_id': str(row['row_id']), 'bound': int(row['bound']), 'aliases': row['aliases'],
        }
        for row in buckets['mismatch']
    ]

    users = _users(connection)
    view_issues = verify_views(connection, users)

    summary = {
        'generated_at': _dt.datetime.now(_dt.timezone.utc).isoformat(),
        'mode': 'repair' if repair_mode else 'audit',
        'users': users,
        'rows_total': len(rows),
        'rows_bound_ok': len(buckets['ok']),
        'rows_missing_binding_single_alias': len(buckets['missing_single']),
        'rows_missing_binding_multi_alias_ambiguous': len(still_ambiguous),
        'rows_binding_mismatch': len(buckets['mismatch']),
        'repaired': repaired,
        'repaired_via_related_task': len(recovered) if repair_mode else 0,
        'planned_repair': planned,
        'pre_fix_impact': pre_fix,
        'ambiguous_rows': ambiguous_list,
        'mismatch_rows': mismatch_list,
        'view_verification': {
            'issue_count': len(view_issues),
            'issues': view_issues[:200],
        },
    }
    if json_output:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_summary(summary)
    clean = (not view_issues) and (not still_ambiguous) and (not mismatch_list)
    return 0 if clean else 1


def _print_summary(summary: dict) -> None:
    print(f"行总数: {summary['rows_total']}  用户: {', '.join(summary['users'])}")
    print(f"绑定正确: {summary['rows_bound_ok']}")
    print(f"缺绑定但可唯一确定(单别名): {summary['rows_missing_binding_single_alias']}")
    print(f"歧义(多别名且无绑定，已从客户视图排除): {summary['rows_missing_binding_multi_alias_ambiguous']}")
    print(f"绑定与别名不匹配(隐藏并列出): {summary['rows_binding_mismatch']}")
    if summary['mode'] == 'repair':
        print(f"本次已修复: {summary['repaired']} 条")
    else:
        print(f"计划可修复(确定性): {summary['planned_repair']} 条")
    pre = summary['pre_fix_impact']
    print(f"共享账号(同人多别名): {pre['multi_alias_accounts']} 个，涉及客户 {pre['customers_with_merged_account']} 个")
    print(f"多别名账号上已正确绑定但曾会被错误暴露的历史行: {pre['bound_rows_on_multi_alias_accounts']}")
    print(f"修复前错误暴露总次数(行×错误别名): {pre['wrong_exposure_count']}")
    if summary['ambiguous_rows']:
        print('无法确定归属的行（保持隐藏，需要人工判断）:')
        for item in summary['ambiguous_rows']:
            print(f"  - user={item['owner']} {item['table']} legacy_id={item['legacy_id']} "
                  f"aliases={item['aliases']} related_task_id={item['related_task_id']}")
    if summary['mismatch_rows']:
        print('绑定与别名不匹配的行:')
        for item in summary['mismatch_rows']:
            print(f"  - user={item['owner']} {item['table']} legacy_id={item['legacy_id']} "
                  f"bound={item['bound']} aliases={item['aliases']}")
    vv = summary['view_verification']
    print(f"视图一致性校验: {vv['issue_count']} 个问题")
    for issue in vv['issues'][:20]:
        print(f"  - {issue}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url', default='')
    parser.add_argument('--repair', action='store_true',
                        help='应用确定性修复（默认只做只读审计）')
    parser.add_argument('--journal-path', default='',
                        help='修复变更日志输出路径（默认 trosa_binding_repair_<时间>.json）')
    parser.add_argument('--json', action='store_true', help='以 JSON 输出完整报告')
    args = parser.parse_args()

    dsn = _dsn(args.database_url)
    connection = _connect(dsn)
    try:
        journal_name = (
            args.journal_path
            or f"trosa_binding_repair_{_dt.datetime.now().strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        return audit(connection, repair_mode=args.repair,
                     journal_path=Path(journal_name), json_output=args.json)
    finally:
        connection.close()


if __name__ == '__main__':
    raise SystemExit(main())
