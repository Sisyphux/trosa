"""Inbox dialogue backend (contract ``inbox-dialogue-contract.md`` v1.1).

This module owns the new, type-independent dialogue window between a human and
sela::

    inbox_threads            one conversation per subject
    inbox_messages           free-text messages in that conversation
    inbox_action_receipts    idempotency + "confirmation consumed once"
    inbox_legacy_route_hits  call counters for the routes being retired

Everything here is deterministic and server-side.  Free text comes in, typed
actions go out; guardrails never depend on a "question type".  The module never
commits or rolls back: an HTTP route opens ``BEGIN IMMEDIATE``, calls one of the
public operations below and commits, or rolls the whole thing back on error.

Works in both storage modes:

* PostgreSQL (production / rehearsal) uses the ``trosa`` schema and the
  ``trosa.compat_*`` scope helpers, exactly like ``_modern_inbox_rows``.
* SQLite (local tests) uses the mirrored tables created by ``db.py``.

No model calls, no customer email, no classification.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime, timezone

import db

# ``trosa.compat_org_id()`` always returns this fixed organisation in this repo.
ORG_ID = '859a998d-1b48-589b-8035-34dc65c01440'
DAILY_NEW_THREAD_CAP = 50
IRREVERSIBLE_ACTIONS = ('stop_contact', 'merge_identity', 'overwrite_email')
# Reversible facts the human gave in a conversation (contract §4.4B).
FACT_KINDS = ('contact_email', 'contact_person', 'contact_phone', 'identity_different', 'note')
MAX_TEXT = 8000
MAX_TITLE = 200
MAX_SUBJECT = 200
MAX_SUMMARY = 2000
MAX_REFS = 50
MAX_ATTACHMENTS = 5
MAX_SUGGESTED = 3
REF_TYPES = ('prospect', 'customer', 'message', 'action_receipt', 'file', 'thread')

# Canonical labels stored in ``inbox_legacy_route_hits.route``.
LEGACY_ROUTE_LABELS = {
    'respond_question': 'POST /api/inbox/questions/<id>/respond',
    'sela_needs_create': 'POST /api/integrations/sela/needs',
    'sela_resume_status': 'POST /api/integrations/sela/needs/<id>/resume-status',
    'sela_resolve_need': 'POST /api/integrations/sela/needs/<id>/resolve',
    'sela_continuations': 'GET /api/integrations/sela/continuations',
}


class DialogueError(Exception):
    """A deterministic dialogue failure carrying an error-envelope code."""

    def __init__(self, code, message, status=400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details

    def body(self):
        error = {'code': self.code, 'message': self.message}
        if self.details is not None:
            error['details'] = self.details
        return {'error': error}


# ---------------------------------------------------------------------------
# Environment / small helpers
# ---------------------------------------------------------------------------

def is_postgres():
    return db.postgres_mode()


def scope_user():
    return db.get_current_user() or 'hamid'


def _t(name):
    return 'trosa.' + name if is_postgres() else name


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _scope(alias=''):
    prefix = (alias + '.') if alias else ''
    if is_postgres():
        return (f'{prefix}organization_id=trosa.compat_org_id() '
                f'AND {prefix}legacy_user_id=trosa.compat_current_user()', [])
    return (f'{prefix}organization_id=? AND {prefix}legacy_user_id=?',
            [ORG_ID, scope_user()])


def _hash(value):
    text = json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), default=str)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _dump(value):
    return json.dumps(value, ensure_ascii=False)


def _load(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode('utf-8')
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return fallback
    return fallback


def _ts_val(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return str(value)


def _utcnow():
    return datetime.now(timezone.utc)


def _parse_ts(text):
    try:
        return datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return _utcnow()


def _deterministic_uuid(seed):
    """Same md5-based uuid convention as ``trosa.compat_uuid`` / ``_canonical_uuid``."""
    return str(uuid.UUID(hashlib.md5(seed.encode('utf-8')).hexdigest()))


def _row_dict(row):
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except Exception:
        return {}


def _scalar(row):
    """First column of a row, tolerant of SQLite rows and PG CompatRow."""
    data = _row_dict(row)
    if not data:
        return None
    return next(iter(data.values()))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _text(value, *, field='text', minimum=1, maximum=MAX_TEXT, required=True):
    if value is None:
        if required:
            raise DialogueError('invalid_request', f'{field} 不能为空')
        return None
    if not isinstance(value, str):
        raise DialogueError('invalid_request', f'{field} 必须是字符串')
    if len(value) < minimum:
        raise DialogueError('invalid_request', f'{field} 长度不足')
    if len(value) > maximum:
        raise DialogueError('invalid_request', f'{field} 过长')
    return value


def _subject(value):
    if value is None:
        return None
    return _text(value, field='subject', maximum=MAX_SUBJECT, required=False)


def _suggested(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_SUGGESTED:
        raise DialogueError('invalid_request', 'suggested_replies 无效')
    out = []
    for item in value:
        if not isinstance(item, str) or not (1 <= len(item) <= 120):
            raise DialogueError('invalid_request', 'suggested_replies 无效')
        out.append(item)
    return out


def _refs(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_REFS:
        raise DialogueError('invalid_request', 'refs 无效')
    out = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {'type', 'id'}:
            raise DialogueError('invalid_request', 'refs 无效')
        kind = item.get('type')
        ident = item.get('id')
        if kind not in REF_TYPES:
            raise DialogueError('invalid_request', 'refs.type 无效')
        if not isinstance(ident, str) or not (1 <= len(ident) <= 200):
            raise DialogueError('invalid_request', 'refs.id 无效')
        out.append({'type': kind, 'id': ident})
    return out


def _attachments(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
        raise DialogueError('invalid_request', 'attachments 无效')
    out = []
    for item in value:
        if not isinstance(item, dict):
            raise DialogueError('invalid_request', 'attachments 无效')
        try:
            file_id = str(uuid.UUID(str(item.get('file_object_id'))))
        except Exception:
            raise DialogueError('invalid_request', 'attachments.file_object_id 无效')
        name = item.get('name')
        if not isinstance(name, str) or not (1 <= len(name) <= 300):
            raise DialogueError('invalid_request', 'attachments.name 无效')
        entry = {'file_object_id': file_id, 'name': name}
        mime = item.get('mime_type')
        if mime is not None:
            if not isinstance(mime, str) or len(mime) > 120:
                raise DialogueError('invalid_request', 'attachments.mime_type 无效')
            entry['mime_type'] = mime
        size = item.get('size_bytes')
        if size is not None:
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise DialogueError('invalid_request', 'attachments.size_bytes 无效')
            entry['size_bytes'] = size
        out.append(entry)
    return out


def _attachment_ids(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
        raise DialogueError('invalid_request', 'attachment_ids 无效')
    out = []
    for item in value:
        try:
            out.append(str(uuid.UUID(str(item))))
        except Exception:
            raise DialogueError('invalid_request', 'attachment_ids 无效')
    return out


def _awaiting(value):
    if value is None:
        return None
    if value not in ('human', 'sela'):
        raise DialogueError('invalid_request', 'awaiting 无效')
    return value


def _seen_revision(value, *, required=False):
    if value is None:
        if required:
            raise DialogueError('invalid_request', '缺少 seen_revision')
        return None
    if isinstance(value, bool):
        raise DialogueError('invalid_request', 'seen_revision 无效')
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise DialogueError('invalid_request', 'seen_revision 无效')
    if number < 0:
        raise DialogueError('invalid_request', 'seen_revision 无效')
    return number


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _msg_dict(row):
    data = _row_dict(row)
    out = {
        'id': str(data['id']),
        'thread_id': str(data['thread_id']),
        'seq': int(data['seq']),
        'role': data['role'],
        'text': data['text'],
    }
    if data.get('actor'):
        out['actor'] = data['actor']
    out['suggested_replies'] = _load(data.get('suggested_replies'), [])
    out['refs'] = _load(data.get('refs'), [])
    out['hints'] = _load(data.get('hints'), None)
    out['attachments'] = _load(data.get('attachments'), [])
    if data.get('awaiting_after'):
        out['awaiting_after'] = data['awaiting_after']
    if data.get('idempotency_key'):
        out['idempotency_key'] = data['idempotency_key']
    out['created_at'] = _ts_val(data.get('created_at'))
    return out


def _thread_dict(conn, row):
    data = _row_dict(row)
    messages = _messages(conn, str(data['id']))
    out = {
        'id': str(data['id']),
        'subject': data.get('subject'),
        'title': data['title'],
        'status': data['status'],
        'awaiting': data['awaiting'],
        'revision': int(data['revision']),
        'opened_at': _ts_val(data.get('opened_at')),
        'updated_at': _ts_val(data.get('updated_at')),
        'closed_at': _ts_val(data.get('closed_at')),
        'closed_by': data.get('closed_by'),
        'closed_summary': data.get('closed_summary'),
        'messages': [_msg_dict(message) for message in messages],
    }
    legacy = _legacy_id_for_thread(conn, str(data['id']))
    if legacy is not None:
        out['legacy_id'] = int(legacy)
    return out


def _summary_dict(conn, row):
    data = _row_dict(row)
    last = _last_message(conn, str(data['id']))
    preview = None
    if last:
        preview = {
            'role': last['role'],
            'text': (last['text'] or '')[:500],
            'created_at': _ts_val(last.get('created_at')),
        }
    return {
        'id': str(data['id']),
        'subject': data.get('subject'),
        'title': data['title'],
        'status': data['status'],
        'awaiting': data['awaiting'],
        'revision': int(data['revision']),
        'opened_at': _ts_val(data.get('opened_at')),
        'updated_at': _ts_val(data.get('updated_at')),
        'closed_at': _ts_val(data.get('closed_at')),
        'closed_by': data.get('closed_by'),
        'closed_summary': data.get('closed_summary'),
        'last_message_preview': preview,
    }


# ---------------------------------------------------------------------------
# Low-level storage
# ---------------------------------------------------------------------------

def _thread_row(conn, thread_id):
    where, params = _scope()
    row = conn.execute(
        f'SELECT * FROM {_t("inbox_threads")} WHERE id=? AND {where}',
        [thread_id] + params,
    ).fetchone()
    return _row_dict(row)


def _lock_thread(conn, thread_id):
    where, params = _scope()
    sql = f'SELECT * FROM {"trosa.inbox_threads" if is_postgres() else "inbox_threads"} WHERE id=? AND {where}'
    if is_postgres():
        sql += ' FOR SHARE'
    row = conn.execute(sql, [thread_id] + params).fetchone()
    return _row_dict(row)


def _is_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _message_row(conn, message_id):
    if not _is_uuid(message_id):
        return None  # a malformed id names no message (never reaches the uuid column)
    row = conn.execute(
        f'SELECT * FROM {_t("inbox_messages")} WHERE id=?', [message_id],
    ).fetchone()
    return _row_dict(row)


def _messages(conn, thread_id):
    rows = conn.execute(
        f'SELECT * FROM {_t("inbox_messages")} WHERE thread_id=? ORDER BY seq ASC',
        [thread_id],
    ).fetchall()
    return [_row_dict(row) for row in rows]


def _last_message(conn, thread_id):
    row = conn.execute(
        f'SELECT * FROM {_t("inbox_messages")} WHERE thread_id=? ORDER BY seq DESC LIMIT 1',
        [thread_id],
    ).fetchone()
    return _row_dict(row)


def _next_seq(conn, thread_id):
    row = conn.execute(
        f'SELECT COALESCE(MAX(seq), 0) FROM {_t("inbox_messages")} WHERE thread_id=?',
        [thread_id],
    ).fetchone()
    return int(_scalar(row)) + 1


def _legacy_id_for_thread(conn, thread_id):
    where, params = _scope()
    row = conn.execute(
        f"SELECT legacy_id FROM {_t('legacy_row_refs')} "
        f"WHERE table_name='inbox_items' AND target_id=? AND {where}",
        [thread_id] + params,
    ).fetchone()
    return int(_scalar(row)) if row else None


def _find_open_thread_by_subject(conn, subject):
    where, params = _scope()
    row = conn.execute(
        f'SELECT * FROM {_t("inbox_threads")} WHERE subject=? AND status=\'open\' AND {where}',
        [subject] + params,
    ).fetchone()
    return _row_dict(row)


def _insert_thread(conn, *, thread_id, subject, title, now, awaiting='human',
                   revision=0, status='open', opened_at=None, updated_at=None,
                   closed_at=None, closed_by=None, closed_summary=None):
    opened_at = opened_at or now
    updated_at = updated_at or now
    if is_postgres():
        conn.execute(
            f'INSERT INTO {_t("inbox_threads")} '
            '(id, subject, title, status, awaiting, revision, opened_at, updated_at, '
            ' closed_at, closed_by, closed_summary) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (thread_id, subject, title, status, awaiting, revision, opened_at,
             updated_at, closed_at, closed_by, closed_summary),
        )
    else:
        conn.execute(
            'INSERT INTO inbox_threads '
            '(id, organization_id, legacy_user_id, subject, title, status, awaiting, '
            ' revision, opened_at, updated_at, closed_at, closed_by, closed_summary) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (thread_id, ORG_ID, scope_user(), subject, title, status, awaiting,
             revision, opened_at, updated_at, closed_at, closed_by, closed_summary),
        )


def _insert_message(conn, *, thread_id, seq, role, text, actor=None,
                    suggested_replies=None, refs=None, hints=None, attachments=None,
                    awaiting_after=None, idempotency_key=None, created_at=None):
    message_id = str(uuid.uuid4())
    created_at = created_at or _now()
    suggested = _dump(suggested_replies or [])
    refs_json = _dump(refs or [])
    attachments_json = _dump(attachments or [])
    hints_json = _dump(hints) if hints is not None else None
    if is_postgres():
        conn.execute(
            f'INSERT INTO {_t("inbox_messages")} '
            '(id, thread_id, seq, role, actor, text, suggested_replies, refs, hints, '
            ' attachments, awaiting_after, idempotency_key, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?::jsonb, ?::jsonb, ?::jsonb, ?::jsonb, ?, ?, ?)',
            (message_id, thread_id, seq, role, actor, text, suggested, refs_json,
             hints_json, attachments_json, awaiting_after, idempotency_key, created_at),
        )
    else:
        conn.execute(
            'INSERT INTO inbox_messages '
            '(id, thread_id, seq, role, actor, text, suggested_replies, refs, hints, '
            ' attachments, awaiting_after, idempotency_key, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (message_id, thread_id, seq, role, actor, text, suggested, refs_json,
             hints_json, attachments_json, awaiting_after, idempotency_key, created_at),
        )
    return message_id


def _touch_thread(conn, thread_id, *, revision, awaiting, updated_at):
    conn.execute(
        f'UPDATE {_t("inbox_threads")} SET revision=?, awaiting=?, updated_at=? WHERE id=?',
        (revision, awaiting, updated_at, thread_id),
    )


def _receipt_replay(conn, operation, key, request_hash):
    if not key:
        return None
    where, params = _scope()
    row = conn.execute(
        f'SELECT request_hash, response FROM {_t("inbox_action_receipts")} '
        f'WHERE operation=? AND idempotency_key=? AND {where}',
        [operation, key] + params,
    ).fetchone()
    if not row:
        return None
    data = _row_dict(row)
    if data.get('request_hash') != request_hash:
        raise DialogueError('idempotency_conflict', '幂等键已用于另一份请求', 409)
    return _load(data.get('response'), {})


def _receipt_write(conn, *, thread_id, operation, key, request_hash, response,
                   message_id=None, consumed_message_id=None):
    if not key:
        return
    receipt_id = str(uuid.uuid4())
    now = _now()
    if is_postgres():
        conn.execute(
            f'INSERT INTO {_t("inbox_action_receipts")} '
            '(id, thread_id, operation, idempotency_key, request_hash, response, '
            ' message_id, consumed_message_id, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?::jsonb, ?, ?, ?)',
            (receipt_id, thread_id, operation, key, request_hash, _dump(response),
             message_id, consumed_message_id, now),
        )
    else:
        conn.execute(
            'INSERT INTO inbox_action_receipts '
            '(id, organization_id, legacy_user_id, thread_id, operation, idempotency_key, '
            ' request_hash, response, message_id, consumed_message_id, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (receipt_id, ORG_ID, scope_user(), thread_id, operation, key, request_hash,
             _dump(response), message_id, consumed_message_id, now),
        )


def _confirmation_consumed(conn, message_id):
    where, params = _scope()
    row = conn.execute(
        f'SELECT 1 FROM {_t("inbox_action_receipts")} '
        f'WHERE consumed_message_id=? AND {where} LIMIT 1',
        [message_id] + params,
    ).fetchone()
    return bool(row)


def _is_consumed_conflict(exc):
    diag = getattr(exc, 'diag', None)
    name = getattr(diag, 'constraint_name', None)
    if name == 'inbox_receipts_consumed_idx':
        return True
    return 'inbox_receipts_consumed_idx' in str(exc)


def _enforce_daily_cap(conn):
    where, params = _scope()
    if is_postgres():
        condition = "opened_at >= date_trunc('day', now())"
    else:
        condition = "date(opened_at) = date('now')"
    row = conn.execute(
        f'SELECT COUNT(*) FROM {_t("inbox_threads")} WHERE {where} AND {condition}',
        params,
    ).fetchone()
    if int(_scalar(row)) >= DAILY_NEW_THREAD_CAP:
        raise DialogueError(
            'rate_limited',
            f'当天新建对话已达上限（{DAILY_NEW_THREAD_CAP}），请追加到已有对话',
            429,
        )


# ---------------------------------------------------------------------------
# Public operations (no commit / rollback here)
# ---------------------------------------------------------------------------

def create_thread(conn, *, payload, idempotency_key=None, actor='sela'):
    subject = _subject(payload.get('subject'))
    title = _text(payload.get('title'), field='title', maximum=MAX_TITLE, required=False)
    if not title:
        title = subject or 'sela 对话'
    text = _text(payload.get('text'), field='text')
    suggested = _suggested(payload.get('suggested_replies'))
    refs = _refs(payload.get('refs'))
    attachments = _attachments(payload.get('attachments'))
    awaiting = _awaiting(payload.get('awaiting')) or 'human'
    request_hash = _hash({
        'op': 'create', 'subject': subject, 'title': title, 'text': text,
        'suggested_replies': suggested, 'refs': refs, 'attachments': attachments,
        'awaiting': awaiting,
    })
    replay = _receipt_replay(conn, 'create', idempotency_key, request_hash)
    if replay is not None:
        return replay
    if subject:
        existing = _find_open_thread_by_subject(conn, subject)
        if existing:
            body = {'success': True, 'created': False,
                    'thread': _thread_dict(conn, existing)}
            _receipt_write(conn, thread_id=str(existing['id']), operation='create',
                           key=idempotency_key, request_hash=request_hash, response=body)
            return body
    _enforce_daily_cap(conn)
    thread_id = str(uuid.uuid4())
    now = _now()
    _insert_thread(conn, thread_id=thread_id, subject=subject, title=title, now=now,
                   awaiting=awaiting, revision=1)
    message_id = _insert_message(
        conn, thread_id=thread_id, seq=1, role='sela', text=text, actor=actor,
        suggested_replies=suggested, refs=refs, attachments=attachments,
        awaiting_after=awaiting, idempotency_key=idempotency_key, created_at=now,
    )
    body = {'success': True, 'created': True,
            'thread': _thread_dict(conn, _thread_row(conn, thread_id))}
    _receipt_write(conn, thread_id=thread_id, operation='create', key=idempotency_key,
                   request_hash=request_hash, response=body, message_id=message_id)
    return body


def append_message(conn, *, thread_id, payload, idempotency_key=None, actor='sela'):
    text = _text(payload.get('text'), field='text')
    suggested = _suggested(payload.get('suggested_replies'))
    refs = _refs(payload.get('refs'))
    attachments = _attachments(payload.get('attachments'))
    awaiting = _awaiting(payload.get('awaiting'))
    seen = _seen_revision(payload.get('seen_revision'))
    request_hash = _hash({
        'op': 'append', 'thread_id': thread_id, 'text': text,
        'suggested_replies': suggested, 'refs': refs, 'attachments': attachments,
        'awaiting': awaiting, 'seen_revision': seen,
    })
    replay = _receipt_replay(conn, 'append', idempotency_key, request_hash)
    if replay is not None:
        return replay
    row = _thread_row(conn, thread_id)
    if not row:
        raise DialogueError('not_found', '对话不存在', 404)
    if row['status'] == 'closed':
        raise DialogueError('thread_closed', '对话已关闭', 409)
    if seen is not None and seen != int(row['revision']):
        raise DialogueError('thread_changed', '对话已被更新', 409, {
            'current_revision': int(row['revision']),
            'current_awaiting': row['awaiting'],
        })
    seq = _next_seq(conn, thread_id)
    awaiting_after = awaiting or 'human'
    now = _now()
    message_id = _insert_message(
        conn, thread_id=thread_id, seq=seq, role='sela', text=text, actor=actor,
        suggested_replies=suggested, refs=refs, attachments=attachments,
        awaiting_after=awaiting_after, idempotency_key=idempotency_key, created_at=now,
    )
    _touch_thread(conn, thread_id, revision=seq, awaiting=awaiting_after, updated_at=now)
    body = {'success': True, 'thread': _thread_dict(conn, _thread_row(conn, thread_id))}
    _receipt_write(conn, thread_id=thread_id, operation='append', key=idempotency_key,
                   request_hash=request_hash, response=body, message_id=message_id)
    return body


def close_thread(conn, *, thread_id, payload, idempotency_key=None, closed_by='sela'):
    summary = _text(payload.get('summary'), field='summary', maximum=MAX_SUMMARY)
    seen = _seen_revision(payload.get('seen_revision'))
    request_hash = _hash({'op': 'close', 'thread_id': thread_id, 'summary': summary,
                          'seen_revision': seen, 'closed_by': closed_by})
    replay = _receipt_replay(conn, 'close', idempotency_key, request_hash)
    if replay is not None:
        return replay
    row = _thread_row(conn, thread_id)
    if not row:
        raise DialogueError('not_found', '对话不存在', 404)
    if row['status'] != 'closed':
        if seen is not None and seen != int(row['revision']):
            raise DialogueError('thread_changed', '对话已被更新', 409, {
                'current_revision': int(row['revision']),
                'current_awaiting': row['awaiting'],
            })
        now = _now()
        conn.execute(
            f'UPDATE {_t("inbox_threads")} SET status=\'closed\', awaiting=\'none\', '
            'closed_at=?, closed_by=?, closed_summary=?, updated_at=? WHERE id=?',
            (now, closed_by, summary, now, thread_id),
        )
        row = _thread_row(conn, thread_id)
    body = {'success': True, 'thread': _thread_dict(conn, row)}
    _receipt_write(conn, thread_id=thread_id, operation='close', key=idempotency_key,
                   request_hash=request_hash, response=body)
    return body


def user_close(conn, *, thread_id, payload, idempotency_key=None, actor=''):
    seen = _seen_revision(payload.get('seen_revision'), required=True)
    note = payload.get('note')
    if note is not None:
        if not isinstance(note, str):
            raise DialogueError('invalid_request', 'note 无效')
        if len(note) > MAX_SUMMARY:
            raise DialogueError('invalid_request', 'note 过长')
    request_hash = _hash({'op': 'user_close', 'thread_id': thread_id,
                          'seen_revision': seen, 'note': note})
    replay = _receipt_replay(conn, 'user_close', idempotency_key, request_hash)
    if replay is not None:
        return replay
    row = _thread_row(conn, thread_id)
    if not row:
        raise DialogueError('not_found', '对话不存在', 404)
    if row['status'] != 'closed':
        if seen != int(row['revision']):
            raise DialogueError('thread_changed', '对话已被更新', 409, {
                'current_revision': int(row['revision']),
                'current_awaiting': row['awaiting'],
            })
        now = _now()
        summary = '用户关闭：' + note.strip() if (note and note.strip()) else '用户关闭'
        conn.execute(
            f'UPDATE {_t("inbox_threads")} SET status=\'closed\', awaiting=\'none\', '
            "closed_at=?, closed_by='human', closed_summary=?, updated_at=? WHERE id=?",
            (now, summary, now, thread_id),
        )
        row = _thread_row(conn, thread_id)
    body = {'success': True, 'thread': _thread_dict(conn, row)}
    _receipt_write(conn, thread_id=thread_id, operation='user_close', key=idempotency_key,
                   request_hash=request_hash, response=body)
    return body


def reply_human(conn, *, thread_id, payload, idempotency_key=None, actor='',
                attachment_lookup=None):
    text = _text(payload.get('text'), field='text')
    seen = _seen_revision(payload.get('seen_revision'), required=True)
    attachment_ids = _attachment_ids(payload.get('attachment_ids'))
    request_hash = _hash({'op': 'reply', 'thread_id': thread_id, 'text': text,
                          'seen_revision': seen, 'attachment_ids': attachment_ids})
    replay = _receipt_replay(conn, 'reply', idempotency_key, request_hash)
    if replay is not None:
        return replay
    row = _thread_row(conn, thread_id)
    if not row:
        raise DialogueError('not_found', '对话不存在', 404)
    if row['status'] == 'closed':
        raise DialogueError('thread_closed', '对话已关闭', 409)
    if seen != int(row['revision']):
        raise DialogueError('thread_changed', '对话已被更新', 409, {
            'current_revision': int(row['revision']),
            'current_awaiting': row['awaiting'],
        })
    seq = _next_seq(conn, thread_id)
    now = _now()
    attachments = []
    if attachment_ids and attachment_lookup is not None:
        attachments = attachment_lookup(conn, attachment_ids) or []
    message_id = _insert_message(
        conn, thread_id=thread_id, seq=seq, role='human', text=text,
        actor=actor or None, attachments=attachments, awaiting_after='sela',
        idempotency_key=idempotency_key, created_at=now,
    )
    _touch_thread(conn, thread_id, revision=seq, awaiting='sela', updated_at=now)
    message = _message_row(conn, message_id)
    # v1.3: the undo feature is not enabled, so the human reply response always
    # carries ``undo_token: null`` (the field is retained for forward
    # compatibility with the original design in the contract).
    body = {'success': True,
            'thread': _thread_dict(conn, _thread_row(conn, thread_id)),
            'message': _msg_dict(message),
            'undo_token': None}
    _receipt_write(conn, thread_id=thread_id, operation='reply', key=idempotency_key,
                   request_hash=request_hash, response=body, message_id=message_id)
    return body


def get_thread(conn, *, thread_id):
    row = _thread_row(conn, thread_id)
    if not row:
        raise DialogueError('not_found', '对话不存在', 404)
    return {'success': True, 'thread': _thread_dict(conn, row)}


def _counts(conn):
    where, params = _scope()

    def count(extra):
        row = conn.execute(
            f'SELECT COUNT(*) FROM {_t("inbox_threads")} WHERE {where} AND {extra}',
            params,
        ).fetchone()
        return int(_scalar(row))

    return {
        'awaiting_human': count("status='open' AND awaiting='human'"),
        'awaiting_sela': count("status='open' AND awaiting='sela'"),
        'open': count("status='open'"),
    }


def list_threads(conn, *, awaiting=None, status='open', subject=None,
                 updated_after=None, limit=50, cursor=None):
    if awaiting not in (None, 'human', 'sela', 'none'):
        raise DialogueError('invalid_request', 'awaiting 无效')
    if status not in ('open', 'closed', 'all'):
        raise DialogueError('invalid_request', 'status 无效')
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise DialogueError('invalid_request', 'limit 无效')
    limit = max(1, min(limit, 200))
    where, params = _scope()
    if status in ('open', 'closed'):
        where += ' AND status=?'
        params.append(status)
    if awaiting:
        where += ' AND awaiting=?'
        params.append(awaiting)
    if subject:
        where += ' AND subject=?'
        params.append(subject)
    if updated_after:
        where += ' AND updated_at>=?'
        params.append(updated_after)
    order = 'ASC' if awaiting == 'sela' else 'DESC'
    offset = 0
    if cursor:
        try:
            offset = int(base64.urlsafe_b64decode(cursor.encode('utf-8')).decode('utf-8'))
        except Exception:
            raise DialogueError('invalid_request', 'cursor 无效')
    rows = conn.execute(
        f'SELECT * FROM {_t("inbox_threads")} WHERE {where} '
        f'ORDER BY updated_at {order}, id {order} LIMIT ? OFFSET ?',
        params + [limit + 1, offset],
    ).fetchall()
    rows = [_row_dict(row) for row in rows]
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more:
        next_cursor = base64.urlsafe_b64encode(
            str(offset + limit).encode('utf-8')).decode('utf-8')
    return {
        'success': True,
        'threads': [_summary_dict(conn, row) for row in rows],
        'next_cursor': next_cursor,
        'counts': _counts(conn),
    }


def observability(conn):
    counts = _counts(conn)
    where, params = _scope()
    oldest = None
    row = conn.execute(
        f'SELECT MIN(updated_at) FROM {_t("inbox_threads")} '
        f"WHERE {where} AND status='open' AND awaiting='sela'",
        params,
    ).fetchone()
    if row and _scalar(row) is not None:
        delta = _utcnow() - _parse_ts(_ts_val(_scalar(row)))
        oldest = max(0, int(delta.total_seconds()))
    if is_postgres():
        day_condition = "day >= (CURRENT_DATE - INTERVAL '30 days')::date"
    else:
        day_condition = "day >= date('now', '-30 days')"
    hits = conn.execute(
        f'SELECT route, SUM(hits) FROM {_t("inbox_legacy_route_hits")} '
        f'WHERE {where} AND {day_condition} GROUP BY route',
        params,
    ).fetchall()
    legacy = {}
    for item in hits:
        values = _row_dict(item)
        keys = list(values.keys())
        legacy[str(values[keys[0]])] = int(values[keys[1]] or 0)
    return {
        'waiting_human': counts['awaiting_human'],
        'waiting_sela': counts['awaiting_sela'],
        'oldest_wait_seconds': oldest,
        'last_poll_at': None,
        'last_ok_at': None,
        'last_poll_error': None,
        'waiting_session': None,
        'blocked_reason': None,
        'last_agent_run': None,
        'legacy_route_hits_30d': legacy,
    }


# ---------------------------------------------------------------------------
# Idempotency-free helpers
# ---------------------------------------------------------------------------

def record_legacy_hit(conn, route):
    """Best-effort legacy-route counter; never breaks the legacy route itself."""
    try:
        conn.execute('SAVEPOINT legacy_hit')
        if is_postgres():
            conn.execute(
                f'INSERT INTO {_t("inbox_legacy_route_hits")} '
                '(organization_id, legacy_user_id, route, day, hits) '
                'VALUES (trosa.compat_org_id(), trosa.compat_current_user(), ?, CURRENT_DATE, 1) '
                'ON CONFLICT (organization_id, legacy_user_id, route, day) '
                'DO UPDATE SET hits = trosa.inbox_legacy_route_hits.hits + 1',
                (route,),
            )
        else:
            conn.execute(
                'INSERT INTO inbox_legacy_route_hits '
                '(organization_id, legacy_user_id, route, day, hits) '
                "VALUES (?, ?, ?, date('now'), 1) "
                'ON CONFLICT (organization_id, legacy_user_id, route, day) '
                'DO UPDATE SET hits = hits + 1',
                (ORG_ID, scope_user(), route),
            )
        conn.execute('RELEASE SAVEPOINT legacy_hit')
    except Exception:
        try:
            conn.execute('ROLLBACK TO SAVEPOINT legacy_hit')
            conn.execute('RELEASE SAVEPOINT legacy_hit')
        except Exception:
            pass


def resolve_legacy_thread_id(conn, legacy_id):
    where, params = _scope()
    row = conn.execute(
        f'SELECT target_id FROM {_t("legacy_row_refs")} '
        f"WHERE table_name='inbox_items' AND legacy_id=? AND {where}",
        [legacy_id] + params,
    ).fetchone()
    return str(_scalar(row)) if row else None


def _ensure_legacy_ref(conn, thread_id, legacy_id):
    if is_postgres():
        conn.execute(
            f'INSERT INTO {_t("legacy_row_refs")} '
            '(organization_id, legacy_user_id, table_name, legacy_id, target_id, created_at) '
            "VALUES (trosa.compat_org_id(), trosa.compat_current_user(), 'inbox_items', ?, ?, ?) "
            'ON CONFLICT (organization_id, legacy_user_id, table_name, legacy_id) DO NOTHING',
            (legacy_id, thread_id, _now()),
        )
    else:
        conn.execute(
            'INSERT OR IGNORE INTO legacy_row_refs '
            '(organization_id, legacy_user_id, table_name, legacy_id, target_id, created_at) '
            "VALUES (?, ?, 'inbox_items', ?, ?, ?)",
            (ORG_ID, scope_user(), legacy_id, thread_id, _now()),
        )


def ensure_thread_for_legacy_item(conn, *, legacy_id, title, content, subject=None,
                                  refs=None, hints=None, created_at=None):
    """Materialise an open legacy request as a thread (on-demand migration).

    Reuses the legacy row's canonical uuid when the identifier adapter knows it.
    """
    thread_id = resolve_legacy_thread_id(conn, legacy_id)
    if thread_id and _thread_row(conn, thread_id):
        return thread_id
    if not thread_id:
        thread_id = _deterministic_uuid('inbox:' + scope_user() + ':' + str(legacy_id))
    if _thread_row(conn, thread_id) is None:
        now = created_at or _now()
        _insert_thread(conn, thread_id=thread_id, subject=subject,
                       title=title or f'历史请求 {legacy_id}', now=now,
                       awaiting='human', revision=1, opened_at=now, updated_at=now)
        _insert_message(conn, thread_id=thread_id, seq=1, role='sela',
                        text=content or title or f'历史请求 {legacy_id}',
                        actor='sela', refs=refs or [], hints=hints,
                        awaiting_after='human', created_at=now)
    _ensure_legacy_ref(conn, thread_id, legacy_id)
    return thread_id


def dual_write_legacy_answer(conn, *, legacy_id, title, content, human_text,
                             subject=None, refs=None, hints=None, created_at=None):
    """Mirror a legacy human answer into its thread and push awaiting to sela."""
    thread_id = ensure_thread_for_legacy_item(
        conn, legacy_id=legacy_id, title=title, content=content,
        subject=subject, refs=refs, hints=hints, created_at=created_at,
    )
    row = _thread_row(conn, thread_id)
    if not row or row['status'] == 'closed':
        return thread_id
    seq = _next_seq(conn, thread_id)
    now = _now()
    _insert_message(conn, thread_id=thread_id, seq=seq, role='human',
                    text=human_text, awaiting_after='sela', created_at=now)
    _touch_thread(conn, thread_id, revision=seq, awaiting='sela', updated_at=now)
    return thread_id


# ---------------------------------------------------------------------------
# Irreversible action framework (§4.4)
# ---------------------------------------------------------------------------

def irreversible_action(conn, *, thread_id, action, arguments,
                        proposal_message_id, confirmed_by_message_id,
                        handler, idempotency_key=None, actor='sela'):
    """Run an irreversible action gated by a human confirmation message.

    ``handler(conn, arguments, now)`` performs the business write inside the same
    transaction.  Any guardrail failure raises ``confirmation_required``; the
    "confirmation consumed once" claim is arbitrated atomically by the partial
    unique index ``inbox_receipts_consumed_idx``.
    """
    if action not in IRREVERSIBLE_ACTIONS:
        raise DialogueError('invalid_request', '未知的不可逆动作')
    if not isinstance(arguments, dict):
        raise DialogueError('invalid_request', 'arguments 必须是对象')
    if not proposal_message_id or not confirmed_by_message_id:
        raise DialogueError('confirmation_required', '缺少确认消息', 409,
                            {'reason': 'missing_confirmation_ids'})
    operation = 'irreversible:' + action
    if not idempotency_key:
        idempotency_key = 'auto:' + uuid.uuid4().hex
    request_hash = _hash({
        'op': operation, 'thread_id': thread_id, 'action': action,
        'arguments': arguments, 'proposal_message_id': proposal_message_id,
        'confirmed_by_message_id': confirmed_by_message_id,
    })
    replay = _receipt_replay(conn, operation, idempotency_key, request_hash)
    if replay is not None:
        return replay
    if not _lock_thread(conn, thread_id):
        raise DialogueError('not_found', '对话不存在', 404)
    proposal = _message_row(conn, proposal_message_id)
    if (not proposal or str(proposal['thread_id']) != str(thread_id)
            or proposal['role'] != 'sela'):
        raise DialogueError('confirmation_required', '提议消息无效', 409,
                            {'reason': 'proposal_message_invalid'})
    confirmation = _message_row(conn, confirmed_by_message_id)
    if (not confirmation or str(confirmation['thread_id']) != str(thread_id)
            or confirmation['role'] != 'human'):
        raise DialogueError('confirmation_required', '确认消息无效', 409,
                            {'reason': 'confirmation_message_invalid'})
    if int(confirmation['seq']) <= int(proposal['seq']):
        raise DialogueError('confirmation_required', '确认必须晚于提议', 409,
                            {'reason': 'confirmation_not_later_than_proposal'})
    if _confirmation_consumed(conn, confirmed_by_message_id):
        raise DialogueError('confirmation_required', '确认消息已被使用', 409,
                            {'reason': 'already_consumed'})
    now = _now()
    result = handler(conn, arguments, now) if handler is not None else {}
    body = {
        'success': True,
        'action': action,
        'thread': _thread_dict(conn, _thread_row(conn, thread_id)),
        'result': result,
    }
    try:
        _receipt_write(conn, thread_id=thread_id, operation=operation,
                       key=idempotency_key, request_hash=request_hash, response=body,
                       message_id=confirmed_by_message_id,
                       consumed_message_id=confirmed_by_message_id)
    except Exception as exc:
        if _is_consumed_conflict(exc):
            raise DialogueError('confirmation_required', '确认消息已被使用', 409,
                                {'reason': 'already_consumed'})
        raise
    return body


def _digits(text):
    return ''.join(ch for ch in str(text or '') if ch.isdigit())


def record_fact(conn, *, thread_id, fact, arguments, source_message_id, handler,
                needles=(), digit_needles=(), idempotency_key=None, actor='sela'):
    """Record a fact the human supplied in this thread (contract §4.4B, reversible).

    No confirmation is consumed (one human message can carry several facts), but
    the cited message must be a ``role=human`` message of this thread, and every
    value that must come from the human appears **verbatim** in it (case-insensitive;
    phone numbers compare as digit strings).  ``note`` carries the agent's own
    words, so it only needs the human source message.
    """
    if fact not in FACT_KINDS:
        raise DialogueError('invalid_request', '未知的事实类型')
    if not isinstance(arguments, dict):
        raise DialogueError('invalid_request', 'arguments 必须是对象')
    if not source_message_id:
        raise DialogueError('invalid_request', '缺少 source_message_id')
    operation = 'fact:' + fact
    if not idempotency_key:
        idempotency_key = 'auto:' + uuid.uuid4().hex
    request_hash = _hash({'op': operation, 'thread_id': thread_id, 'fact': fact,
                          'arguments': arguments, 'source_message_id': source_message_id})
    replay = _receipt_replay(conn, operation, idempotency_key, request_hash)
    if replay is not None:
        return replay
    if not _lock_thread(conn, thread_id):
        raise DialogueError('not_found', '对话不存在', 404)
    source = _message_row(conn, source_message_id)
    if not source or str(source['thread_id']) != str(thread_id) or source['role'] != 'human':
        raise DialogueError('provenance_mismatch', '来源消息必须是这个对话里人写的消息', 409,
                            {'reason': 'source_message_invalid'})
    text = str(source.get('text') or '')
    # A value sela proposed and the human then agreed to ("可以用这个邮箱") is human-supplied:
    # the human read it and answered it.  Only the sela message immediately before the human's
    # message counts, so an old or unrelated proposal can never vouch for a value.
    previous = conn.execute(
        f'SELECT * FROM {_t("inbox_messages")} WHERE thread_id=? AND seq<? ORDER BY seq DESC LIMIT 1',
        [thread_id, source['seq']],
    ).fetchone()
    previous = _row_dict(previous)
    if previous and previous.get('role') == 'sela':
        text = text + '\n' + str(previous.get('text') or '')
    lowered = text.lower()
    for needle in needles:
        if str(needle or '').strip().lower() not in lowered:
            raise DialogueError('provenance_mismatch', '这个值没有出现在你引用的那条消息里', 409,
                                {'reason': 'value_not_in_message'})
    for number in digit_needles:
        wanted = _digits(number)
        if not wanted or wanted not in _digits(text):
            raise DialogueError('provenance_mismatch', '这个号码没有出现在你引用的那条消息里', 409,
                                {'reason': 'value_not_in_message'})
    now = _now()
    result = handler(conn, arguments, now) if handler is not None else {}
    body = {'success': True, 'fact': fact, 'thread': _thread_dict(conn, _thread_row(conn, thread_id)),
            'result': result, 'already_present': bool(isinstance(result, dict) and result.get('already_present'))}
    _receipt_write(conn, thread_id=thread_id, operation=operation, key=idempotency_key,
                   request_hash=request_hash, response=body, message_id=source_message_id)
    return body
