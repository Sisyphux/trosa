"""The small, explicit business read model used by current Trosa surfaces.

The PostgreSQL store has canonical tables and a compatibility projection for
historical identifiers.  This module is the boundary for the *business*
meaning of those records: callers receive interactions and tasks, never a
requirement to understand which transport table supplied them.  SQLite remains
available for isolated development and recovery, so the queries deliberately
use the same current relation names on both stores.

The module also owns the canonical Interaction and Task writes shared by
Gmail, Agent confirmation, and the human workflow.  Flask remains responsible
only for authentication, audit, undo, and HTTP contract adaptation.  SQLite
branches are isolated development/recovery adapters, never production
business implementations.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from db import postgres_mode


def _ids(customer_ids: Iterable[int]) -> list[int]:
    return list(dict.fromkeys(int(value) for value in customer_ids if value is not None))


def customer_tasks(conn: Any, customer_id: int, *, include_done: bool = False) -> list[dict]:
    """Return human tasks in the one ordering used by Today and Customer.

    Retired outreach scheduler rows are delivery history, not tasks.  They
    remain accessible through history/recovery but cannot become a next step.
    """
    if postgres_mode():
        done_clause = '' if include_done else "AND status='open'"
        rows = conn.execute(
            f'''SELECT id, customer_id, title, content, reason, due_date AS remind_date,
                       task_type AS reminder_type, source_activity_legacy_id AS source_activity_id,
                       CASE WHEN status='done' THEN 1 ELSE 0 END AS is_done,
                       completed_at, created_at
                  FROM trosa.customer_tasks
                 WHERE customer_id=? {done_clause}
                 ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END,
                          due_date ASC, manual_order ASC, id ASC''',
            (customer_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    done_clause = '' if include_done else 'AND r.is_done=0'
    rows = conn.execute(
        f'''SELECT r.id, r.customer_id, r.title, r.content, r.reason,
                   r.remind_date, r.reminder_type, r.source_activity_id,
                   r.is_done, r.completed_at, r.created_at
              FROM reminders r
             WHERE r.customer_id=? {done_clause}
               AND COALESCE(r.reminder_type, 'follow_up') NOT LIKE 'outreach_%'
             ORDER BY CASE WHEN r.is_done=0 THEN 0 ELSE 1 END,
                      r.remind_date ASC, r.manual_order ASC, r.id ASC''',
        (customer_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def customer_contacts(conn: Any, customer_id: int) -> list[dict]:
    """Return Contacts without making product callers depend on compat tables."""
    relation = 'trosa.customer_contacts' if postgres_mode() else 'contacts'
    rows = conn.execute(
        f'''SELECT id, customer_id, name, title, email, phone, whatsapp, linkedin,
                   preferred_channel, contact_type, is_primary, notes, created_at
              FROM {relation} WHERE customer_id=?
             ORDER BY is_primary DESC, created_at DESC, id DESC''', (customer_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def today_tasks(conn: Any, *, due_on_or_before: str, limit: int | None = None) -> list[dict]:
    """The one Today work view.  It is a projection of open Tasks, never a second queue."""
    if postgres_mode():
        query = '''SELECT id, customer_id, title, content, reason, due_date AS remind_date,
                          task_type AS reminder_type, manual_order, customer_name, customer_company
                     FROM trosa.today_tasks WHERE due_date<=?
                    ORDER BY due_date, manual_order, id'''
    else:
        query = '''SELECT r.id, r.customer_id, r.title, r.content, r.reason,
                          r.remind_date, r.reminder_type, r.manual_order,
                          c.name AS customer_name, c.company AS customer_company
                     FROM reminders r JOIN customers c ON c.id=r.customer_id
                    WHERE r.is_done=0 AND r.remind_date<=?
                      AND COALESCE(r.reminder_type, 'follow_up') NOT LIKE 'outreach_%'
                      AND (c.is_deleted=0 OR c.is_deleted IS NULL)
                    ORDER BY r.remind_date, r.manual_order, r.id'''
    params: list[Any] = [due_on_or_before]
    if limit is not None:
        query += ' LIMIT ?'
        params.append(max(1, int(limit)))
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def customer_record(conn: Any, customer_id: int) -> dict | None:
    """Return the modern Customer identity and state used by all adapters."""
    relation = 'trosa.customer_records' if postgres_mode() else 'customers'
    row = conn.execute(f'''SELECT * FROM {relation}
                            WHERE id=? AND ({'deleted_at IS NULL' if postgres_mode() else '(is_deleted=0 OR is_deleted IS NULL)'})''',
                       (customer_id,)).fetchone()
    return dict(row) if row else None


def active_customers(conn: Any, *, include_deleted: bool = False) -> list[dict]:
    """List the modern Customer records used by reporting and search surfaces."""
    if postgres_mode():
        where = '' if include_deleted else 'WHERE deleted_at IS NULL'
        rows = conn.execute(f'SELECT * FROM trosa.customer_records {where} ORDER BY updated_at DESC, id DESC').fetchall()
    else:
        where = '' if include_deleted else 'WHERE (is_deleted=0 OR is_deleted IS NULL)'
        rows = conn.execute(f'SELECT * FROM customers {where} ORDER BY updated_at DESC, id DESC').fetchall()
    return [dict(row) for row in rows]


def weekly_interactions(conn: Any, *, from_date: str, to_date: str) -> list[dict]:
    """Return reportable Interaction facts; weekly is a view, never its own history."""
    if postgres_mode():
        rows = conn.execute(
            '''SELECT i.*, c.name AS customer_name, c.company AS customer_company, c.country AS customer_country
                 FROM trosa.customer_interactions i
                 JOIN trosa.customer_records c ON c.id=i.customer_id
                WHERE i.occurred_on>=? AND i.occurred_on<=? AND i.is_reported=true
                  AND c.deleted_at IS NULL
                ORDER BY i.occurred_on DESC, i.created_at DESC, i.id DESC''',
            (from_date, to_date),
        ).fetchall()
        return [dict(row) for row in rows]
    rows = conn.execute(
        '''SELECT i.*, c.name AS customer_name, c.company AS customer_company, c.country AS customer_country FROM (
             SELECT 'communication' AS kind, f.id, f.customer_id, f.follow_date AS occurred_on,
                    f.created_at, f.content, f.result, f.next_plan, f.activity_type,
                    f.direction, f.source, f.is_reported
               FROM follow_up_logs f WHERE f.follow_date>=? AND f.follow_date<=?
                    AND f.is_reported=1 AND (f.is_deleted=0 OR f.is_deleted IS NULL)
             UNION ALL
             SELECT 'email', o.id, o.customer_id, o.sent_date, o.created_at, o.content,
                    o.reply_content, '', 'outreach_email', 'outbound', 'gmail_delivery', o.is_reported
               FROM outreach_emails o WHERE o.sent_date>=? AND o.sent_date<=? AND o.is_reported=1
            ) i JOIN customers c ON c.id=i.customer_id
           WHERE (c.is_deleted=0 OR c.is_deleted IS NULL)
           ORDER BY occurred_on DESC, created_at DESC, id DESC''',
        (from_date, to_date, from_date, to_date),
    ).fetchall()
    return [dict(row) for row in rows]


def record_external_interaction(
    conn: Any, *, customer_id: int, content: str, occurred_on: str,
    direction: str, source: str, activity_type: str = 'email', result: str = '',
    next_plan: str = '', source_reference: str = '', contact_id: int | None = None,
    is_reported: bool = False, related_task_id: int | None = None,
) -> int:
    """Persist an adapter-observed fact as one Interaction.

    Gmail and sela call this boundary instead of inventing an independent
    follow-up-log implementation.  The SQLite branch exists only for local
    recovery fixtures; production writes the canonical event first.
    """
    if postgres_mode():
        account = conn.execute(
            '''SELECT account_id FROM trosa.account_legacy_refs
                WHERE organization_id=trosa.compat_org_id()
                  AND legacy_user_id=trosa.compat_current_user()
                  AND legacy_customer_id=?''', (customer_id,),
        ).fetchone()
        if not account:
            raise ValueError('customer is not visible to the current user')
        if source_reference:
            existing = conn.execute(
                '''SELECT ref.legacy_id FROM trosa.legacy_row_refs ref
                     JOIN trosa.timeline_events event ON event.id=ref.target_id
                    WHERE ref.organization_id=trosa.compat_org_id()
                      AND ref.legacy_user_id=trosa.compat_current_user()
                      AND ref.table_name='follow_up_logs' AND event.account_id=?
                      AND event.source_module=? AND event.source_reference=?''',
                (account['account_id'], source, source_reference),
            ).fetchone()
            if existing:
                return int(existing[0])
        legacy_id = conn.execute(
            "SELECT trosa.compat_next_id('follow_up_logs', trosa.compat_current_user())",
        ).fetchone()[0]
        target_id = conn.execute(
            "SELECT trosa.compat_uuid(?)", (f'interaction:{source}:{source_reference or legacy_id}',),
        ).fetchone()[0]
        contact_method_id = None
        if contact_id:
            contact = conn.execute(
                '''SELECT contact_method_id FROM trosa.contact_legacy_refs
                    WHERE organization_id=trosa.compat_org_id()
                      AND legacy_user_id=trosa.compat_current_user() AND legacy_contact_id=?
                      AND legacy_customer_id=?''', (contact_id, customer_id),
            ).fetchone()
            contact_method_id = contact['contact_method_id'] if contact else None
        payload = {
            'is_reported': bool(is_reported),
            **({'related_task_id': int(related_task_id)} if related_task_id else {}),
        }
        conn.execute(
            '''INSERT INTO trosa.timeline_events
               (id, account_id, contact_method_id, event_type, direction, content, result, next_plan,
                source_module, source_reference, occurred_at, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, trosa.compat_time(?), ?::jsonb)
               ON CONFLICT (id) DO NOTHING''',
            (target_id, account['account_id'], contact_method_id, activity_type, direction, content, result,
             next_plan, source, source_reference, occurred_on, json.dumps(payload)),
        )
        conn.execute(
            '''INSERT INTO trosa.legacy_row_refs
               (organization_id, legacy_user_id, table_name, legacy_id, target_id)
               VALUES (trosa.compat_org_id(), trosa.compat_current_user(), 'follow_up_logs', ?, ?)
               ON CONFLICT (organization_id, legacy_user_id, table_name, legacy_id) DO NOTHING''',
            (legacy_id, target_id),
        )
        return int(legacy_id)
    cursor = conn.execute(
        '''INSERT INTO follow_up_logs
           (customer_id, content, follow_date, result, next_plan, activity_type, direction, contact_id,
            related_task_id, is_reported, source, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))''',
        (customer_id, content, occurred_on, result, next_plan, activity_type, direction, contact_id,
         related_task_id, 1 if is_reported else 0, source),
    )
    return int(cursor.lastrowid)


def merge_open_task(
    conn: Any, *, customer_id: int, title: str, content: str, reason: str,
    due_on: str, task_type: str = 'follow_up', source_interaction_id: int | None = None,
    now: str = '',
) -> int:
    """Create or merge the one Customer Task for a due date.

    This is the production write boundary for the normal follow-up task.  It
    writes ``trosa.tasks`` directly; the integer row reference is created only
    so legacy HTTP clients and undo snapshots can address the same Task.
    """
    if not postgres_mode():
        if task_type != 'follow_up':
            cursor = conn.execute(
                '''INSERT INTO reminders
                   (customer_id, title, content, reason, remind_date, is_done,
                    reminder_type, source_activity_id, created_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)''',
                (customer_id, title, content, reason, due_on, task_type,
                 source_interaction_id, now),
            )
            return int(cursor.lastrowid)
        existing = conn.execute(
            '''SELECT id, title, reason FROM reminders
                 WHERE customer_id=? AND is_done=0 AND reminder_type='follow_up'
                   AND remind_date=? ORDER BY id ASC LIMIT 1''',
            (customer_id, due_on),
        ).fetchone()
        if existing:
            merged_reason = ' / '.join(part for part in (existing['reason'], reason) if part)
            conn.execute(
                '''UPDATE reminders SET title=?, content=?, reason=?, updated_at=? WHERE id=?''',
                (title or existing['title'], content or title or existing['title'], merged_reason, now, existing['id']),
            )
            return int(existing['id'])
        cursor = conn.execute(
            '''INSERT INTO reminders
               (customer_id, title, content, reason, remind_date, is_done,
                reminder_type, source_activity_id, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)''',
            (customer_id, title, content, reason, due_on, task_type, source_interaction_id, now),
        )
        return int(cursor.lastrowid)

    account = conn.execute(
        '''SELECT account_id FROM trosa.account_legacy_refs
            WHERE organization_id=trosa.compat_org_id()
              AND legacy_user_id=trosa.compat_current_user()
              AND legacy_customer_id=?''',
        (customer_id,),
    ).fetchone()
    if not account:
        raise ValueError('customer is not visible to the current user')
    account_id = account['account_id']
    if task_type == 'follow_up':
        existing = conn.execute(
            '''SELECT row_ref.legacy_id, task.id, task.title, task.reason
                 FROM trosa.tasks task
                 JOIN trosa.legacy_row_refs row_ref ON row_ref.target_id=task.id
                  AND row_ref.organization_id=trosa.compat_org_id()
                  AND row_ref.legacy_user_id=trosa.compat_current_user()
                  AND row_ref.table_name='reminders'
                WHERE task.account_id=? AND task.status='open'
                  AND task.task_type='follow_up'
                  AND trosa.compat_local_date(task.due_at)=?
                ORDER BY row_ref.legacy_id ASC LIMIT 1''',
            (account_id, due_on),
        ).fetchone()
        if existing:
            merged_reason = ' / '.join(part for part in (existing['reason'], reason) if part)
            conn.execute(
                '''UPDATE trosa.tasks SET title=?, content=?, reason=?, updated_at=now()
                    WHERE id=?''',
                (title or existing['title'], content or title or existing['title'], merged_reason, existing['id']),
            )
            return int(existing['legacy_id'])
    legacy_id = conn.execute(
        "SELECT trosa.compat_next_id('reminders', trosa.compat_current_user())",
    ).fetchone()[0]
    task_id = conn.execute(
        'SELECT trosa.compat_uuid(?)', (f'modern-task:{account_id}:{legacy_id}',),
    ).fetchone()[0]
    conn.execute(
        '''INSERT INTO trosa.tasks
           (id, account_id, title, content, reason, due_at, status, task_type,
            source_activity_legacy_id, manual_order, legacy_payload, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, trosa.compat_time(?), 'open', ?, ?, 0,
                   '{}'::jsonb, coalesce(trosa.compat_time(?), now()), now())''',
        (task_id, account_id, title, content, reason, due_on, task_type,
         str(source_interaction_id or ''), now),
    )
    conn.execute(
        '''INSERT INTO trosa.legacy_row_refs
           (organization_id, legacy_user_id, table_name, legacy_id, target_id)
           VALUES (trosa.compat_org_id(), trosa.compat_current_user(), 'reminders', ?, ?)''',
        (legacy_id, task_id),
    )
    return int(legacy_id)


def complete_task(
    conn: Any, *, task_id: int, completed_at: str, source_interaction_id: int | None = None,
) -> None:
    """Complete a Task through its canonical fact, retaining only its API id."""
    if not postgres_mode():
        conn.execute(
            '''UPDATE reminders SET is_done=1, completed_at=?, source_activity_id=coalesce(?, source_activity_id)
               WHERE id=?''',
            (completed_at, source_interaction_id, task_id),
        )
        return
    changed = conn.execute(
        '''UPDATE trosa.tasks task SET status='done', completed_at=trosa.compat_time(?),
               source_activity_legacy_id=coalesce(nullif(?::text, ''), source_activity_legacy_id), updated_at=now()
             FROM trosa.legacy_row_refs ref
            WHERE ref.organization_id=trosa.compat_org_id()
              AND ref.legacy_user_id=trosa.compat_current_user()
              AND ref.table_name='reminders' AND ref.legacy_id=?
              AND task.id=ref.target_id''',
        (completed_at, source_interaction_id, task_id),
    )
    if not changed.rowcount:
        raise ValueError('task is not visible to the current user')


def complete_open_follow_up_tasks(conn: Any, *, customer_ids: Iterable[int], completed_at: str) -> int:
    """Close the current open follow-up Tasks for Customers in one formal write."""
    ids = _ids(customer_ids)
    if not ids:
        return 0
    placeholders = ','.join('?' for _ in ids)
    if not postgres_mode():
        cursor = conn.execute(
            f'''UPDATE reminders SET is_done=1, completed_at=?
                 WHERE customer_id IN ({placeholders}) AND is_done=0 AND reminder_type='follow_up' ''',
            [completed_at, *ids],
        )
        return int(cursor.rowcount or 0)
    cursor = conn.execute(
        f'''UPDATE trosa.tasks task SET status='done', completed_at=trosa.compat_time(?), updated_at=now()
              FROM trosa.account_legacy_refs ref
             WHERE ref.organization_id=trosa.compat_org_id()
               AND ref.legacy_user_id=trosa.compat_current_user()
               AND ref.account_id=task.account_id
               AND ref.legacy_customer_id IN ({placeholders})
               AND task.status='open' AND task.task_type='follow_up' ''',
        [completed_at, *ids],
    )
    return int(cursor.rowcount or 0)


def create_contact(conn: Any, *, customer_id: int, values: dict[str, Any], created_at: str = '') -> int:
    """Create one Contact directly in core/Trosa relations.

    ``contact_legacy_refs`` holds only the stable compatibility identifier;
    person, contact method, and customer ownership are canonical facts.
    """
    if not postgres_mode():
        cursor = conn.execute(
            '''INSERT INTO contacts
               (customer_id, name, title, email, phone, whatsapp, linkedin,
                preferred_channel, contact_type, is_primary, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (customer_id, values.get('name', ''), values.get('title', ''), values.get('email', ''),
             values.get('phone', ''), values.get('whatsapp', ''), values.get('linkedin', ''),
             values.get('preferred_channel', ''), values.get('contact_type', 'person'),
             values.get('is_primary', 0), values.get('notes', ''), created_at),
        )
        return int(cursor.lastrowid)
    account = conn.execute(
        '''SELECT ref.account_id, account.company_id FROM trosa.account_legacy_refs ref
             JOIN trosa.accounts account ON account.id=ref.account_id
            WHERE ref.organization_id=trosa.compat_org_id()
              AND ref.legacy_user_id=trosa.compat_current_user() AND ref.legacy_customer_id=?''',
        (customer_id,),
    ).fetchone()
    if not account:
        raise ValueError('customer is not visible to the current user')
    contact_id = conn.execute(
        "SELECT trosa.compat_next_id('contacts', trosa.compat_current_user())",
    ).fetchone()[0]
    email = str(values.get('email') or '').strip().lower()
    person_id = conn.execute(
        'SELECT trosa.compat_uuid(?)', (f'modern-person:email:{email}' if email else f'modern-person:{account["account_id"]}:{contact_id}',),
    ).fetchone()[0]
    conn.execute(
        '''INSERT INTO core.people (id, organization_id, full_name, normalized_name)
           VALUES (?, trosa.compat_org_id(), ?, trosa.compat_normalized_name(?))
           ON CONFLICT (id) DO UPDATE SET full_name=CASE WHEN core.people.full_name IN ('', 'UNKNOWN')
             THEN excluded.full_name ELSE core.people.full_name END, updated_at=now()''',
        (person_id, str(values.get('name') or '').strip() or 'UNKNOWN', str(values.get('name') or '')),
    )
    conn.execute(
        '''INSERT INTO core.company_people (id, company_id, person_id, title, source)
           VALUES (trosa.compat_uuid(?), ?, ?, ?, 'trosa') ON CONFLICT DO NOTHING''',
        (f'modern-company-person:{account["company_id"]}:{person_id}:{values.get("title", "")}',
         account['company_id'], person_id, str(values.get('title') or '')),
    )
    method_id = None
    if email:
        method_id = conn.execute('SELECT trosa.compat_uuid(?)', (f'modern-email:{email}',)).fetchone()[0]
        conn.execute(
            '''INSERT INTO core.contact_methods
               (id, organization_id, person_id, kind, value, normalized_value)
               VALUES (?, trosa.compat_org_id(), ?, 'email', ?, ?)
               ON CONFLICT (organization_id, kind, normalized_value) DO UPDATE
                 SET person_id=coalesce(core.contact_methods.person_id, excluded.person_id), updated_at=now()''',
            (method_id, person_id, email, email),
        )
        method = conn.execute(
            '''SELECT id FROM core.contact_methods WHERE organization_id=trosa.compat_org_id()
                 AND kind='email' AND normalized_value=?''', (email,),
        ).fetchone()
        method_id = method['id']
    conn.execute(
        '''INSERT INTO trosa.contact_legacy_refs
           (organization_id, legacy_user_id, legacy_contact_id, legacy_customer_id, account_id,
            person_id, contact_method_id, name, title, phone, whatsapp, linkedin,
            preferred_channel, contact_type, is_primary, notes, legacy_payload, created_at, updated_at)
           VALUES (trosa.compat_org_id(), trosa.compat_current_user(), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, '{}'::jsonb, coalesce(trosa.compat_time(?), now()), now())''',
        (contact_id, customer_id, account['account_id'], person_id, method_id,
         str(values.get('name') or ''), str(values.get('title') or ''), str(values.get('phone') or ''),
         str(values.get('whatsapp') or ''), str(values.get('linkedin') or ''),
         str(values.get('preferred_channel') or ''), str(values.get('contact_type') or 'person'),
         bool(values.get('is_primary')), str(values.get('notes') or ''), created_at),
    )
    return int(contact_id)


def set_customer_judgment(conn: Any, *, customer_id: int, judgment: str, updated_at: str = '') -> bool:
    """Set the Customer's human judgment in its canonical state fact."""
    if not postgres_mode():
        cursor = conn.execute('UPDATE customers SET customer_judgment=?, updated_at=? WHERE id=?',
                              (judgment, updated_at, customer_id))
        return bool(cursor.rowcount)
    cursor = conn.execute(
        '''INSERT INTO trosa.customer_states
               (organization_id, legacy_user_id, legacy_customer_id, account_id, customer_judgment, updated_at)
           SELECT ref.organization_id, ref.legacy_user_id, ref.legacy_customer_id, ref.account_id, ?, now()
             FROM trosa.account_legacy_refs ref
            WHERE ref.organization_id=trosa.compat_org_id()
              AND ref.legacy_user_id=trosa.compat_current_user()
              AND ref.legacy_customer_id=?
           ON CONFLICT (organization_id, legacy_user_id, legacy_customer_id)
           DO UPDATE SET customer_judgment=excluded.customer_judgment, updated_at=now()''',
        (judgment, customer_id),
    )
    return bool(cursor.rowcount)


