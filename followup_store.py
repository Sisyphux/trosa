"""Storage for follow-up suggestions (``trosa.followup_suggestions``).

A suggestion is a private note the daily scan leaves for one member: "this
customer has been quiet since <date>".  It is not a task and never becomes
overdue; unconfirmed rows expire.  Works in both storage modes (PostgreSQL in
production, the SQLite mirror in ``db.py`` for tests), like ``inbox_dialogue``.

Every function takes an open ``conn`` and never commits; the caller owns the
transaction.  Rows are always scoped to the current member.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone

import db

ORG_ID = '859a998d-1b48-589b-8035-34dc65c01440'
STATUSES = ('new', 'handled', 'dismissed', 'snoozed', 'expired', 'voided')
# A suggestion nobody acted on stops being shown after this many days.
EXPIRE_AFTER_DAYS = 3
COLUMNS = ('id', 'account_id', 'suggestion_date', 'rule', 'certainty', 'evidence_event_id',
           'priority', 'reason', 'facts', 'status', 'snooze_until', 'feedback_reason',
           'handled_at', 'dismissed_at', 'snoozed_at', 'expired_at', 'voided_at',
           'dedupe_key', 'created_at', 'updated_at')


def is_postgres():
    return db.postgres_mode()


def scope_user():
    return db.get_current_user() or 'hamid'


def _table():
    return 'trosa.followup_suggestions' if is_postgres() else 'followup_suggestions'


def _scope():
    if is_postgres():
        return ('organization_id=trosa.compat_org_id() '
                'AND legacy_user_id=trosa.compat_current_user()', [])
    return 'organization_id=? AND legacy_user_id=?', [ORG_ID, scope_user()]


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return str(value)


def _row(raw):
    if raw is None:
        return None
    data = dict(raw)
    out = {}
    for key in COLUMNS:
        value = data.get(key)
        if key in ('id', 'account_id', 'evidence_event_id') and value is not None:
            value = str(value)
        elif key in ('suggestion_date', 'snooze_until') and value is not None:
            value = value.isoformat() if isinstance(value, date) and not isinstance(value, datetime) else str(value)[:10]
        elif key.endswith('_at'):
            value = _iso(value)
        elif key == 'facts':
            if isinstance(value, (str, bytes)):
                try:
                    value = json.loads(value)
                except ValueError:
                    value = {}
            value = value or {}
        out[key] = value
    return out


def _select(where='', params=(), order='', limit=None):
    scope, scope_params = _scope()
    sql = f'SELECT {", ".join(COLUMNS)} FROM {_table()} WHERE {scope}'
    if where:
        sql += ' AND ' + where
    if order:
        sql += ' ORDER BY ' + order
    if limit:
        sql += f' LIMIT {int(limit)}'
    return sql, scope_params + list(params)


def get(conn, suggestion_id):
    sql, params = _select('id=?', [str(suggestion_id)])
    return _row(conn.execute(sql, params).fetchone())


def list_by_status(conn, status, limit=500):
    sql, params = _select('status=?', [status], 'priority, suggestion_date DESC, created_at', limit)
    return [_row(r) for r in conn.execute(sql, params).fetchall()]


def list_visible(conn, today, limit=50):
    """Rows to show today: new ones, plus snoozed ones whose day has come."""
    today = _day(today)
    sql, params = _select(
        "(status='new' OR (status='snoozed' AND snooze_until<=?))", [today],
        'priority, suggestion_date DESC, created_at', limit)
    return [_row(r) for r in conn.execute(sql, params).fetchall()]


def _day(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def muted_evidence(conn):
    """``{(account_id, evidence_event_id)}`` the member said "no need" to."""
    sql, params = _select("status='dismissed' AND evidence_event_id IS NOT NULL")
    return {(str(r['account_id']), str(r['evidence_event_id']))
            for r in map(_row, conn.execute(sql, params).fetchall())}


def recent_dismissals(conn, since):
    """``{account_id: [dates]}`` of suggestions turned down or left unanswered since ``since``.

    Nobody has to say "no need": a suggestion that simply expired counts as
    ignored, and two ignores in a row quiet the customer (for "ignored twice").
    """
    stamp = _iso(since) if isinstance(since, datetime) else _day(since)
    sql, params = _select(
        "(status='dismissed' AND dismissed_at>=?) OR (status='expired' AND expired_at>=?)",
        [stamp, stamp])
    out = {}
    for r in map(_row, conn.execute(sql, params).fetchall()):
        out.setdefault(r['account_id'], []).append((r['dismissed_at'] or r['expired_at'] or '')[:10])
    return out


def upsert_candidates(conn, candidates, today):
    """Write today's candidates.  Same ``dedupe_key`` never adds a row.

    A row still ``new`` has its reason/facts/priority refreshed; a row the
    member already answered (handled / dismissed / snoozed) is left alone.
    Returns ``{'inserted': n, 'refreshed': n, 'kept': n}``.
    """
    today = _day(today)
    now = _now()
    counts = {'inserted': 0, 'refreshed': 0, 'kept': 0}
    scope, scope_params = _scope()
    for c in candidates:
        key = c.dedupe_key
        existing = conn.execute(
            f'SELECT id, status FROM {_table()} WHERE {scope} AND dedupe_key=?',
            scope_params + [key]).fetchone()
        facts = json.dumps(dict(c.facts), ensure_ascii=False, default=str)
        evidence = None if c.evidence_event_id is None else str(c.evidence_event_id)
        if existing is None:
            new_id = str(uuid.uuid4())
            if is_postgres():
                conn.execute(
                    f'INSERT INTO {_table()} (id, account_id, suggestion_date, rule, certainty, '
                    'evidence_event_id, priority, reason, facts, dedupe_key, created_at, updated_at) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (new_id, str(c.account_id), today, c.rule, c.certainty, evidence,
                     int(c.priority), c.reason, facts, key, now, now))
            else:
                conn.execute(
                    f'INSERT INTO {_table()} (id, organization_id, legacy_user_id, account_id, '
                    'suggestion_date, rule, certainty, evidence_event_id, priority, reason, facts, '
                    'dedupe_key, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (new_id, ORG_ID, scope_user(), str(c.account_id), today, c.rule, c.certainty,
                     evidence, int(c.priority), c.reason, facts, key, now, now))
            counts['inserted'] += 1
        elif dict(existing)['status'] == 'new':
            conn.execute(
                f'UPDATE {_table()} SET reason=?, facts=?, priority=?, certainty=?, updated_at=? '
                f'WHERE id=?',
                (c.reason, facts, int(c.priority), c.certainty, now, str(dict(existing)['id'])))
            counts['refreshed'] += 1
        else:
            counts['kept'] += 1
    return counts


_STAMP = {'handled': 'handled_at', 'dismissed': 'dismissed_at', 'snoozed': 'snoozed_at',
          'expired': 'expired_at', 'voided': 'voided_at'}


def transition(conn, suggestion_id, status, *, snooze_until=None, feedback_reason=None):
    """Move one of the member's rows to ``status``.  Returns the row, or None if absent."""
    if status not in STATUSES or status == 'new':
        raise ValueError(f'bad status: {status}')
    now = _now()
    sets = ['status=?', 'updated_at=?', f'{_STAMP[status]}=?']
    params = [status, now, now]
    if status == 'snoozed':
        sets.append('snooze_until=?')
        params.append(_day(snooze_until))
    if feedback_reason is not None:
        sets.append('feedback_reason=?')
        params.append(str(feedback_reason)[:200])
    scope, scope_params = _scope()
    conn.execute(f'UPDATE {_table()} SET {", ".join(sets)} WHERE {scope} AND id=?',
                 params + scope_params + [str(suggestion_id)])
    return get(conn, suggestion_id)


