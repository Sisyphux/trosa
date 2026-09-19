"""Inbox 重新判定：自动处理系统能自行决定的部分。

在写入时和进程启动时运行。它只做三件事，全部是安全的、幂等的：

1. 把已退役的类型（不再代表人工问题的历史队列）标记为自动关闭；
2. 把退信/系统通知类 Gmail 采集标记为自动关闭（投递事实，不需要归属）；
3. 当同一封邮件的后续事实已经匹配到客户时，自动关闭旧的待归属问题。

它不删除任何行：原文、来源和时间保留在 ``inbox_items``，只是不再占用 Inbox。
自动关闭一律写入 ``resolution_source='auto'`` 和原因，保持可审计。

这个模块不导入 app.py，避免循环依赖；serve / app 启动和写入路径都可以调用它。
"""
import json
import logging
from datetime import datetime

import inbox_questions as iq
from db import get_db, postgres_mode, set_db_user, USERS, get_current_user
from trosa_domain import resolve_inbox_item, set_inbox_status

logger = logging.getLogger(__name__)


def _now_text():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _capture_identity(content):
    """Best-effort stable identity for one capture without guessing."""
    try:
        payload = json.loads(content or '{}')
    except (TypeError, ValueError):
        return ''
    if not isinstance(payload, dict):
        return ''
    for key in ('conversation_identity', 'sender_email', 'email', 'phone'):
        value = str(payload.get(key) or '').strip()
        if value:
            return value.casefold()
    messages = payload.get('messages') if isinstance(payload.get('messages'), list) else []
    for message in messages:
        if not isinstance(message, dict):
            continue
        for key in ('sender_email', 'sender', 'from'):
            value = str(message.get(key) or '').strip()
            if value:
                return value.casefold()
    return ''


def _capture_message_id(content):
    try:
        payload = json.loads(content or '{}')
    except (TypeError, ValueError):
        return ''
    if not isinstance(payload, dict):
        return ''
    messages = payload.get('messages') if isinstance(payload.get('messages'), list) else []
    if messages and isinstance(messages[0], dict):
        return str(messages[0].get('message_id') or messages[0].get('id') or '').strip()
    return str(payload.get('message_id') or payload.get('id') or '').strip()


def _noise_capture(content):
    try:
        import gmail_sync
        payload = json.loads(content or '{}')
    except (TypeError, ValueError, ImportError):
        return False
    if not isinstance(payload, dict):
        return False
    messages = payload.get('messages') if isinstance(payload.get('messages'), list) else []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = gmail_sync.gmail_noise_role(message)
        if role['delivery_notice'] or role['noise']:
            return True
    return False


def _open_rows(conn):
    if postgres_mode():
        rows = conn.execute(
            '''SELECT ref.legacy_id AS id, item.item_type, item.title, item.content,
                      COALESCE(item.legacy_payload->>'compat_dedupe_key', item.dedupe_key) AS dedupe_key,
                      item.status, item.created_at::text AS created_at,
                      COALESCE(item.question_kind, '') AS question_kind,
                      COALESCE(item.question_key, '') AS question_key,
                      COALESCE(item.source_type, '') AS source_type,
                      ar.legacy_customer_id AS customer_id
                 FROM trosa.inbox_items item
                 JOIN trosa.legacy_row_refs ref ON ref.target_id=item.id
                 LEFT JOIN trosa.account_legacy_refs ar
                   ON ar.account_id=item.account_id
                  AND ar.organization_id=ref.organization_id
                  AND ar.legacy_user_id=ref.legacy_user_id
                WHERE ref.organization_id=trosa.compat_org_id()
                  AND ref.legacy_user_id=trosa.compat_current_user()
                  AND ref.table_name='inbox_items'
                  AND item.status='open'
                ORDER BY item.created_at ASC, ref.legacy_id ASC''',
        ).fetchall()
        return [dict(row) for row in rows]
    rows = conn.execute(
        '''SELECT id, item_type, title, content, dedupe_key, status, created_at,
                  COALESCE(question_kind, '') AS question_kind,
                  COALESCE(question_key, '') AS question_key,
                  COALESCE(source_type, '') AS source_type,
                  customer_id
             FROM inbox_items WHERE status='open'
            ORDER BY created_at ASC, id ASC''',
    ).fetchall()
    return [dict(row) for row in rows]