def resolve_inbox_item(
    conn: Any, *, inbox_item_id: int, resolved_at: str, resolution_note: str = '',
    resolution_reason: str = '',
) -> None:
    """Resolve an Inbox item through the canonical Inbox fact."""
    if not postgres_mode():
        conn.execute(
            '''UPDATE inbox_items SET status='resolved', resolved_at=?, resolution_reason=?, resolution_note=?
                 WHERE id=? AND status='open' ''',
            (resolved_at, resolution_reason, resolution_note, inbox_item_id),
        )
        return
    changed = conn.execute(
        '''UPDATE trosa.inbox_items item
              SET status='resolved', resolved_at=trosa.compat_time(?), resolution_reason=?, resolution_note=?
             FROM trosa.legacy_row_refs ref
            WHERE ref.organization_id=trosa.compat_org_id()
              AND ref.legacy_user_id=trosa.compat_current_user()
              AND ref.table_name='inbox_items' AND ref.legacy_id=?
              AND item.id=ref.target_id AND item.status='open' ''',
        (resolved_at, resolution_reason, resolution_note, inbox_item_id),
    )
    if not changed.rowcount:
        raise ValueError('inbox item is not visible or already resolved')


def set_inbox_status(conn: Any, *, inbox_item_id: int, status: str, changed_at: str) -> None:
    """Set a canonical Inbox lifecycle status while preserving its content."""
    if not postgres_mode():
        conn.execute('UPDATE inbox_items SET status=?, resolved_at=? WHERE id=?',
                     (status, changed_at, inbox_item_id))
        return
    changed = conn.execute(
        '''UPDATE trosa.inbox_items item SET status=?, resolved_at=trosa.compat_time(?)
             FROM trosa.legacy_row_refs ref
            WHERE ref.organization_id=trosa.compat_org_id() AND ref.legacy_user_id=trosa.compat_current_user()
              AND ref.table_name='inbox_items' AND ref.legacy_id=? AND item.id=ref.target_id''',
        (status, changed_at, inbox_item_id),
    )
    if not changed.rowcount:
        raise ValueError('inbox item is not visible to the current user')