def void_unsupported(conn, supported_keys, today):
    """Rows waiting to be shown whose rule no longer holds become ``voided``.

    Covers ``new`` rows and snoozed rows whose day has come; a snoozed row still
    asleep is left alone (the scan deliberately skips its evidence).
    """
    keys = set(supported_keys)
    today = _day(today)
    n = 0
    for row in list_by_status(conn, 'new') + list_by_status(conn, 'snoozed'):
        if row['status'] == 'snoozed' and (row['snooze_until'] or '') > today:
            continue
        if _dedupe(conn, row['id']) not in keys:
            transition(conn, row['id'], 'voided')
            n += 1
    return n


def _dedupe(conn, suggestion_id):
    scope, scope_params = _scope()
    r = conn.execute(f'SELECT dedupe_key FROM {_table()} WHERE {scope} AND id=?',
                     scope_params + [str(suggestion_id)]).fetchone()
    return dict(r)['dedupe_key'] if r else None


def expire_old(conn, today, days=EXPIRE_AFTER_DAYS):
    """``new`` rows older than ``days`` days become ``expired`` (never shown as overdue)."""
    cutoff = (date.fromisoformat(_day(today)) - timedelta(days=days)).isoformat()
    sql, params = _select("status='new' AND suggestion_date<=?", [cutoff])
    rows = [_row(r) for r in conn.execute(sql, params).fetchall()]
    for row in rows:
        transition(conn, row['id'], 'expired')
    return len(rows)