def _message_matched(conn, message_id):
    if not message_id:
        return False
    if postgres_mode():
        row = conn.execute(
            '''SELECT match_status FROM trosa.email_message_receipts
                WHERE organization_id=trosa.compat_org_id() AND provider_message_id=?''',
            (message_id,),
        ).fetchone()
    else:
        row = conn.execute(
            'SELECT match_status FROM gmail_message_states WHERE provider_message_id=?',
            (message_id,),
        ).fetchone()
    return bool(row and str(row['match_status']) == 'matched')


def _update_question_metadata(conn, row, question_kind, question_key, source_type):
    if postgres_mode():
        conn.execute(
            '''UPDATE trosa.inbox_items SET question_kind=?, question_key=?, source_type=?
                 FROM trosa.legacy_row_refs ref
                WHERE ref.organization_id=trosa.compat_org_id()
                  AND ref.legacy_user_id=trosa.compat_current_user()
                  AND ref.table_name='inbox_items' AND ref.legacy_id=?
                  AND trosa.inbox_items.id=ref.target_id''',
            (question_kind, question_key, source_type, row['id']),
        )
    else:
        conn.execute(
            'UPDATE inbox_items SET question_kind=?, question_key=?, source_type=? WHERE id=?',
            (question_kind, question_key, source_type, row['id']),
        )


def reconcile_inbox_connection(conn):
    """重新判定当前用户所有 open Inbox 条目，返回各结果的计数。"""
    stats = {'scanned': 0, 'retired': 0, 'noise': 0, 'later_fact': 0, 'metadata': 0}
    now = _now_text()
    for row in _open_rows(conn):
        stats['scanned'] += 1
        item_type = str(row.get('item_type') or '')
        question_kind = iq.question_kind_for(item_type)
        question_key = str(row.get('question_key') or '').strip()
        source_type = str(row.get('source_type') or '').strip() or iq.source_type_for(item_type)

        # 自动关闭：已退役类型不再代表人工问题。
        if item_type in iq.RETIRED_ITEM_TYPES:
            resolve_inbox_item(
                conn, inbox_item_id=row['id'], resolved_at=now,
                resolution_reason='retired_question_type',
                resolution_note='该类型已不再代表需要人工判断的问题；历史记录保留用于审计。',
                resolution_source='auto',
            )
            stats['retired'] += 1
            continue

        # 自动关闭：退信/系统通知是投递事实。
        if item_type == 'gmail_capture' and _noise_capture(row.get('content')):
            resolve_inbox_item(
                conn, inbox_item_id=row['id'], resolved_at=now,
                resolution_reason='inbound_noise',
                resolution_note='退信或系统通知是投递事实，不需要人工归属。',
                resolution_source='auto',
            )
            stats['noise'] += 1
            continue

        # 自动关闭：同一封邮件后来已被匹配进客户时间线。
        if item_type == 'gmail_capture' and _message_matched(conn, _capture_message_id(row.get('content'))):
            resolve_inbox_item(
                conn, inbox_item_id=row['id'], resolved_at=now,
                resolution_reason='later_fact',
                resolution_note='同一封邮件后来已匹配并记录到客户时间线，旧问题自动失效。',
                resolution_source='auto',
            )
            stats['later_fact'] += 1
            continue

        # 补齐问题元数据并收敛同一发件人的证据到同一问题键。
        if item_type in ('gmail_capture', 'browser_capture'):
            question_key = iq.question_key_for(item_type, row.get('dedupe_key') or '',
                                               _capture_identity(row.get('content'))) or question_key
        if (str(row.get('question_kind') or '') != question_kind
                or str(row.get('question_key') or '') != question_key
                or str(row.get('source_type') or '') != source_type):
            _update_question_metadata(conn, row, question_kind, question_key, source_type)
            stats['metadata'] += 1

    if any(stats.values()):
        conn.commit()
    return stats


def reconcile_current_user():
    """在当前数据库用户上下文内执行一次重新判定。"""
    conn = get_db()
    try:
        stats = reconcile_inbox_connection(conn)
        if any(stats.values()):
            logger.info('Inbox 重新判定: %s', stats)
        return stats
    finally:
        conn.close()


def reconcile_all_users():
    """启动时为每个用户重新判定一次；任何用户失败都不阻塞其它用户。"""
    results = {}
    old_user = get_current_user()
    for user in list(USERS):
        try:
            set_db_user(user)
            results[user] = reconcile_current_user()
        except Exception:
            logger.warning('Inbox 重新判定失败：%s', user, exc_info=True)
        finally:
            try:
                set_db_user(old_user)
            except Exception:
                pass
    return results