def assign_inbox_customer(conn: Any, *, inbox_item_id: int, customer_id: int) -> None:
    """Associate an open Inbox fact with a Customer without reusing a view write."""
    if not postgres_mode():
        conn.execute(
            "UPDATE inbox_items SET customer_id=? WHERE id=? AND status='open'",
            (customer_id, inbox_item_id),
        )
        return
    changed = conn.execute(
        '''UPDATE trosa.inbox_items item SET account_id=account_ref.account_id
             FROM trosa.legacy_row_refs inbox_ref
             JOIN trosa.account_legacy_refs account_ref
               ON account_ref.organization_id=trosa.compat_org_id()
              AND account_ref.legacy_user_id=trosa.compat_current_user()
              AND account_ref.legacy_customer_id=?
            WHERE inbox_ref.organization_id=trosa.compat_org_id()
              AND inbox_ref.legacy_user_id=trosa.compat_current_user()
              AND inbox_ref.table_name='inbox_items' AND inbox_ref.legacy_id=?
              AND item.id=inbox_ref.target_id AND item.status='open' ''',
        (customer_id, inbox_item_id),
    )
    if not changed.rowcount:
        raise ValueError('inbox item or customer is not visible to the current user')


def create_inbox_item(
    conn: Any, *, item_type: str, title: str, content: str = '', customer_id: int | None = None,
    dedupe_key: str = '', status: str = 'open', created_at: str = '', resolved_at: str = '',
    resolution_reason: str = '', resolution_note: str = '',
) -> int:
    """Create or refresh a canonical Inbox item and return its stable API id.

    ``legacy_row_refs`` and the namespaced raw dedupe key are transport
    adapters only.  Inbox content and status are held by ``trosa.inbox_items``.
    """
    if not postgres_mode():
        existing = None
        if dedupe_key:
            existing = conn.execute('SELECT id FROM inbox_items WHERE dedupe_key=?', (dedupe_key,)).fetchone()
        if existing:
            conn.execute(
                '''UPDATE inbox_items SET customer_id=?, item_type=?, title=?, content=?, status=?,
                       resolved_at=?, resolution_reason=?, resolution_note=? WHERE id=?''',
                (customer_id, item_type, title, content, status, resolved_at, resolution_reason,
                 resolution_note, existing['id']),
            )
            return int(existing['id'])
        cursor = conn.execute(
            '''INSERT INTO inbox_items
               (item_type, customer_id, title, content, dedupe_key, status, created_at,
                resolved_at, resolution_reason, resolution_note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (item_type, customer_id, title, content, dedupe_key, status, created_at,
             resolved_at, resolution_reason, resolution_note),
        )
        return int(cursor.lastrowid)

    account_id = None
    if customer_id is not None:
        account = conn.execute(
            '''SELECT account_id FROM trosa.account_legacy_refs
                WHERE organization_id=trosa.compat_org_id()
                  AND legacy_user_id=trosa.compat_current_user()
                  AND legacy_customer_id=?''', (customer_id,),
        ).fetchone()
        if not account:
            raise ValueError('customer is not visible to the current user')
        account_id = account['account_id']
    existing = None
    if dedupe_key:
        existing = conn.execute(
            '''SELECT ref.legacy_id, item.id FROM trosa.inbox_items item
                 JOIN trosa.legacy_row_refs ref ON ref.target_id=item.id
                WHERE ref.organization_id=trosa.compat_org_id()
                  AND ref.legacy_user_id=trosa.compat_current_user()
                  AND ref.table_name='inbox_items'
                  AND (item.legacy_payload->>'compat_dedupe_key'=?
                       OR (item.legacy_payload->>'compat_dedupe_key' IS NULL AND item.dedupe_key=?))
                ORDER BY item.created_at, ref.legacy_id LIMIT 1''',
            (dedupe_key, dedupe_key),
        ).fetchone()
    if existing:
        conn.execute(
            '''UPDATE trosa.inbox_items SET account_id=?, item_type=?, title=?, content=?, status=?,
                   resolved_at=trosa.compat_time(?), resolution_reason=?, resolution_note=?
                 WHERE id=?''',
            (account_id, item_type, title, content, status, resolved_at, resolution_reason,
             resolution_note, existing['id']),
        )
        return int(existing['legacy_id'])
    legacy_id = conn.execute(
        "SELECT trosa.compat_next_id('inbox_items', trosa.compat_current_user())",
    ).fetchone()[0]
    target_id = conn.execute(
        "SELECT trosa.compat_uuid('inbox:' || trosa.compat_current_user() || ':' || ?::text)",
        (legacy_id,),
    ).fetchone()[0]
    payload = json.dumps({'compat_dedupe_key': dedupe_key}) if dedupe_key else '{}'
    conn.execute(
        '''INSERT INTO trosa.inbox_items
           (id, account_id, item_type, title, content, dedupe_key, status, created_at,
            resolved_at, resolution_reason, resolution_note, legacy_payload)
           VALUES (?, ?, ?, ?, ?, CASE WHEN ?='' THEN '' ELSE 'compat:' || trosa.compat_current_user() || ':' || ? END, ?, coalesce(trosa.compat_time(?), now()),
                   trosa.compat_time(?), ?, ?, ?::jsonb)''',
        (target_id, account_id, item_type, title, content, dedupe_key, dedupe_key, status,
         created_at, resolved_at, resolution_reason, resolution_note, payload),
    )
    conn.execute(
        '''INSERT INTO trosa.legacy_row_refs
           (organization_id, legacy_user_id, table_name, legacy_id, target_id)
           VALUES (trosa.compat_org_id(), trosa.compat_current_user(), 'inbox_items', ?, ?)''',
        (legacy_id, target_id),
    )
    return int(legacy_id)


def update_interaction(
    conn: Any, *, interaction_id: int, occurred_on: str, activity_type: str, direction: str,
    content: str, result: str, next_plan: str,
) -> None:
    """Edit one Interaction's canonical event fields."""
    if not postgres_mode():
        conn.execute(
            '''UPDATE follow_up_logs SET follow_date=?, activity_type=?, direction=?, content=?,
                   result=?, next_plan=?, updated_at=datetime('now','localtime') WHERE id=?''',
            (occurred_on, activity_type, direction, content, result, next_plan, interaction_id),
        )
        return
    changed = conn.execute(
        '''UPDATE trosa.timeline_events event
              SET occurred_at=trosa.compat_time(?), event_type=?, direction=?, content=?, result=?, next_plan=?
             FROM trosa.legacy_row_refs ref
            WHERE ref.organization_id=trosa.compat_org_id() AND ref.legacy_user_id=trosa.compat_current_user()
              AND ref.table_name='follow_up_logs' AND ref.legacy_id=? AND event.id=ref.target_id''',
        (occurred_on, activity_type, direction, content, result, next_plan, interaction_id),
    )
    if not changed.rowcount:
        raise ValueError('interaction is not visible to the current user')


def set_interaction_flag(conn: Any, *, interaction_id: int, field: str, value: bool) -> None:
    """Set a formal Interaction lifecycle/reporting flag in event payload."""
    if field not in {'is_deleted', 'is_reported'}:
        raise ValueError('unsupported interaction flag')
    if not postgres_mode():
        column = 'is_deleted' if field == 'is_deleted' else 'is_reported'
        conn.execute(f'UPDATE follow_up_logs SET {column}=? WHERE id=?', (1 if value else 0, interaction_id))
        return
    changed = conn.execute(
        '''UPDATE trosa.timeline_events event
              SET payload=coalesce(payload, '{}'::jsonb) || jsonb_build_object(?::text, ?::boolean)
             FROM trosa.legacy_row_refs ref
            WHERE ref.organization_id=trosa.compat_org_id() AND ref.legacy_user_id=trosa.compat_current_user()
              AND ref.table_name='follow_up_logs' AND ref.legacy_id=? AND event.id=ref.target_id''',
        (field, value, interaction_id),
    )
    if not changed.rowcount:
        raise ValueError('interaction is not visible to the current user')


def set_customer_deleted(conn: Any, *, customer_id: int, deleted: bool, changed_at: str = '') -> None:
    """Soft-delete or restore the canonical Customer Account."""
    if not postgres_mode():
        conn.execute(
            'UPDATE customers SET is_deleted=?, deleted_at=?, updated_at=? WHERE id=?',
            (1 if deleted else 0, changed_at if deleted else '', changed_at, customer_id),
        )
        return
    changed = conn.execute(
        '''UPDATE trosa.accounts account
              SET deleted_at=CASE WHEN ? THEN coalesce(trosa.compat_time(?), now()) ELSE NULL END,
                  updated_at=now()
             FROM trosa.account_legacy_refs ref
            WHERE ref.organization_id=trosa.compat_org_id() AND ref.legacy_user_id=trosa.compat_current_user()
              AND ref.legacy_customer_id=? AND account.id=ref.account_id''',
        (deleted, changed_at, customer_id),
    )
    if not changed.rowcount:
        raise ValueError('customer is not visible to the current user')


def update_customer_priority(conn: Any, *, customer_id: int, action: str, changed_at: str = '') -> None:
    """Apply a Customer pin action to canonical Account and detail facts."""
    if action not in {'pin', 'unpin', 'up', 'down'}:
        raise ValueError('unsupported priority action')
    if not postgres_mode():
        customer = conn.execute(
            'SELECT id, coalesce(is_pinned, 0) AS is_pinned, coalesce(pinned_order, 0) AS pinned_order '
            'FROM customers WHERE id=? AND coalesce(is_deleted,0)=0', (customer_id,),
        ).fetchone()
        if not customer:
            raise ValueError('customer is not visible to the current user')
        if action == 'pin':
            next_order = conn.execute('SELECT coalesce(max(pinned_order),0)+1 FROM customers WHERE coalesce(is_pinned,0)=1').fetchone()[0]
            conn.execute('UPDATE customers SET is_pinned=1, pinned_order=?, pinned_at=? WHERE id=?',
                         (next_order, changed_at, customer_id))
        elif action == 'unpin':
            conn.execute("UPDATE customers SET is_pinned=0, pinned_order=0, pinned_at='' WHERE id=?", (customer_id,))
        else:
            operator, ordering = ('<', 'DESC') if action == 'up' else ('>', 'ASC')
            neighbor = conn.execute(
                f'''SELECT id, pinned_order FROM customers WHERE coalesce(is_pinned,0)=1
                      AND pinned_order {operator} ? ORDER BY pinned_order {ordering} LIMIT 1''',
                (customer['pinned_order'],),
            ).fetchone()
            if neighbor:
                conn.execute('UPDATE customers SET pinned_order=? WHERE id=?', (neighbor['pinned_order'], customer_id))
                conn.execute('UPDATE customers SET pinned_order=? WHERE id=?', (customer['pinned_order'], neighbor['id']))
        return
    customer = conn.execute(
        '''SELECT account.id, account.pinned_order FROM trosa.accounts account
             JOIN trosa.account_legacy_refs ref ON ref.account_id=account.id
            WHERE ref.organization_id=trosa.compat_org_id() AND ref.legacy_user_id=trosa.compat_current_user()
              AND ref.legacy_customer_id=? AND account.deleted_at IS NULL''', (customer_id,),
    ).fetchone()
    if not customer:
        raise ValueError('customer is not visible to the current user')
    if action == 'pin':
        next_order = conn.execute(
            '''SELECT coalesce(max(account.pinned_order),0)+1 FROM trosa.accounts account
                 JOIN trosa.account_legacy_refs ref ON ref.account_id=account.id
                WHERE ref.organization_id=trosa.compat_org_id() AND ref.legacy_user_id=trosa.compat_current_user()
                  AND account.is_pinned AND account.deleted_at IS NULL''',
        ).fetchone()[0]
        conn.execute('UPDATE trosa.accounts SET is_pinned=true, pinned_order=?, updated_at=now() WHERE id=?',
                     (next_order, customer['id']))
        conn.execute(
            '''INSERT INTO trosa.customer_details (account_id, pinned_at, updated_at)
               VALUES (?, trosa.compat_time(?), now())
               ON CONFLICT (account_id) DO UPDATE SET pinned_at=excluded.pinned_at, updated_at=now()''',
            (customer['id'], changed_at),
        )
    elif action == 'unpin':
        conn.execute('UPDATE trosa.accounts SET is_pinned=false, pinned_order=0, updated_at=now() WHERE id=?',
                     (customer['id'],))
        conn.execute('UPDATE trosa.customer_details SET pinned_at=NULL, updated_at=now() WHERE account_id=?',
                     (customer['id'],))
    else:
        operator, ordering = ('<', 'DESC') if action == 'up' else ('>', 'ASC')
        neighbor = conn.execute(
            f'''SELECT account.id, account.pinned_order FROM trosa.accounts account
                 JOIN trosa.account_legacy_refs ref ON ref.account_id=account.id
                WHERE ref.organization_id=trosa.compat_org_id() AND ref.legacy_user_id=trosa.compat_current_user()
                  AND account.is_pinned AND account.deleted_at IS NULL AND account.pinned_order {operator} ?
                ORDER BY account.pinned_order {ordering} LIMIT 1''',
            (customer['pinned_order'],),
        ).fetchone()
        if neighbor:
            conn.execute('UPDATE trosa.accounts SET pinned_order=?, updated_at=now() WHERE id=?',
                         (neighbor['pinned_order'], customer['id']))
            conn.execute('UPDATE trosa.accounts SET pinned_order=?, updated_at=now() WHERE id=?',
                         (customer['pinned_order'], neighbor['id']))


def update_contact(conn: Any, *, contact_id: int, values: dict[str, Any]) -> None:
    """Update Contact identity and attributes without using a compatibility write."""
    if not postgres_mode():
        conn.execute(
            '''UPDATE contacts SET name=?, title=?, email=?, phone=?, whatsapp=?, linkedin=?,
                   preferred_channel=?, contact_type=?, is_primary=?, notes=? WHERE id=?''',
            (values.get('name', ''), values.get('title', ''), values.get('email', ''), values.get('phone', ''),
             values.get('whatsapp', ''), values.get('linkedin', ''), values.get('preferred_channel', ''),
             values.get('contact_type', 'person'), values.get('is_primary', 0), values.get('notes', ''), contact_id),
        )
        return
    ref = conn.execute(
        '''SELECT * FROM trosa.contact_legacy_refs WHERE organization_id=trosa.compat_org_id()
              AND legacy_user_id=trosa.compat_current_user() AND legacy_contact_id=?''', (contact_id,),
    ).fetchone()
    if not ref:
        raise ValueError('contact is not visible to the current user')
    conn.execute('UPDATE core.people SET full_name=?, normalized_name=lower(?) WHERE id=?',
                 (values.get('name', ''), values.get('name', ''), ref['person_id']))
    if ref['contact_method_id']:
        conn.execute('UPDATE core.contact_methods SET value=?, normalized_value=lower(?), updated_at=now() WHERE id=?',
                     (values.get('email', ''), values.get('email', ''), ref['contact_method_id']))
    elif values.get('email'):
        method_id = conn.execute(
            "SELECT trosa.compat_uuid('contact-method:' || trosa.compat_current_user() || ':' || ?::text)",
            (contact_id,),
        ).fetchone()[0]
        conn.execute(
            '''INSERT INTO core.contact_methods
               (id, organization_id, person_id, kind, value, normalized_value)
               VALUES (?, trosa.compat_org_id(), ?, 'email', ?, lower(?))''',
            (method_id, ref['person_id'], values.get('email', ''), values.get('email', '')),
        )
        conn.execute(
            '''UPDATE trosa.contact_legacy_refs SET contact_method_id=?
                WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=trosa.compat_current_user()
                  AND legacy_contact_id=?''',
            (method_id, contact_id),
        )
    conn.execute(
        '''UPDATE trosa.contact_legacy_refs SET name=?, title=?, phone=?, whatsapp=?, linkedin=?,
               preferred_channel=?, contact_type=?, is_primary=?, notes=?, updated_at=now()
             WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=trosa.compat_current_user()
               AND legacy_contact_id=?''',
        (values.get('name', ''), values.get('title', ''), values.get('phone', ''), values.get('whatsapp', ''),
         values.get('linkedin', ''), values.get('preferred_channel', ''), values.get('contact_type', 'person'),
         bool(values.get('is_primary')), values.get('notes', ''), contact_id),
    )


def delete_contact(conn: Any, *, contact_id: int) -> None:
    """Remove a Contact from its Customer without destroying shared identity facts."""
    if not postgres_mode():
        conn.execute('DELETE FROM contacts WHERE id=?', (contact_id,))
        return
    changed = conn.execute(
        '''DELETE FROM trosa.contact_legacy_refs
            WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=trosa.compat_current_user()
              AND legacy_contact_id=?''', (contact_id,),
    )
    if not changed.rowcount:
        raise ValueError('contact is not visible to the current user')


def create_outreach_message(conn: Any, *, customer_id: int, subject: str, content: str, sent_on: str, reply_status: str, created_at: str) -> int:
    """Record a canonical outbound delivery and return its API id."""
    if not postgres_mode():
        cursor = conn.execute('''INSERT INTO outreach_emails (customer_id,subject,content,sent_date,reply_status,created_at)
                                 VALUES (?,?,?,?,?,?)''',
                              (customer_id, subject, content, sent_on, reply_status, created_at))
        return int(cursor.lastrowid)
    account = conn.execute('''SELECT account_id FROM trosa.account_legacy_refs WHERE organization_id=trosa.compat_org_id()
                              AND legacy_user_id=trosa.compat_current_user() AND legacy_customer_id=?''', (customer_id,)).fetchone()
    if not account: raise ValueError('customer is not visible to the current user')
    legacy_id = conn.execute("SELECT trosa.compat_next_id('outreach_emails', trosa.compat_current_user())").fetchone()[0]
    message_id = conn.execute("SELECT trosa.compat_uuid('outreach:' || trosa.compat_current_user() || ':' || ?::text)", (legacy_id,)).fetchone()[0]
    conn.execute('''INSERT INTO trosa.outreach_messages (id,account_id,subject,body,sent_at,reply_status,created_at)
                    VALUES (?,?,?,?,trosa.compat_time(?),?,coalesce(trosa.compat_time(?),now()))''',
                 (message_id, account['account_id'], subject, content, sent_on, reply_status, created_at))
    conn.execute('''INSERT INTO trosa.legacy_row_refs (organization_id,legacy_user_id,table_name,legacy_id,target_id)
                    VALUES (trosa.compat_org_id(),trosa.compat_current_user(),'outreach_emails',?,?)''', (legacy_id,message_id))
    return int(legacy_id)


def update_outreach_message(conn: Any, *, outreach_id: int, reply_status: str, reply_content: str, reply_on: str) -> None:
    if not postgres_mode():
        conn.execute('UPDATE outreach_emails SET reply_status=?, reply_content=?, reply_date=? WHERE id=?',
                     (reply_status, reply_content, reply_on, outreach_id)); return
    changed=conn.execute('''UPDATE trosa.outreach_messages message SET reply_status=?,reply_content=?,reply_at=trosa.compat_time(?)
                             FROM trosa.legacy_row_refs ref WHERE ref.organization_id=trosa.compat_org_id()
                             AND ref.legacy_user_id=trosa.compat_current_user() AND ref.table_name='outreach_emails'
                             AND ref.legacy_id=? AND message.id=ref.target_id''',(reply_status,reply_content,reply_on,outreach_id))
    if not changed.rowcount: raise ValueError('outreach is not visible')


def delete_outreach_message(conn: Any, *, outreach_id: int) -> None:
    if not postgres_mode():
        conn.execute('DELETE FROM outreach_emails WHERE id=?', (outreach_id,)); return
    ref = conn.execute('''DELETE FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id()
                          AND legacy_user_id=trosa.compat_current_user() AND table_name='outreach_emails'
                          AND legacy_id=? RETURNING target_id''', (outreach_id,)).fetchone()
    if not ref: raise ValueError('outreach is not visible')
    conn.execute('''DELETE FROM trosa.outreach_messages message WHERE id=?
                    AND NOT EXISTS (SELECT 1 FROM trosa.legacy_row_refs WHERE target_id=message.id)''', (ref['target_id'],))


def set_outreach_reported(conn: Any, *, outreach_id: int, reported: bool) -> None:
    if not postgres_mode():
        conn.execute('UPDATE outreach_emails SET is_reported=? WHERE id=?', (1 if reported else 0, outreach_id)); return
    changed = conn.execute(
        '''UPDATE trosa.outreach_messages message
              SET legacy_payload=coalesce(legacy_payload,'{}'::jsonb) || jsonb_build_object('is_reported', ?::boolean)
             FROM trosa.legacy_row_refs ref WHERE ref.organization_id=trosa.compat_org_id()
               AND ref.legacy_user_id=trosa.compat_current_user() AND ref.table_name='outreach_emails'
               AND ref.legacy_id=? AND message.id=ref.target_id''', (reported, outreach_id))
    if not changed.rowcount: raise ValueError('outreach is not visible')


def set_customer_stage(conn: Any, *, customer_ids: Iterable[int], stage: str) -> int:
    """Set the formal user-scoped Customer business stage."""
    ids = _ids(customer_ids)
    if not ids:
        return 0
    placeholders = ','.join('?' for _ in ids)
    if not postgres_mode():
        return int(conn.execute(
            f'UPDATE customers SET business_stage=?, updated_at=datetime(\'now\',\'localtime\') WHERE id IN ({placeholders})',
            [stage, *ids],
        ).rowcount or 0)
    return int(conn.execute(
        f'''INSERT INTO trosa.customer_states
               (organization_id, legacy_user_id, legacy_customer_id, account_id, business_stage, updated_at)
           SELECT ref.organization_id, ref.legacy_user_id, ref.legacy_customer_id, ref.account_id, ?, now()
             FROM trosa.account_legacy_refs ref
            WHERE ref.organization_id=trosa.compat_org_id() AND ref.legacy_user_id=trosa.compat_current_user()
              AND ref.legacy_customer_id IN ({placeholders})
           ON CONFLICT (organization_id, legacy_user_id, legacy_customer_id)
           DO UPDATE SET business_stage=excluded.business_stage, updated_at=now()''',
        [stage, *ids],
    ).rowcount or 0)


def set_customer_level(conn: Any, *, customer_ids: Iterable[int], level: str) -> int:
    """Set the canonical Customer priority level."""
    ids = _ids(customer_ids)
    if not ids:
        return 0
    placeholders = ','.join('?' for _ in ids)
    if not postgres_mode():
        return int(conn.execute(
            f'UPDATE customers SET level=?, updated_at=datetime(\'now\',\'localtime\') WHERE id IN ({placeholders})',
            [level, *ids],
        ).rowcount or 0)
    return int(conn.execute(
        f'''UPDATE trosa.accounts account SET priority_level=?, updated_at=now()
              FROM trosa.account_legacy_refs ref
             WHERE ref.organization_id=trosa.compat_org_id() AND ref.legacy_user_id=trosa.compat_current_user()
               AND ref.legacy_customer_id IN ({placeholders}) AND account.id=ref.account_id''',
        [level, *ids],
    ).rowcount or 0)


def update_customer(conn: Any, *, customer_id: int, values: dict[str, Any]) -> None:
    """Update the canonical Customer aggregate without writing a legacy row."""
    if not postgres_mode():
        raise ValueError('SQLite callers retain their local aggregate adapter')
    ref = conn.execute(
        '''SELECT ref.account_id, account.company_id FROM trosa.account_legacy_refs ref
             JOIN trosa.accounts account ON account.id=ref.account_id
            WHERE ref.organization_id=trosa.compat_org_id() AND ref.legacy_user_id=trosa.compat_current_user()
              AND ref.legacy_customer_id=?''', (customer_id,),
    ).fetchone()
    if not ref:
        raise ValueError('customer is not visible to the current user')
    conn.execute(
        '''UPDATE core.companies SET canonical_name=?, normalized_name=lower(?), country_code=?, website=?, updated_at=now()
             WHERE id=?''',
        (values.get('company', ''), values.get('company', ''), values.get('country', ''), values.get('website', ''), ref['company_id']),
    )
    conn.execute(
        '''UPDATE trosa.accounts SET display_name=?, priority_level=?, profile=?, field=?, industry=?, company_size=?,
               annual_revenue=?, tags=?, last_contact_at=trosa.compat_time(?), next_follow_up_at=trosa.compat_time(?),
               updated_at=now() WHERE id=?''',
        (values.get('name', ''), values.get('level', ''), values.get('profile', ''), values.get('field', ''),
         values.get('industry', ''), values.get('company_size', ''), values.get('annual_revenue', ''),
         values.get('tags', ''), values.get('last_contact', ''), values.get('next_follow_up', ''), ref['account_id']),
    )
    conn.execute(
        '''INSERT INTO trosa.customer_details (account_id, notes, system_notes, import_source, manual_next_task, updated_at)
           VALUES (?, ?, ?, ?, ?, now()) ON CONFLICT (account_id) DO UPDATE
           SET notes=excluded.notes, system_notes=excluded.system_notes, import_source=excluded.import_source,
               manual_next_task=excluded.manual_next_task, updated_at=now()''',
        (ref['account_id'], values.get('notes', ''), values.get('system_notes', ''), values.get('import_source', ''),
         bool(values.get('manual_next_follow'))),
    )
    conn.execute(
        '''INSERT INTO trosa.customer_states
               (organization_id, legacy_user_id, legacy_customer_id, account_id, business_stage, business_role, customer_judgment, updated_at)
           VALUES (trosa.compat_org_id(), trosa.compat_current_user(), ?, ?, ?, ?, ?, now())
           ON CONFLICT (organization_id, legacy_user_id, legacy_customer_id) DO UPDATE
           SET business_stage=excluded.business_stage, business_role=excluded.business_role,
               customer_judgment=excluded.customer_judgment, updated_at=now()''',
        (customer_id, ref['account_id'], values.get('business_stage', ''), values.get('business_role', ''),
         values.get('customer_judgment', '')),
    )
    # Modern Customer edits must not leave stale SQLite-shaped fields in the
    # compatibility payload.  The payload remains useful provenance, but the
    # old projection must fall back to the canonical Account/Company/Details
    # facts after a modern write instead of replaying an earlier snapshot.
    legacy_customer_fields = [
        'name', 'company', 'country', 'level', 'type', 'website', 'profile',
        'field', 'status', 'last_contact', 'next_follow_up', 'manual_next_follow',
        'customer_type', 'industry', 'company_size', 'annual_revenue', 'tags',
        'import_source', 'external_source', 'external_id', 'attention_state',
        'attention_reason', 'attention_updated_at', 'attention_review_date',
        'is_pinned', 'pinned_order', 'pinned_at', 'is_deleted', 'deleted_at',
        'business_stage', 'business_role', 'customer_judgment',
    ]
    conn.execute(
        '''UPDATE trosa.account_legacy_refs
              SET legacy_payload=coalesce(legacy_payload, '{}'::jsonb) - %s::text[]
            WHERE organization_id=trosa.compat_org_id()
              AND legacy_user_id=trosa.compat_current_user()
              AND legacy_customer_id=?''',
        (legacy_customer_fields, customer_id),
    )
    conn.execute(
        '''UPDATE trosa.accounts
              SET legacy_payload=coalesce(legacy_payload, '{}'::jsonb) - %s::text[]
            WHERE id=?''',
        (legacy_customer_fields, ref['account_id']),
    )


def create_customer(conn: Any, *, values: dict[str, Any]) -> int:
    """Create the complete canonical Customer aggregate and its API id."""
    if not postgres_mode():
        raise ValueError('SQLite callers retain their local aggregate adapter')
    customer_id = conn.execute(
        "SELECT trosa.compat_next_id('customers', trosa.compat_current_user())",
    ).fetchone()[0]
    company_id = conn.execute(
        "SELECT trosa.compat_uuid('modern-company:' || trosa.compat_current_user() || ':' || ?::text)",
        (customer_id,),
    ).fetchone()[0]
    account_id = conn.execute(
        "SELECT trosa.compat_uuid('modern-account:' || trosa.compat_current_user() || ':' || ?::text)",
        (customer_id,),
    ).fetchone()[0]
    conn.execute(
        '''INSERT INTO core.companies
           (id, organization_id, canonical_name, normalized_name, website, country_code)
           VALUES (?, trosa.compat_org_id(), ?, lower(?), ?, ?)''',
        (company_id, values.get('company', ''), values.get('company', ''), values.get('website', ''), values.get('country', '')),
    )
    conn.execute(
        '''INSERT INTO trosa.accounts
           (id, organization_id, company_id, display_name, priority_level, profile, field, industry,
            company_size, annual_revenue, tags, last_contact_at, next_follow_up_at, legacy_payload)
           VALUES (?, trosa.compat_org_id(), ?, ?, ?, ?, ?, ?, ?, ?, ?, trosa.compat_time(?),
                   trosa.compat_time(?), '{}'::jsonb)''',
        (account_id, company_id, values.get('name', ''), values.get('level', ''), values.get('profile', ''),
         values.get('field', ''), values.get('industry', ''), values.get('company_size', ''),
         values.get('annual_revenue', ''), values.get('tags', ''), values.get('last_contact', ''), values.get('next_follow_up', '')),
    )
    conn.execute(
        '''INSERT INTO trosa.account_legacy_refs
           (organization_id, legacy_user_id, legacy_customer_id, account_id, source_db, legacy_payload)
           VALUES (trosa.compat_org_id(), trosa.compat_current_user(), ?, ?, 'modern-trosa', ?::jsonb)''',
        (customer_id, account_id, json.dumps(values)),
    )
    conn.execute(
        '''INSERT INTO trosa.customer_details
           (account_id, notes, system_notes, import_source, external_source, external_id,
            manual_next_task)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (account_id, values.get('notes', ''), values.get('system_notes', ''),
         values.get('import_source', 'manual'), values.get('external_source', ''),
         values.get('external_id', ''), bool(values.get('manual_next_follow'))),
    )
    conn.execute(
        '''INSERT INTO trosa.customer_states
           (organization_id, legacy_user_id, legacy_customer_id, account_id, business_stage, business_role, customer_judgment)
           VALUES (trosa.compat_org_id(), trosa.compat_current_user(), ?, ?, ?, ?, ?)''',
        (customer_id, account_id, values.get('business_stage', ''), values.get('business_role', ''),
         values.get('customer_judgment', '')),
    )
    return int(customer_id)


