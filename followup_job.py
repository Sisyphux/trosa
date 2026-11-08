"""Daily follow-up scan: quietly note which customers may be worth contacting.

For one member: read the timeline, run ``followup_candidates``, write the result
into ``trosa.followup_suggestions`` (same evidence never adds a row), void what
no longer holds, expire what nobody answered, then self-check that nothing
older than its life is still waiting.  The new ones are mentioned once in an
Inbox conversation.  It never creates tasks or sends anything.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import db
import followup_candidates as fc
import followup_store as store
import inbox_dialogue as dialogue

logger = logging.getLogger(__name__)
TZ = ZoneInfo('Asia/Shanghai')
HISTORY_DAYS = 400          # longest rule window (R3: 365 days) plus slack
IGNORE_WINDOW_DAYS = 14

_EVENTS = '''
SELECT e.id, e.account_id, e.event_type, e.direction, e.content, e.result, e.next_plan,
       e.source_module, e.occurred_at, e.payload
  FROM trosa.timeline_events e
  JOIN trosa.accounts a ON a.id = e.account_id
  JOIN identity.users u ON u.id = a.owner_user_id
 WHERE u.legacy_user_id = ?
   AND lower(coalesce(e.payload->>'is_deleted', '0')) NOT IN ('1', 'true')
   AND e.occurred_at >= ?
 ORDER BY e.occurred_at, e.id'''

_ACCOUNTS = '''
SELECT a.id, a.display_name, a.deleted_at, d.notes, d.system_notes, d.import_source
  FROM trosa.accounts a
  JOIN identity.users u ON u.id = a.owner_user_id
  LEFT JOIN trosa.customer_details d ON d.account_id = a.id
 WHERE u.legacy_user_id = ?'''

_MAILS = '''
SELECT m.account_id, m.reply_status, m.subject, m.body, m.reply_content
  FROM trosa.outreach_messages m
  JOIN trosa.accounts a ON a.id = m.account_id
  JOIN identity.users u ON u.id = a.owner_user_id
 WHERE u.legacy_user_id = ?'''

_INBOX = '''
SELECT i.account_id, i.title, i.content
  FROM trosa.inbox_items i
  JOIN trosa.accounts a ON a.id = i.account_id
  JOIN identity.users u ON u.id = a.owner_user_id
 WHERE u.legacy_user_id = ?'''

_OPEN_TASKS = """
SELECT t.account_id, t.task_type AS reminder_type, t.status
  FROM trosa.tasks t
  JOIN trosa.accounts a ON a.id = t.account_id
  JOIN identity.users u ON u.id = a.owner_user_id
 WHERE u.legacy_user_id = ? AND t.status = 'open'"""

# Names (and only names + newest-record day) of the other members' customers,
# so one company is not suggested to two people at once.
_PEERS = '''
SELECT a.display_name AS name, max(e.occurred_at) AS last_at
  FROM trosa.accounts a
  JOIN identity.users u ON u.id = a.owner_user_id
  JOIN trosa.timeline_events e ON e.account_id = a.id
 WHERE u.legacy_user_id <> ? AND a.deleted_at IS NULL AND coalesce(a.display_name, '') <> ''
 GROUP BY a.display_name'''


def _rows(conn, sql, params):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def load_member_data(conn, member, today):
    """Everything the engine needs for ``member``, read through the app connection."""
    since = datetime.combine(today - timedelta(days=HISTORY_DAYS), datetime.min.time(), TZ)
    events = [{**e, 'event_id': str(e['id']), 'account_id': str(e['account_id']), 'owner': member}
              for e in _rows(conn, _EVENTS, (member, since))]
    accounts = [{'account_id': str(a['id']), 'name': a['display_name'], 'deleted_at': a['deleted_at'],
                 'notes': a['notes'], 'system_notes': a['system_notes'],
                 'import_source': a['import_source']} for a in _rows(conn, _ACCOUNTS, (member,))]
    mails, inbox = {}, {}
    for row in _rows(conn, _MAILS, (member,)):
        mails.setdefault(str(row['account_id']), []).append(row)
    for row in _rows(conn, _INBOX, (member,)):
        inbox.setdefault(str(row['account_id']), []).append(row)
    open_tasks = [{'account_id': str(t['account_id']), 'reminder_type': t['reminder_type'],
                   'status': t['status']} for t in _rows(conn, _OPEN_TASKS, (member,))]
    peers = {}
    for row in _rows(conn, _PEERS, (member,)):
        peers[row['name']] = row['last_at'].astimezone(TZ).date()
    return {'events': events, 'accounts': accounts, 'mails': mails, 'inbox': inbox,
            'open_task_account_ids': fc.open_task_account_ids(open_tasks), 'peers': peers}


def run_for_member(conn, member, today, loader=load_member_data, params=None):
    """One member's daily pass.  Does not commit.  Returns a small summary dict."""
    data = loader(conn, member, today)
    first = store.first_suggestion_date(conn)
    week_start = today - timedelta(days=today.weekday())
    result = fc.scan(
        data['accounts'], data['events'], data['open_task_account_ids'], today, params, member,
        muted=store.settled_evidence(conn, today),
        ignored=store.recent_dismissals(conn, today - timedelta(days=IGNORE_WINDOW_DAYS)),
        launch_date=first or today,
        r3_released_this_week=store.count_rule_since(conn, 'R3', week_start),
        mails=data.get('mails'), inbox=data.get('inbox'),
        peer_last_contact=data.get('peers'))
    written = store.upsert_candidates(conn, result.released, today)
    still_true = {c.dedupe_key for c in result.released} | {c.dedupe_key for c in result.held_back}
    voided = store.void_unsupported(conn, still_true, today)
    expired = store.expire_old(conn, today)
    stale = store.stale_new_count(conn, today)
    summary = {'member': member, 'released': len(result.released), **written,
               'voided': voided, 'expired': expired, 'stale_new': stale, 'ok': stale == 0}
    if stale:
        logger.error('跟进建议自检失败 [%s]: 仍有 %s 条超过有效期的「新」建议', member, stale)
    return summary