def stale_new_count(conn, today, days=EXPIRE_AFTER_DAYS):
    """Self-check: ``new`` rows older than their life.  Must be 0 after the daily job."""
    cutoff = (date.fromisoformat(_day(today)) - timedelta(days=days)).isoformat()
    scope, scope_params = _scope()
    r = conn.execute(f"SELECT count(*) AS n FROM {_table()} WHERE {scope} AND status='new' "
                     f'AND suggestion_date<?', scope_params + [cutoff]).fetchone()
    return int(dict(r)['n'])


EXCERPT_CHARS = 140


def _excerpt(*parts):
    text = ' '.join(' '.join(str(p or '').split()) for p in parts if p).strip()
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS].rstrip() + '…'


def _in_clause(values):
    return ', '.join(['?'] * len(values))


def describe(conn, rows):
    """Add what the page shows: the customer's name and number, and where it last left off.

    ``last_said`` is a short excerpt of the timeline record the reason hangs on,
    so the member can recall the conversation without opening anything.
    """
    rows = list(rows)
    if not rows:
        return []
    accounts = sorted({r['account_id'] for r in rows})
    events = sorted({r['evidence_event_id'] for r in rows if r['evidence_event_id']})
    customers, said = {}, {}
    if is_postgres():
        for r in conn.execute(
                f"SELECT id, account_id, COALESCE(NULLIF(company, ''), name) AS name FROM trosa.customer_records "
                f'WHERE deleted_at IS NULL AND account_id IN ({_in_clause(accounts)})', accounts):
            r = dict(r)
            customers[str(r['account_id'])] = (r['id'], r['name'])
        if events:
            for r in conn.execute(
                    f'SELECT id, content, result, occurred_at FROM trosa.timeline_events '
                    f'WHERE id IN ({_in_clause(events)})', events):
                r = dict(r)
                said[str(r['id'])] = (_iso(r['occurred_at'])[:10], _excerpt(r['content'], r['result']))
    else:
        for r in conn.execute(
                f"SELECT id, COALESCE(NULLIF(company, ''), name) AS name FROM customers WHERE (is_deleted=0 OR is_deleted IS NULL) "
                f'AND id IN ({_in_clause(accounts)})', accounts):
            r = dict(r)
            customers[str(r['id'])] = (r['id'], r['name'])
        if events:
            for r in conn.execute(
                    f'SELECT id, content, result, follow_date FROM follow_up_logs '
                    f'WHERE id IN ({_in_clause(events)})', events):
                r = dict(r)
                said[str(r['id'])] = (str(r['follow_date'])[:10], _excerpt(r['content'], r['result']))
    out = []
    for row in rows:
        customer = customers.get(row['account_id'])
        if customer is None:
            continue  # customer archived or removed since the scan
        last = said.get(row['evidence_event_id'] or '')
        out.append({
            'id': row['id'], 'customer_id': customer[0], 'customer_name': customer[1],
            'reason': row['reason'], 'suggestion_date': row['suggestion_date'],
            'snooze_until': row['snooze_until'],
            'last_said': {'date': last[0], 'text': last[1]} if last else None,
        })
    return out