def update_task(
    conn: Any, *, task_id: int, title: str, content: str, reason: str, due_on: str,
    now: str = '', manual_order: int | None = None,
) -> None:
    """Edit a Task through ``trosa.tasks`` without reopening a reminder model."""
    if not postgres_mode():
        assignments = 'title=?, content=?, reason=?, remind_date=?, updated_at=?'
        values: list[Any] = [title, content, reason, due_on, now]
        if manual_order is not None:
            assignments += ', manual_order=?'
            values.append(manual_order)
        values.append(task_id)
        conn.execute(f'UPDATE reminders SET {assignments} WHERE id=?', values)
        return
    assignments = '''title=?, content=?, reason=?, due_at=trosa.compat_time(?), updated_at=now()'''
    values = [title, content, reason, due_on]
    if manual_order is not None:
        assignments += ', manual_order=?'
        values.append(manual_order)
    values.append(task_id)
    changed = conn.execute(
        f'''UPDATE trosa.tasks task SET {assignments}
              FROM trosa.legacy_row_refs ref
             WHERE ref.organization_id=trosa.compat_org_id()
               AND ref.legacy_user_id=trosa.compat_current_user()
               AND ref.table_name='reminders' AND ref.legacy_id=?
               AND task.id=ref.target_id''',
        values,
    )
    if not changed.rowcount:
        raise ValueError('task is not visible to the current user')