def morning_note(conn, member, today):
    """Say today's new suggestions once, in the member's Inbox conversation.

    Nothing is posted on a quiet day.  The conversation is per day, so running
    the job twice never says it twice.  Does not commit.
    """
    day = today.isoformat()
    rows = [r for r in store.list_by_status(conn, 'new') if r['suggestion_date'] == day]
    items = store.describe(conn, rows)
    if not items:
        return 0
    lines = ['\u00b7 %s\uff1a%s' % (i['customer_name'], i['reason']) for i in items]
    dialogue.create_thread(
        conn, idempotency_key='followup:%s:%s' % (member, day),
        payload={'subject': 'followup:' + day, 'title': '今天可能该联系的客户',
                 'text': '今天这几位客户可能该联系了：\n' + '\n'.join(lines),
                 # A single customer gets an open-customer button; several would be indistinguishable.
                 'refs': [{'type': 'customer', 'id': str(items[0]['customer_id'])}] if len(items) == 1 else []})
    return len(items)


def run_daily(users, enabled, today=None, loader=load_member_data):
    """Run the pass for every enabled member; one member failing never stops the others."""
    today = today or datetime.now(TZ).date()
    summaries = []
    for member in users:
        if not enabled(member):
            continue
        conn = None
        try:
            db.set_db_user(member)
            conn = db.get_db()
            summary = run_for_member(conn, member, today, loader=loader)
            conn.commit()
            try:
                summary['noted'] = morning_note(conn, member, today)
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception('跟进建议 [%s] 对话提示没有发出', member)
                summary['noted'] = 0
            summaries.append(summary)
            logger.info('跟进建议 [%s]: %s', member, summary)
        except Exception:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.exception('跟进建议 [%s] 生成失败', member)
            summaries.append({'member': member, 'ok': False, 'error': True})
        finally:
            if conn is not None:
                conn.close()
    db.set_db_user(None)
    return summaries
