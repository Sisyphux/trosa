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
import uuid
from typing import Any, Iterable

from db import postgres_mode


def _ids(customer_ids: Iterable[int]) -> list[int]:
    return list(dict.fromkeys(int(value) for value in customer_ids if value is not None))


def customer_tasks(conn: Any, customer_id: int, *, include_done: bool = False, customer_ids=None) -> list[dict]:
    """Return human tasks in the one ordering used by Today and Customer.

    Retired outreach scheduler rows are delivery history, not tasks.  They
    remain accessible through history/recovery but cannot become a next step.
    """
    if postgres_mode():
        done_clause = '' if include_done else "AND status='open'"
        ids = _ids(customer_ids) if customer_ids is not None else [customer_id]
        if not ids:
            return []
        marks = ','.join('?' for _ in ids)
        rows = conn.execute(
            f'''SELECT id, customer_id, title, content, reason, due_date AS remind_date,
                       task_type AS reminder_type, source_activity_legacy_id AS source_activity_id,
                       CASE WHEN status='done' THEN 1 ELSE 0 END AS is_done,
                       completed_at, created_at
                  FROM trosa.customer_tasks
                 WHERE customer_id IN ({marks}) {done_clause}
                 ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END,
                          due_date ASC, manual_order ASC, id ASC''',
            ids,
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


def customer_contacts(conn: Any, customer_id: int, *, customer_ids=None) -> list[dict]:
    """Return Contacts without making product callers depend on compat tables."""
    relation = 'trosa.customer_contacts' if postgres_mode() else 'contacts'
    ids = _ids(customer_ids) if customer_ids is not None else [customer_id]
    if not ids:
        return []
    marks = ','.join('?' for _ in ids)
    rows = conn.execute(
        f'''SELECT id, customer_id, name, title, email, phone, whatsapp, linkedin,
                   preferred_channel, contact_type, is_primary, notes, created_at
              FROM {relation} WHERE customer_id IN ({marks})
             ORDER BY is_primary DESC, created_at DESC, id DESC''', ids,
    ).fetchall()
    return [dict(row) for row in rows]


def today_tasks(conn: Any, *, due_on_or_before: str, limit: int | None = None) -> list[dict]:
    """The one Today work view.  It is a projection of open Tasks, never a second queue.

    Same-customer same-date open follow-ups are a write-path bug (see
    ``merge_open_task``), never two real commitments.  The read collapses them
    to the earliest row so Today shows one entry per customer per date even
    when legacy data still holds duplicates.  The canonical task id is the
    first key because one account can still have multiple legacy customer
    aliases; a view fan-out must never turn one task into multiple Today cards.
    """
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
    rows = [dict(row) for row in conn.execute(query, params).fetchall()]
    seen_task_ids: set[Any] = set()
    seen_customer_days: set[tuple[Any, str]] = set()
    collapsed: list[dict] = []
    for row in rows:
        task_id = row.get('id')
        customer_day = (row.get('customer_id'), str(row.get('remind_date') or '')[:10])
        if customer_day in seen_customer_days:
            continue
        if task_id not in (None, ''):
            if task_id in seen_task_ids:
                continue
            seen_task_ids.add(task_id)
        seen_customer_days.add(customer_day)
        collapsed.append(row)
    if limit is not None:
        collapsed = collapsed[:max(1, int(limit))]
    return collapsed


def deduplicate_open_follow_ups(conn: Any) -> int:
    """Merge legacy same-customer same-date open follow-ups, keeping the earliest.

    Returns the number of duplicate rows completed.  Survivor rows keep the
    earliest id and gain the merged ``reason`` (``' / '``-joined).  This is the
    offline repair for rows created before reschedule/edit paths merged on
    collision; new writes must not create them in the first place.
    """
    merged = 0
    if postgres_mode():
        groups = conn.execute(
            '''SELECT customer_id, due_date, array_agg(id ORDER BY id) AS ids
                 FROM trosa.customer_tasks
                WHERE status='open' AND task_type='follow_up'
                GROUP BY customer_id, due_date HAVING count(*) > 1''',
        ).fetchall()
        for group in groups:
            ids = list(group['ids'] or [])
            if len(ids) < 2:
                continue
            survivor = min(ids)
            reasons: list[str] = []
            for task_id in sorted(ids):
                row = conn.execute(
                    'SELECT reason FROM trosa.customer_tasks WHERE id=?', (task_id,),
                ).fetchone()
                if row and str(row['reason'] or '').strip() and str(row['reason']).strip() not in reasons:
                    reasons.append(str(row['reason']).strip())
            conn.execute(
                '''UPDATE trosa.tasks task SET reason=?, updated_at=now()
                     FROM trosa.legacy_row_refs ref
                    WHERE ref.organization_id=trosa.compat_org_id()
                      AND ref.legacy_user_id=trosa.compat_current_user()
                      AND ref.table_name='reminders' AND ref.legacy_id=?
                      AND task.id=ref.target_id AND task.status='open' ''',
                (' / '.join(reasons)[:2000], survivor),
            )
            for duplicate_id in sorted(ids):
                if duplicate_id == survivor:
                    continue
                conn.execute(
                    '''UPDATE trosa.tasks task SET status='done',
                           completed_at=coalesce(completed_at, now()), updated_at=now()
                      FROM trosa.legacy_row_refs ref
                     WHERE ref.organization_id=trosa.compat_org_id()
                       AND ref.legacy_user_id=trosa.compat_current_user()
                       AND ref.table_name='reminders' AND ref.legacy_id=?
                       AND task.id=ref.target_id AND task.status='open' ''',
                    (duplicate_id,),
                )
                merged += 1
        return merged
    groups = conn.execute(
        '''SELECT customer_id, remind_date, group_concat(id) AS ids, count(*) AS n
             FROM reminders
            WHERE is_done=0 AND COALESCE(reminder_type, 'follow_up')='follow_up'
            GROUP BY customer_id, remind_date HAVING n > 1''',
    ).fetchall()
    for group in groups:
        ids = sorted(int(value) for value in str(group['ids'] or '').split(',') if value.strip())
        if len(ids) < 2:
            continue
        survivor = ids[0]
        reasons = []
        for task_id in ids:
            row = conn.execute('SELECT reason FROM reminders WHERE id=?', (task_id,)).fetchone()
            if row and str(row['reason'] or '').strip() and str(row['reason']).strip() not in reasons:
                reasons.append(str(row['reason']).strip())
        conn.execute('UPDATE reminders SET reason=? WHERE id=?', (' / '.join(reasons)[:2000], survivor))
        for duplicate_id in ids[1:]:
            conn.execute(
                "UPDATE reminders SET is_done=1, completed_at=datetime('now','localtime') WHERE id=? AND is_done=0",
                (duplicate_id,),
            )
            merged += 1
    return merged


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
        # Identity includes organization, user and owning account so two
        # customers can share a source reference (or two users can share a
        # per-user legacy id) without addressing the same UUID.
        target_id = conn.execute(
            '''SELECT trosa.compat_uuid(
                   'interaction:' || trosa.compat_org_id()::text || ':'
                   || trosa.compat_current_user() || ':' || ?::text || ':'
                   || ? || ':' || ?::text)''',
            (str(account['account_id']), source, str(source_reference or legacy_id)),
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
            'customer_id': int(customer_id),
            'is_reported': bool(is_reported),
            **({'related_task_id': int(related_task_id)} if related_task_id else {}),
        }
        conn.execute(
            '''INSERT INTO trosa.timeline_events
               (id, account_id, contact_method_id, event_type, direction, content, result, next_plan,
                source_module, source_reference, occurred_at, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, trosa.compat_time(?), ?::jsonb)
               ON CONFLICT (id) DO UPDATE SET
                   contact_method_id=excluded.contact_method_id,
                   event_type=excluded.event_type, direction=excluded.direction,
                   content=excluded.content, result=excluded.result,
                   next_plan=excluded.next_plan, source_reference=excluded.source_reference,
                   occurred_at=excluded.occurred_at, payload=excluded.payload''',
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
                 WHERE customer_id=? AND is_done=0 AND COALESCE(reminder_type, 'follow_up')='follow_up'
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
        # Serialize the check-then-insert on this account and date.  Without
        # the lock two concurrent writers both observe no open task and each
        # insert their own row, leaving duplicate same-day entries in Today.
        # The lock is transaction-scoped, so it is held until the caller's
        # commit or rollback and only serializes writers for one due date.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
            ('trosa:merge-task', f'{account_id}:{due_on}'),
        )
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
                   ?::jsonb, coalesce(trosa.compat_time(?), now()), now())''',
        (task_id, account_id, title, content, reason, due_on, task_type,
         str(source_interaction_id or ''), json.dumps({'customer_id': int(customer_id)}), now),
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
    resolution_reason: str = '', resolution_source: str = 'human', resolved_by: str = '',
) -> None:
    """Resolve an Inbox item through the canonical Inbox fact.

    ``resolution_source`` separates a human decision from an automatic close.
    Auto-closing never proves a business action happened; it only states that
    the question no longer needs a human.
    """
    if not postgres_mode():
        conn.execute(
            '''UPDATE inbox_items SET status='resolved', resolved_at=?, resolution_reason=?, resolution_note=?,
                   resolution_source=?, resolved_by=?
                 WHERE id=? AND status='open' ''',
            (resolved_at, resolution_reason, resolution_note, resolution_source, resolved_by, inbox_item_id),
        )
        return
    changed = conn.execute(
        '''UPDATE trosa.inbox_items item
              SET status='resolved', resolved_at=trosa.compat_time(?), resolution_reason=?, resolution_note=?,
                  resolution_source=?, resolved_by=?
             FROM trosa.legacy_row_refs ref
            WHERE ref.organization_id=trosa.compat_org_id()
              AND ref.legacy_user_id=trosa.compat_current_user()
              AND ref.table_name='inbox_items' AND ref.legacy_id=?
              AND item.id=ref.target_id AND item.status='open' ''',
        (resolved_at, resolution_reason, resolution_note, resolution_source, resolved_by, inbox_item_id),
    )
    if not changed.rowcount:
        raise ValueError('inbox item is not visible or already resolved')


def set_inbox_status(conn: Any, *, inbox_item_id: int, status: str, changed_at: str,
                     resolution_source: str = '', resolved_by: str = '', resolution_note: str = '') -> None:
    """Set a canonical Inbox lifecycle status while preserving its content."""
    if not postgres_mode():
        conn.execute('''UPDATE inbox_items SET status=?, resolved_at=?,
                              resolution_source=CASE WHEN ?<>'' THEN ? ELSE resolution_source END,
                              resolved_by=CASE WHEN ?<>'' THEN ? ELSE resolved_by END,
                              resolution_note=CASE WHEN ?<>'' THEN ? ELSE resolution_note END
                         WHERE id=?''',
                     (status, changed_at, resolution_source, resolution_source,
                      resolved_by, resolved_by, resolution_note, resolution_note, inbox_item_id))
        return
    changed = conn.execute(
        '''UPDATE trosa.inbox_items item SET status=?, resolved_at=trosa.compat_time(?),
                  resolution_source=CASE WHEN ?<>'' THEN ? ELSE item.resolution_source END,
                  resolved_by=CASE WHEN ?<>'' THEN ? ELSE item.resolved_by END,
                  resolution_note=CASE WHEN ?<>'' THEN ? ELSE item.resolution_note END
             FROM trosa.legacy_row_refs ref
            WHERE ref.organization_id=trosa.compat_org_id() AND ref.legacy_user_id=trosa.compat_current_user()
              AND ref.table_name='inbox_items' AND ref.legacy_id=? AND item.id=ref.target_id''',
        (status, changed_at, resolution_source, resolution_source,
         resolved_by, resolved_by, resolution_note, resolution_note, inbox_item_id),
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
    question_kind: str = '', question_key: str = '', source_type: str = '',
    resolution_source: str = '', resolved_by: str = '', evidence: str = '',
) -> int:
    """Create or refresh a canonical Inbox item and return its stable API id.

    ``question_kind``/``question_key`` describe the human question this item
    raises; ``item_type`` stays the technical evidence type.  Automated writers
    pass the metadata so the Inbox never has to re-derive it from raw payloads.

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
                       resolved_at=?, resolution_reason=?, resolution_note=?,
                       question_kind=CASE WHEN ?<>'' THEN ? ELSE question_kind END,
                       question_key=CASE WHEN ?<>'' THEN ? ELSE question_key END,
                       source_type=CASE WHEN ?<>'' THEN ? ELSE source_type END,
                       evidence=CASE WHEN ?<>'' THEN ? ELSE evidence END
                     WHERE id=?''',
                (customer_id, item_type, title, content, status, resolved_at, resolution_reason,
                 resolution_note, question_kind, question_kind, question_key, question_key,
                 source_type, source_type, evidence, evidence, existing['id']),
            )
            return int(existing['id'])
        cursor = conn.execute(
            '''INSERT INTO inbox_items
               (item_type, customer_id, title, content, dedupe_key, status, created_at,
                resolved_at, resolution_reason, resolution_note,
                question_kind, question_key, source_type, resolution_source, resolved_by, evidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (item_type, customer_id, title, content, dedupe_key, status, created_at,
             resolved_at, resolution_reason, resolution_note,
             question_kind, question_key, source_type, resolution_source, resolved_by,
             evidence or '[]'),
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
    def _find_existing():
        """Resolve a dedupe key to its single Inbox fact.

        Callers and legacy rows may hold the functional transport key while
        canonical storage namespaces it as ``compat:<user>:<raw>``.  Match the
        payload key, the raw column and the canonical column so both the first
        lookup and the post-conflict recovery reach the same fact.
        """
        return conn.execute(
            '''SELECT ref.legacy_id, item.id FROM trosa.inbox_items item
                 JOIN trosa.legacy_row_refs ref ON ref.target_id=item.id
                WHERE ref.organization_id=trosa.compat_org_id()
                  AND ref.legacy_user_id=trosa.compat_current_user()
                  AND ref.table_name='inbox_items'
                  AND (item.legacy_payload->>'compat_dedupe_key'=?
                       OR item.dedupe_key=?
                       OR item.dedupe_key='compat:' || trosa.compat_current_user() || ':' || ?)
                ORDER BY item.created_at, ref.legacy_id LIMIT 1''',
            (dedupe_key, dedupe_key, dedupe_key),
        ).fetchone()

    existing = _find_existing() if dedupe_key else None
    if existing:
        conn.execute(
            '''UPDATE trosa.inbox_items SET account_id=?, item_type=?, title=?, content=?, status=?,
                   resolved_at=trosa.compat_time(?), resolution_reason=?, resolution_note=?,
                   question_kind=CASE WHEN ?<>'' THEN ? ELSE question_kind END,
                   question_key=CASE WHEN ?<>'' THEN ? ELSE question_key END,
                   source_type=CASE WHEN ?<>'' THEN ? ELSE source_type END,
                   evidence=COALESCE(NULLIF(?, '')::jsonb, evidence)
                 WHERE id=?''',
            (account_id, item_type, title, content, status, resolved_at, resolution_reason,
             resolution_note, question_kind, question_kind, question_key, question_key,
             source_type, source_type, evidence, existing['id']),
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
    inserted = conn.execute(
        '''INSERT INTO trosa.inbox_items
           (id, account_id, item_type, title, content, dedupe_key, status, created_at,
            resolved_at, resolution_reason, resolution_note,
            question_kind, question_key, source_type, resolution_source, resolved_by, evidence,
            legacy_payload)
           VALUES (?, ?, ?, ?, ?, CASE WHEN ?='' THEN '' ELSE 'compat:' || trosa.compat_current_user() || ':' || ? END, ?, coalesce(trosa.compat_time(?), now()),
                   trosa.compat_time(?), ?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?::jsonb)
           ON CONFLICT (dedupe_key) WHERE dedupe_key <> '' DO NOTHING''',
        (target_id, account_id, item_type, title, content, dedupe_key, dedupe_key, status,
         created_at, resolved_at, resolution_reason, resolution_note,
         question_kind, question_key, source_type, resolution_source, resolved_by,
         evidence or '[]', payload),
    )
    if dedupe_key and not inserted.rowcount:
        # A concurrent writer won the dedupe race: fall back to its row
        # instead of failing the whole business action with a 500.
        existing = _find_existing()
        if existing:
            conn.execute(
                '''UPDATE trosa.inbox_items SET account_id=?, item_type=?, title=?, content=?, status=?,
                       resolved_at=trosa.compat_time(?), resolution_reason=?, resolution_note=?,
                       question_kind=CASE WHEN ?<>'' THEN ? ELSE question_kind END,
                       question_key=CASE WHEN ?<>'' THEN ? ELSE question_key END,
                       source_type=CASE WHEN ?<>'' THEN ? ELSE source_type END,
                       evidence=COALESCE(NULLIF(?, '')::jsonb, evidence)
                     WHERE id=?''',
                (account_id, item_type, title, content, status, resolved_at, resolution_reason,
                 resolution_note, question_kind, question_kind, question_key, question_key,
                 source_type, source_type, evidence, existing['id']),
            )
            return int(existing['legacy_id'])
        # Never fabricate a legacy ref that points at a row we did not insert.
        raise ValueError('inbox dedupe conflict could not be resolved')
    conn.execute(
        '''INSERT INTO trosa.legacy_row_refs
           (organization_id, legacy_user_id, table_name, legacy_id, target_id)
           VALUES (trosa.compat_org_id(), trosa.compat_current_user(), 'inbox_items', ?, ?)
           ON CONFLICT (organization_id, legacy_user_id, table_name, legacy_id) DO NOTHING''',
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
    """Soft-delete or restore the current user's Customer projection."""
    if not postgres_mode():
        conn.execute(
            'UPDATE customers SET is_deleted=?, deleted_at=?, updated_at=? WHERE id=?',
            (1 if deleted else 0, changed_at if deleted else '', changed_at, customer_id),
        )
        return
    # An account can have more than one legacy reference; archive belongs to
    # the caller's Customer record, never to the shared account.
    changed = conn.execute(
        '''UPDATE trosa.account_legacy_refs
              SET legacy_payload=coalesce(legacy_payload, '{}'::jsonb)
                   || jsonb_build_object('is_deleted', ?::text, 'deleted_at', ?::text)
            WHERE organization_id=trosa.compat_org_id()
              AND legacy_user_id=trosa.compat_current_user()
              AND legacy_customer_id=?''',
        ('1' if deleted else '0', changed_at if deleted else '', customer_id),
    )
    if not changed.rowcount:
        raise ValueError('customer is not visible to the current user')


def update_customer_priority(conn: Any, *, customer_id: int, action: str, changed_at: str = '') -> None:
    """Apply a pin action to the current user's Customer projection."""
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
        '''SELECT id, pinned_order FROM trosa.customer_records
            WHERE id=? AND deleted_at IS NULL''', (customer_id,),
    ).fetchone()
    if not customer:
        raise ValueError('customer is not visible to the current user')

    def _sync_pin_payload(target_customer_id: int, *, is_pinned: bool, pinned_order: int, pinned_at: str) -> None:
        conn.execute(
            '''UPDATE trosa.account_legacy_refs
                  SET legacy_payload=coalesce(legacy_payload, '{}'::jsonb)
                       || jsonb_build_object('is_pinned', ?::text, 'pinned_order', ?::text,
                                             'pinned_at', ?::text)
                WHERE organization_id=trosa.compat_org_id()
                  AND legacy_user_id=trosa.compat_current_user()
                  AND legacy_customer_id=?''',
            ('1' if is_pinned else '0', str(pinned_order), pinned_at, target_customer_id),
        )

    if action == 'pin':
        next_order = conn.execute(
            '''SELECT coalesce(max(pinned_order),0)+1 FROM trosa.customer_records
                WHERE is_pinned AND deleted_at IS NULL''',
        ).fetchone()[0]
        _sync_pin_payload(customer_id, is_pinned=True, pinned_order=int(next_order or 0),
                          pinned_at=changed_at or '')
    elif action == 'unpin':
        _sync_pin_payload(customer_id, is_pinned=False, pinned_order=0, pinned_at='')
    else:
        operator, ordering = ('<', 'DESC') if action == 'up' else ('>', 'ASC')
        neighbor = conn.execute(
            f'''SELECT id, pinned_order FROM trosa.customer_records
                WHERE is_pinned AND deleted_at IS NULL AND pinned_order {operator} ?
                ORDER BY pinned_order {ordering} LIMIT 1''',
            (customer['pinned_order'],),
        ).fetchone()
        if neighbor:
            _sync_pin_payload(customer_id, is_pinned=True,
                              pinned_order=int(neighbor['pinned_order'] or 0), pinned_at='')
            _sync_pin_payload(int(neighbor['id']), is_pinned=True,
                              pinned_order=int(customer['pinned_order'] or 0), pinned_at='')


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
    # The ref supplies the visible name.  Do not update a deduplicated Person
    # or ContactMethod in place: both can be referenced by another user.
    email = values.get('email', '')
    method_id = None
    if email:
        existing_method = conn.execute(
            '''SELECT id FROM core.contact_methods
                WHERE organization_id=trosa.compat_org_id() AND kind='email'
                  AND normalized_value=lower(?) LIMIT 1''', (email,),
        ).fetchone()
        method_id = existing_method['id'] if existing_method else None
    if email and not method_id:
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
        '''UPDATE trosa.contact_legacy_refs SET name=?, title=?, phone=?, whatsapp=?, linkedin=?,
               preferred_channel=?, contact_type=?, is_primary=?, notes=?, contact_method_id=?, updated_at=now()
             WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=trosa.compat_current_user()
               AND legacy_contact_id=?''',
        (values.get('name', ''), values.get('title', ''), values.get('phone', ''), values.get('whatsapp', ''),
         values.get('linkedin', ''), values.get('preferred_channel', ''), values.get('contact_type', 'person'),
         bool(values.get('is_primary')), values.get('notes', ''), method_id, contact_id),
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
    conn.execute('''INSERT INTO trosa.outreach_messages (id,account_id,subject,body,sent_at,reply_status,created_at,legacy_payload)
                    VALUES (?,?,?,?,trosa.compat_time(?),?,coalesce(trosa.compat_time(?),now()),?::jsonb)''',
                 (message_id, account['account_id'], subject, content, sent_on, reply_status, created_at,
                  json.dumps({'customer_id': int(customer_id)})))
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
    """Set the current user's Customer priority level."""
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
        f'''UPDATE trosa.account_legacy_refs
               SET legacy_payload=coalesce(legacy_payload, '{{}}'::jsonb)
                    || jsonb_build_object('level', ?::text)
             WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=trosa.compat_current_user()
               AND legacy_customer_id IN ({placeholders})''',
        [level, *ids],
    ).rowcount or 0)


def update_customer(conn: Any, *, customer_id: int, values: dict[str, Any]) -> None:
    """Update the current user's Customer record without changing a shared Account."""
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
    if any(key in values for key in ('business_stage', 'business_role', 'customer_judgment')):
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
    # Only persist the fields the caller actually supplied.  ``customer_records``
    # treats a present payload key as authoritative, so defaulting an absent key
    # to '' silently clears canonical data (external_source/external_id, status,
    # type, last_interaction_on) on any partial update.
    payload_fields = (
        ('name', ('name',)), ('company', ('company',)), ('country', ('country',)),
        ('level', ('level',)), ('type', ('customer_type', 'type')),
        ('website', ('website',)), ('profile', ('profile',)), ('field', ('field',)),
        ('industry', ('industry',)), ('company_size', ('company_size',)),
        ('annual_revenue', ('annual_revenue',)), ('tags', ('tags',)),
        ('status', ('status',)), ('notes', ('notes',)), ('system_notes', ('system_notes',)),
        ('import_source', ('import_source',)), ('external_source', ('external_source',)),
        ('external_id', ('external_id',)), ('last_contact', ('last_contact',)),
        ('next_follow_up', ('next_follow_up',)),
    )
    customer_payload: dict[str, Any] = {}
    for target, sources in payload_fields:
        for source in sources:
            if source in values:
                customer_payload[target] = values.get(source) or ''
                break
    if 'manual_next_follow' in values:
        customer_payload['manual_next_follow'] = bool(values.get('manual_next_follow'))
    elif 'manual_next_task' in values:
        customer_payload['manual_next_follow'] = bool(values.get('manual_next_task'))
    conn.execute(
        '''UPDATE trosa.account_legacy_refs
              SET legacy_payload=(coalesce(legacy_payload, '{}'::jsonb)
                                  - ARRAY['business_stage', 'business_role', 'customer_judgment'])
                                 || ?::jsonb
            WHERE organization_id=trosa.compat_org_id()
              AND legacy_user_id=trosa.compat_current_user()
              AND legacy_customer_id=?''',
        (json.dumps(customer_payload), customer_id),
    )


def create_customer(conn: Any, *, values: dict[str, Any]) -> int:
    """Create the complete canonical Customer aggregate and its API id."""
    if not postgres_mode():
        raise ValueError('SQLite callers retain their local aggregate adapter')
    customer_id = conn.execute(
        "SELECT trosa.compat_next_id('customers', trosa.compat_current_user())",
    ).fetchone()[0]
    # Fresh canonical identity per created customer.  A per-user customer id
    # can be reused after an explicit transfer, so the identity must not depend
    # on it.
    company_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    owner_user_id = conn.execute(
        '''SELECT id FROM identity.users
            WHERE organization_id=trosa.compat_org_id()
              AND legacy_user_id=trosa.compat_current_user()''',
    ).fetchone()[0]
    conn.execute(
        '''INSERT INTO core.companies
           (id, organization_id, canonical_name, normalized_name, website, country_code)
           VALUES (?, trosa.compat_org_id(), ?, lower(?), ?, ?)''',
        (company_id, values.get('company', ''), values.get('company', ''), values.get('website', ''), values.get('country', '')),
    )
    conn.execute(
        '''INSERT INTO trosa.accounts
           (id, organization_id, company_id, owner_user_id, display_name, priority_level, profile, field, industry,
            company_size, annual_revenue, tags, last_contact_at, next_follow_up_at, legacy_payload)
           VALUES (?, trosa.compat_org_id(), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, trosa.compat_time(?),
                   trosa.compat_time(?), '{}'::jsonb)''',
        (account_id, company_id, owner_user_id, values.get('name', ''), values.get('level', ''), values.get('profile', ''),
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


def transfer_customer(conn: Any, *, customer_id: int, to_user: str) -> int:
    """Move an active Customer (account and history) to another owner.

    The database enforces a single active owner per account, so transfer is an
    explicit operation rather than a second shared owner.  A target that already
    owns a customer for the same shared company is reported instead of merged.
    """
    if not postgres_mode():
        raise ValueError('customer transfer is only available in PostgreSQL mode')
    row = conn.execute(
        'SELECT trosa.transfer_customer_account(?, ?) AS customer_id',
        (int(customer_id), str(to_user)),
    ).fetchone()
    return int(row['customer_id'])


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
    # The status predicate makes the reschedule-vs-complete race atomic: a
    # task completed concurrently is reported as gone instead of being
    # silently resurrected as an open task.
    changed = conn.execute(
        f'''UPDATE trosa.tasks task SET {assignments}
              FROM trosa.legacy_row_refs ref
             WHERE ref.organization_id=trosa.compat_org_id()
               AND ref.legacy_user_id=trosa.compat_current_user()
               AND ref.table_name='reminders' AND ref.legacy_id=?
               AND task.id=ref.target_id AND task.status='open' ''',
        values,
    )
    if not changed.rowcount:
        raise ValueError('task is not visible to the current user')


def customer_interactions(
    conn: Any, customer_id: int, *, limit: int | None = None, offset: int = 0, customer_ids=None,
) -> list[dict]:
    """Return a single product-level timeline across communication and email.

    ``kind`` describes what happened, while ``source`` describes where the
    durable fact arrived.  Upper layers do not branch on historical table
    names.  An outbound delivery remains an interaction but does not by itself
    establish a customer relationship; that rule lives in ``customer_facts``.
    """
    if postgres_mode():
        ids = _ids(customer_ids) if customer_ids is not None else [customer_id]
        if not ids:
            return []
        marks = ','.join('?' for _ in ids)
        query = f'''SELECT id, customer_id, kind, occurred_on, activity_type,
                          direction, content, result, next_plan, source,
                          is_reported, delivery_status, reply_date, created_at
                     FROM trosa.customer_interactions
                    WHERE customer_id IN ({marks})
                    ORDER BY occurred_on DESC, created_at DESC, id DESC'''
        params: list[Any] = ids
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
        interactions_by_customer = {customer_id: [] for customer_id in ids}
        tasks_by_customer = {customer_id: [] for customer_id in ids}
        for item in customer_interactions(conn, None, customer_ids=ids):
            interactions_by_customer[item['customer_id']].append(item)
        for item in customer_tasks(conn, None, customer_ids=ids):
            tasks_by_customer[item['customer_id']].append(item)
        for customer_id in ids:
            fact = facts[customer_id]
            for item in interactions_by_customer[customer_id]:
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
            tasks = tasks_by_customer[customer_id]
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