def customer_interactions(
    conn: Any, customer_id: int, *, limit: int | None = None, offset: int = 0,
) -> list[dict]:
    """Return a single product-level timeline across communication and email.

    ``kind`` describes what happened, while ``source`` describes where the
    durable fact arrived.  Upper layers do not branch on historical table
    names.  An outbound delivery remains an interaction but does not by itself
    establish a customer relationship; that rule lives in ``customer_facts``.
    """
    if postgres_mode():
        query = '''SELECT id, customer_id, kind, occurred_on, activity_type,
                          direction, content, result, next_plan, source,
                          is_reported, delivery_status, reply_date, created_at
                     FROM trosa.customer_interactions
                    WHERE customer_id=?
                    ORDER BY occurred_on DESC, created_at DESC, id DESC'''
        params: list[Any] = [customer_id]
        if limit is not None:
            query += ' LIMIT ? OFFSET ?'
            params.extend([max(1, int(limit)), max(0, int(offset))])
        items = [dict(row) for row in conn.execute(query, params).fetchall()]
        _add_compatibility_aliases(items)
        return items
    query = '''SELECT * FROM (
                   SELECT 'communication' AS kind, f.id, f.customer_id,
                          f.follow_date AS occurred_on, f.created_at,
                          f.activity_type, f.direction, f.content, f.result,
                          f.next_plan, f.source, COALESCE(f.is_reported, 0) AS is_reported,
                          '' AS delivery_status, '' AS reply_date
                     FROM follow_up_logs f
                    WHERE f.customer_id=? AND (f.is_deleted=0 OR f.is_deleted IS NULL)
                   UNION ALL
                   SELECT 'email' AS kind, o.id, o.customer_id,
                          o.sent_date AS occurred_on, o.created_at,
                          'outreach_email' AS activity_type, 'outbound' AS direction,
                          o.subject AS content, o.reply_content AS result,
                          '' AS next_plan, 'gmail_delivery' AS source,
                          COALESCE(o.is_reported, 0) AS is_reported,
                          COALESCE(o.reply_status, '') AS delivery_status,
                          COALESCE(o.reply_date, '') AS reply_date
                     FROM outreach_emails o WHERE o.customer_id=?
               ) interactions
              ORDER BY occurred_on DESC, created_at DESC, id DESC'''
    params: list[Any] = [customer_id, customer_id]
    if limit is not None:
        query += ' LIMIT ? OFFSET ?'
        params.extend([max(1, int(limit)), max(0, int(offset))])
    items = [dict(row) for row in conn.execute(query, params).fetchall()]
    # ``type`` and ``date`` keep existing API clients working during the
    # endpoint transition. New consumers use ``kind`` and ``occurred_on``.
    _add_compatibility_aliases(items)
    return items