def _superseded(conn, row):
    """True when the customer has a record newer than the one the suggestion hangs on.

    The member talked to the customer (or noted something) after the scan, so the
    suggestion is already answered; it disappears without anyone clicking.
    """
    account, evidence = row['account_id'], row['evidence_event_id']
    if is_postgres():
        live = ("lower(coalesce(e.payload->>'is_deleted', '0')) NOT IN ('1', 'true')")
        if evidence:
            sql = ('SELECT 1 FROM trosa.timeline_events e WHERE e.account_id=? AND ' + live +
                   ' AND e.occurred_at > (SELECT occurred_at FROM trosa.timeline_events WHERE id=?) LIMIT 1')
            return conn.execute(sql, (account, evidence)).fetchone() is not None
        sql = ('SELECT 1 FROM trosa.timeline_events e WHERE e.account_id=? AND ' + live +
               ' AND e.occurred_at > ? LIMIT 1')
        return conn.execute(sql, (account, row['created_at'])).fetchone() is not None
    if evidence:
        sql = ('SELECT 1 FROM follow_up_logs WHERE customer_id=? AND (is_deleted=0 OR is_deleted IS NULL) '
               'AND id > ? LIMIT 1')
        return conn.execute(sql, (account, evidence)).fetchone() is not None
    return False


def open_hint(conn, account_id, today):
    """The one line the customer page shows for this customer, or ``None``.

    Only a suggestion that is still waiting (new, or snoozed with its day come)
    and not yet answered by a newer record counts.
    """
    today = _day(today)
    sql, params = _select(
        "account_id=? AND (status='new' OR (status='snoozed' AND snooze_until<=?))",
        [str(account_id), today], 'priority, suggestion_date DESC, created_at', 5)
    for row in map(_row, conn.execute(sql, params).fetchall()):
        if not _superseded(conn, row):
            return {'reason': row['reason'], 'date': row['suggestion_date']}
    return None


def first_suggestion_date(conn):
    """Day the member first received a suggestion (``None`` before the first run)."""
    scope, params = _scope()
    r = conn.execute(f'SELECT min(suggestion_date) AS d FROM {_table()} WHERE {scope}', params).fetchone()
    value = dict(r)['d'] if r else None
    return date.fromisoformat(_day(value)) if value else None


def count_rule_since(conn, rule, since):
    """How many suggestions of ``rule`` were first created on or after ``since`` (weekly R3 cap)."""
    scope, params = _scope()
    r = conn.execute(f'SELECT count(*) AS n FROM {_table()} WHERE {scope} AND rule=? '
                     f'AND suggestion_date>=?', params + [rule, _day(since)]).fetchone()
    return int(dict(r)['n'])


def settled_evidence(conn, today):
    """Evidence the scan must not propose again.

    Dismissed, handled and expired rows stay quiet for the same evidence (new
    records change the evidence, which is what brings a customer back); a
    snoozed row stays quiet until its day.
    """
    sql, params = _select(
        "evidence_event_id IS NOT NULL AND (status IN ('dismissed', 'handled', 'expired') "
        "OR (status='snoozed' AND snooze_until>?))", [_day(today)])
    return {(str(r['account_id']), str(r['evidence_event_id']))
            for r in map(_row, conn.execute(sql, params).fetchall())}
