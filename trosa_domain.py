"""The small, explicit business read model used by current Trosa surfaces.

The PostgreSQL store has canonical tables and a compatibility projection for
historical identifiers.  This module is the boundary for the *business*
meaning of those records: callers receive interactions and tasks, never a
requirement to understand which transport table supplied them.  SQLite remains
available for isolated development and recovery, so the queries deliberately
use the same current relation names on both stores.

Write actions remain in ``app.py`` for now because they share Flask's
authentication, audit and undo transaction hooks.  Read semantics must not be
duplicated merely because an action has several entry points.
"""

from __future__ import annotations

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
        conn.execute(
            '''INSERT INTO trosa.timeline_events
               (id, account_id, contact_method_id, event_type, direction, content, result, next_plan,
                source_module, source_reference, occurred_at, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, trosa.compat_time(?), '{}'::jsonb)
               ON CONFLICT (id) DO NOTHING''',
            (target_id, account['account_id'], contact_method_id, activity_type, direction, content, result,
             next_plan, source, source_reference, occurred_on),
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
           (customer_id, content, follow_date, result, next_plan, activity_type, direction, contact_id, source, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))''',
        (customer_id, content, occurred_on, result, next_plan, activity_type, direction, contact_id, source),
    )
    return int(cursor.lastrowid)


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