def _add_compatibility_aliases(items: list[dict]) -> None:
    """Keep response contracts stable while callers move to Interaction."""
    for item in items:
        item['type'] = 'follow' if item['kind'] == 'communication' else 'outreach'
        item['date'] = item['occurred_on']
        if item['kind'] == 'communication':
            item['follow_date'] = item['occurred_on']
        else:
            item['sent_date'] = item['occurred_on']
            item['subject'] = item.get('content') or ''
            item['reply_status'] = item.get('delivery_status') or ''


def customer_interaction_count(conn: Any, customer_id: int) -> int:
    if postgres_mode():
        row = conn.execute(
            'SELECT COUNT(*) FROM trosa.customer_interactions WHERE customer_id=?', (customer_id,),
        ).fetchone()
        return int(row[0] if row else 0)
    row = conn.execute(
        '''SELECT COUNT(*) FROM (
               SELECT id FROM follow_up_logs
                WHERE customer_id=? AND (is_deleted=0 OR is_deleted IS NULL)
               UNION ALL SELECT id FROM outreach_emails WHERE customer_id=?
           ) history''',
        (customer_id, customer_id),
    ).fetchone()
    return int(row[0] if row else 0)


def customer_facts(conn: Any, customer_ids: Iterable[int]) -> dict[int, dict]:
    """Project the one shared definition of relationship and next-work facts."""
    ids = _ids(customer_ids)
    facts = {customer_id: {
        'contact_state': 'uncontacted', 'has_contact': False,
        'latest_communication_date': '', 'latest_activity': None,
        'next_task': None, 'next_task_date': '', 'next_task_title': '',
        'waiting_reply': False, 'latest_email_date': '', 'latest_email_status': '',
    } for customer_id in ids}
    if not ids:
        return facts

    if postgres_mode():
        # These canonical views are intentionally the only PostgreSQL read
        # dependency for current relationship and work meaning.  The bounded
        # per-customer loop keeps the projection readable and is used only for
        # the current page of customers (at most 100 records).
        for customer_id in ids:
            fact = facts[customer_id]
            for item in customer_interactions(conn, customer_id):
                if item['kind'] == 'communication':
                    if not fact['latest_communication_date']:
                        fact['latest_communication_date'] = item.get('occurred_on') or ''
                        fact['latest_activity'] = item
                    if item.get('direction') in ('inbound', 'two_way') or item.get('activity_type') == 'customer_reply':
                        fact['has_contact'] = True
                elif item.get('delivery_status') == 'replied':
                    fact['has_contact'] = True
                if item['kind'] == 'email' and fact['latest_activity'] is None:
                    fact['latest_activity'] = item
                    fact['waiting_reply'] = item.get('delivery_status') in ('pending', 'no_reply')
                if item['kind'] == 'email' and not fact['latest_email_date']:
                    fact['latest_email_date'] = item.get('occurred_on') or ''
                    fact['latest_email_status'] = item.get('delivery_status') or ''
            tasks = customer_tasks(conn, customer_id)
            if tasks:
                fact['next_task'] = tasks[0]
                fact['next_task_date'] = tasks[0].get('remind_date') or ''
                fact['next_task_title'] = tasks[0].get('title') or tasks[0].get('content') or ''
            fact['contact_state'] = 'contacted' if fact['has_contact'] else 'uncontacted'
        return facts

    marks = ','.join('?' for _ in ids)
    communications = conn.execute(
        f'''SELECT f.customer_id, f.id, f.follow_date, f.content, f.result,
                   f.activity_type, f.direction, f.source, f.created_at
              FROM follow_up_logs f
             WHERE f.customer_id IN ({marks})
               AND (f.is_deleted=0 OR f.is_deleted IS NULL)
             ORDER BY f.follow_date DESC, f.created_at DESC, f.id DESC''', ids,
    ).fetchall()
    for row in communications:
        item = dict(row)
        fact = facts[item['customer_id']]
        if not fact['latest_communication_date']:
            fact['latest_communication_date'] = item.get('follow_date') or ''
            fact['latest_activity'] = {
                'kind': 'communication', 'date': item.get('follow_date') or '',
                'content': item.get('content') or '', 'result': item.get('result') or '',
                'activity_type': item.get('activity_type') or '',
                'direction': item.get('direction') or '', 'source': item.get('source') or '',
            }
        if item.get('direction') in ('inbound', 'two_way') or item.get('activity_type') == 'customer_reply':
            fact['has_contact'] = True

    tasks = conn.execute(
        f'''SELECT r.customer_id, r.id, r.title, r.content, r.reason,
                   r.remind_date, r.reminder_type, r.source_activity_id
              FROM reminders r
             WHERE r.customer_id IN ({marks}) AND r.is_done=0
               AND COALESCE(r.reminder_type, 'follow_up') NOT LIKE 'outreach_%'
             ORDER BY r.customer_id, r.remind_date, r.manual_order, r.id''', ids,
    ).fetchall()
    for row in tasks:
        item = dict(row)
        fact = facts[item['customer_id']]
        if fact['next_task'] is None:
            fact['next_task'] = item
            fact['next_task_date'] = item.get('remind_date') or ''
            fact['next_task_title'] = item.get('title') or item.get('content') or ''

    emails = conn.execute(
        f'''SELECT o.customer_id, o.id, o.sent_date, o.subject, o.reply_status,
                   o.reply_date, o.reply_content, o.created_at
              FROM outreach_emails o WHERE o.customer_id IN ({marks})
             ORDER BY o.customer_id, o.sent_date DESC, o.created_at DESC, o.id DESC''', ids,
    ).fetchall()
    seen_email: set[int] = set()
    for row in emails:
        item = dict(row)
        customer_id = item['customer_id']
        fact = facts[customer_id]
        if item.get('reply_status') == 'replied':
            fact['has_contact'] = True
        if customer_id not in seen_email:
            seen_email.add(customer_id)
            fact['latest_email_date'] = item.get('sent_date') or ''
            fact['latest_email_status'] = item.get('reply_status') or ''
            fact['waiting_reply'] = item.get('reply_status') in ('pending', 'no_reply')
            if fact['latest_activity'] is None:
                fact['latest_activity'] = {
                    'kind': 'email', 'date': item.get('sent_date') or '',
                    'content': item.get('subject') or '', 'result': item.get('reply_content') or '',
                    'delivery_status': item.get('reply_status') or '', 'source': 'gmail_delivery',
                }
    for fact in facts.values():
        fact['contact_state'] = 'contacted' if fact['has_contact'] else 'uncontacted'
    return facts
