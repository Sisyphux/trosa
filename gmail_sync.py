"""Gmail communication sync for Trosa.

The integration deliberately has a narrow responsibility: read messages from
one user-authorised Gmail account, resolve only exact Contact-email matches,
and retain enough source material to audit every imported timeline item.  It
does not send mail, modify Gmail, create customers/contacts, or create tasks.
"""
import base64
import hashlib
import html
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet, InvalidToken

from app.engine import ask_llm, get_ai_config_status
from db import (
    USERS,
    get_current_user,
    get_db,
    get_system_db,
    schedule_safety_backup,
    set_db_user,
)


logger = logging.getLogger(__name__)

GMAIL_SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
GOOGLE_AUTHORIZE_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GMAIL_API_ROOT = 'https://gmail.googleapis.com/gmail/v1/users/me'
_SETTING_PREFIX = 'integration:gmail:'
_SYNC_LOCKS = {user: threading.Lock() for user in USERS}
_SYNC_THREADS = {}
_SYNC_THREADS_LOCK = threading.Lock()
_SHANGHAI = ZoneInfo('Asia/Shanghai')


class GmailSyncError(RuntimeError):
    """A safe, user-facing Gmail integration error."""


class GmailConfigurationError(GmailSyncError):
    """Raised before a connection can begin because deployment is incomplete."""


class GmailApiError(GmailSyncError):
    """A Gmail/Google HTTP response with a safe status code for recovery."""

    def __init__(self, message, status_code=0):
        super().__init__(message)
        self.status_code = int(status_code or 0)


class _HtmlTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        if data:
            self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {'br', 'p', 'div', 'li', 'tr'}:
            self.parts.append('\n')

    def text(self):
        return '\n'.join(self.parts)


def _truthy(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _bounded_env_int(name, default, minimum, maximum):
    try:
        return min(max(int(os.environ.get(name, default)), minimum), maximum)
    except (TypeError, ValueError):
        return default


def _now_text():
    return datetime.now(_SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')


def _setting_key(user):
    if user not in USERS:
        raise GmailSyncError('无效的 Trosa 用户')
    return _SETTING_PREFIX + user


def _load_record(user):
    conn = get_system_db()
    try:
        row = conn.execute('SELECT value FROM app_settings WHERE key=?', (_setting_key(user),)).fetchone()
    finally:
        conn.close()
    if not row or not row['value']:
        return {}
    try:
        value = json.loads(row['value'])
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_record(user, record):
    conn = get_system_db()
    try:
        conn.execute('''INSERT INTO app_settings(key, value, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at''',
                     (_setting_key(user), json.dumps(record, ensure_ascii=False), _now_text()))
        conn.commit()
    finally:
        conn.close()


def _delete_record(user):
    conn = get_system_db()
    try:
        conn.execute('DELETE FROM app_settings WHERE key=?', (_setting_key(user),))
        conn.commit()
    finally:
        conn.close()


def _token_cipher():
    """Return a Fernet cipher derived from a deployment-only secret.

    A raw Fernet key is accepted.  A sufficiently long ordinary deployment
    secret is derived via SHA-256 so existing CRM secret-management practices
    can be reused without ever persisting an unencrypted refresh token.
    """
    raw = str(os.environ.get('GMAIL_TOKEN_ENCRYPTION_KEY') or os.environ.get('CRM_SESSION_SECRET') or '').strip()
    if len(raw) < 32:
        raise GmailConfigurationError('Gmail 令牌加密密钥未配置')
    try:
        return Fernet(raw.encode('utf-8'))
    except (ValueError, TypeError):
        key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode('utf-8')).digest())
        return Fernet(key)


def _encrypt_refresh_token(value):
    return _token_cipher().encrypt(str(value).encode('utf-8')).decode('ascii')


def _decrypt_refresh_token(value):
    try:
        return _token_cipher().decrypt(str(value).encode('ascii')).decode('utf-8')
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise GmailSyncError('Gmail 授权已失效，请重新连接') from exc


def _oauth_config():
    config = {
        'client_id': str(os.environ.get('GMAIL_CLIENT_ID') or '').strip(),
        'client_secret': str(os.environ.get('GMAIL_CLIENT_SECRET') or '').strip(),
        'redirect_uri': str(os.environ.get('GMAIL_REDIRECT_URI') or '').strip(),
    }
    missing = [label for key, label in (
        ('client_id', 'GMAIL_CLIENT_ID'),
        ('client_secret', 'GMAIL_CLIENT_SECRET'),
        ('redirect_uri', 'GMAIL_REDIRECT_URI'),
    ) if not config[key]]
    if missing:
        raise GmailConfigurationError('Gmail OAuth 尚未完成部署配置')
    _token_cipher()
    return config


def scheduler_enabled():
    """Whether the process should register the optional Gmail polling job."""
    if not _truthy(os.environ.get('GMAIL_SYNC_ENABLED', 'true')):
        return False
    try:
        _oauth_config()
    except GmailConfigurationError:
        return False
    return True


def _public_status(record):
    record = record or {}
    configured = True
    configuration_message = ''
    try:
        _oauth_config()
    except GmailConfigurationError as error:
        configured = False
        configuration_message = str(error)
    connected = bool(record.get('refresh_token'))
    state = str(record.get('status') or ('connected' if connected else 'not_connected'))
    return {
        'configured': configured,
        'configuration_message': configuration_message,
        'connected': connected,
        'email': str(record.get('email') or ''),
        'status': state,
        'last_started_at': str(record.get('last_started_at') or ''),
        'last_success_at': str(record.get('last_success_at') or ''),
        'last_error': str(record.get('last_error') or ''),
        'initial_sync_complete': bool(record.get('initial_sync_complete')),
        'initial_sync_started_at': str(record.get('initial_sync_started_at') or ''),
        'last_result': record.get('last_result') if isinstance(record.get('last_result'), dict) else {},
        'sync_enabled': _truthy(os.environ.get('GMAIL_SYNC_ENABLED', 'true')),
    }


def gmail_status(user):
    return _public_status(_load_record(user))


def build_authorization_url(state):
    config = _oauth_config()
    query = {
        'client_id': config['client_id'],
        'redirect_uri': config['redirect_uri'],
        'response_type': 'code',
        'scope': GMAIL_SCOPE,
        'access_type': 'offline',
        'prompt': 'consent',
        'include_granted_scopes': 'true',
        'state': str(state),
    }
    return GOOGLE_AUTHORIZE_URL + '?' + urlencode(query)


def _http_json(method, url, *, headers=None, params=None, data=None):
    try:
        response = requests.request(method, url, headers=headers, params=params, data=data,
                                    timeout=_bounded_env_int('GMAIL_HTTP_TIMEOUT_SECONDS', 20, 5, 90))
    except requests.RequestException as exc:
        raise GmailSyncError('无法连接 Gmail，请稍后重试') from exc
    payload = {}
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not response.ok:
        detail = ''
        if isinstance(payload, dict):
            error = payload.get('error')
            if isinstance(error, dict):
                detail = str(error.get('message') or '')
            elif isinstance(error, str):
                detail = error
        if response.status_code in (400, 401):
            raise GmailApiError('Gmail 授权已失效，请重新连接', response.status_code)
        if response.status_code == 404:
            raise GmailApiError('Gmail 同步基线已过期，需要重新同步', response.status_code)
        raise GmailApiError(('Gmail 请求失败' + (f'：{detail[:160]}' if detail else '')), response.status_code)
    return payload if isinstance(payload, dict) else {}


def _refresh_access_token(record):
    config = _oauth_config()
    refresh_token = _decrypt_refresh_token(record.get('refresh_token'))
    payload = _http_json('POST', GOOGLE_TOKEN_URL, data={
        'client_id': config['client_id'],
        'client_secret': config['client_secret'],
        'refresh_token': refresh_token,
        'grant_type': 'refresh_token',
    })
    access_token = str(payload.get('access_token') or '').strip()
    if not access_token:
        raise GmailSyncError('Gmail 未返回可用授权，请重新连接')
    return access_token


def _gmail_json(access_token, path, params=None):
    return _http_json('GET', GMAIL_API_ROOT + path,
                      headers={'Authorization': 'Bearer ' + access_token}, params=params)


def complete_oauth_authorization(user, code):
    """Exchange one OAuth code and persist only its encrypted refresh token."""
    config = _oauth_config()
    code = str(code or '').strip()
    if not code:
        raise GmailSyncError('Google 未返回授权码')
    token_data = _http_json('POST', GOOGLE_TOKEN_URL, data={
        'code': code,
        'client_id': config['client_id'],
        'client_secret': config['client_secret'],
        'redirect_uri': config['redirect_uri'],
        'grant_type': 'authorization_code',
    })
    refresh_token = str(token_data.get('refresh_token') or '').strip()
    access_token = str(token_data.get('access_token') or '').strip()
    if not refresh_token or not access_token:
        raise GmailSyncError('Google 未返回离线授权；请重新连接 Gmail')
    profile = _gmail_json(access_token, '/profile')
    account = _normalize_email(profile.get('emailAddress'))
    if not account:
        raise GmailSyncError('无法确认此 Gmail 账号')
    previous = _load_record(user)
    record = {
        'email': account,
        'refresh_token': _encrypt_refresh_token(refresh_token),
        'scope': str(token_data.get('scope') or GMAIL_SCOPE),
        'status': 'connected',
        'connected_at': _now_text(),
        'last_started_at': '',
        'last_success_at': '',
        'last_error': '',
        'history_id': '',
        'history_page_token': '',
        'pending_history_id': '',
        'initial_history_id': '',
        'initial_page_token': '',
        'initial_sync_complete': False,
        'initial_sync_started_at': '',
        'last_result': {},
    }
    if previous.get('email') == account and previous.get('initial_sync_complete'):
        # A deliberate reconnect must never make a later message look like a
        # new historical import. Existing per-message idempotency still guards
        # every source item, and an explicit first sync continues from now.
        record['initial_sync_complete'] = False
    _save_record(user, record)
    return _public_status(record)


def disconnect_gmail(user):
    _delete_record(user)
    return _public_status({})


def _normalize_email(value):
    value = str(value or '').strip().casefold()
    return value if re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value) else ''


def _decode_header(value):
    try:
        return str(make_header(decode_header(value or ''))).strip()
    except (UnicodeError, ValueError):
        return str(value or '').strip()


def _addresses(value):
    values = []
    seen = set()
    for name, address in getaddresses([str(value or '')]):
        normalized = _normalize_email(address)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        values.append({'email': normalized, 'name': _decode_header(name)[:160]})
    return values


def _b64url_text(value):
    if not value:
        return ''
    try:
        padded = str(value) + '=' * (-len(str(value)) % 4)
        return base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8', errors='replace')
    except (ValueError, UnicodeError):
        return ''


def _html_to_text(value):
    parser = _HtmlTextExtractor()
    try:
        parser.feed(value or '')
        parser.close()
    except (ValueError, TypeError):
        return re.sub(r'<[^>]+>', ' ', str(value or ''))
    return parser.text()


def _clean_text(value, limit=50000):
    value = str(value or '').replace('\x00', '')
    value = re.sub(r'\r\n?', '\n', value)
    value = re.sub(r'\n{3,}', '\n\n', value).strip()
    return value[:limit]


def _payload_text(payload):
    plain_parts, html_parts, attachments = [], [], []

    def walk(part):
        if not isinstance(part, dict):
            return
        filename = str(part.get('filename') or '').strip()
        if filename:
            attachments.append(filename[:240])
        mime = str(part.get('mimeType') or '').casefold()
        body = part.get('body') if isinstance(part.get('body'), dict) else {}
        data = _b64url_text(body.get('data'))
        if data and mime.startswith('text/plain'):
            plain_parts.append(data)
        elif data and mime.startswith('text/html'):
            html_parts.append(data)
        for child in part.get('parts') or []:
            walk(child)

    walk(payload)
    text = '\n\n'.join(plain_parts) if plain_parts else '\n\n'.join(_html_to_text(part) for part in html_parts)
    return _clean_text(text), list(dict.fromkeys(attachments))[:25]


def _message_time(raw, headers):
    internal = str(raw.get('internalDate') or '').strip()
    try:
        return datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc).astimezone(_SHANGHAI)
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    try:
        date = headers.get('date') or ''
        parsed = datetime.fromisoformat('1970-01-01')
        from email.utils import parsedate_to_datetime
        parsed = parsedate_to_datetime(date)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_SHANGHAI)
        return parsed.astimezone(_SHANGHAI)
    except (TypeError, ValueError, IndexError):
        return datetime.now(_SHANGHAI)


def normalize_gmail_message(raw, account):
    """Turn Gmail API JSON into bounded, auditable source material."""
    payload = raw.get('payload') if isinstance(raw.get('payload'), dict) else {}
    header_rows = payload.get('headers') if isinstance(payload.get('headers'), list) else []
    headers = {}
    for row in header_rows:
        if isinstance(row, dict) and row.get('name'):
            headers[str(row['name']).casefold()] = _decode_header(row.get('value'))
    sender = _addresses(headers.get('from'))
    recipients = _addresses(','.join(value for value in (headers.get('to'), headers.get('cc'), headers.get('bcc')) if value))
    all_participants = sender + recipients
    own = _normalize_email(account)
    sender_emails = [item['email'] for item in sender]
    recipient_emails = [item['email'] for item in recipients]
    if own and own in sender_emails:
        direction = 'outbound'
        external = [item for item in recipients if item['email'] != own]
    elif own and own in recipient_emails:
        direction = 'inbound'
        external = [item for item in sender if item['email'] != own]
    else:
        direction = 'unknown'
        external = [item for item in all_participants if item['email'] != own]
    if not external:
        external = [item for item in all_participants if item['email'] != own]
    body_text, attachments = _payload_text(payload)
    timestamp = _message_time(raw, headers)
    message_id = str(raw.get('id') or '').strip()
    thread_id = str(raw.get('threadId') or '').strip()
    subject = _decode_header(headers.get('subject') or '')[:500]
    primary = external[0] if external else {}
    sender_label = sender[0].get('name') or sender[0].get('email') if sender else ''
    return {
        'message_id': message_id,
        'thread_id': thread_id,
        'history_id': str(raw.get('historyId') or ''),
        'time': timestamp.strftime('%Y-%m-%d %H:%M:%S'),
        'date': timestamp.strftime('%Y-%m-%d'),
        'direction': direction,
        'sender': sender_label[:160],
        'sender_email': sender[0]['email'] if sender else '',
        'from': sender,
        'to': recipients,
        'external_addresses': external,
        'external_emails': [item['email'] for item in external],
        'primary_external_email': primary.get('email', ''),
        'primary_external_name': primary.get('name', ''),
        'subject': subject,
        'text': body_text,
        'snippet': _clean_text(raw.get('snippet') or '', 1200),
        'attachments': attachments,
        'label_ids': [str(item) for item in raw.get('labelIds') or []][:30],
        'source_url': 'https://mail.google.com/mail/u/0/#all/' + message_id if message_id else '',
    }


def _matches_for_emails(cursor, emails):
    emails = [email for email in dict.fromkeys(_normalize_email(value) for value in emails) if email]
    if not emails:
        return {'status': 'ignored', 'customer_id': None, 'contact_id': None, 'customer': {}, 'contact': {}}
    placeholders = ','.join('?' for _ in emails)
    rows = cursor.execute(
        '''SELECT ct.id, ct.customer_id, ct.name, ct.email, ct.is_primary,
                  c.name AS customer_name, c.company AS customer_company
           FROM contacts ct JOIN customers c ON c.id=ct.customer_id
           WHERE COALESCE(c.is_deleted, 0)=0 AND lower(trim(ct.email)) IN (''' + placeholders + ')', emails
    ).fetchall()
    rows = [dict(row) for row in rows]
    customer_ids = {row['customer_id'] for row in rows}
    if not rows:
        return {'status': 'unmatched', 'customer_id': None, 'contact_id': None, 'customer': {}, 'contact': {}}
    if len(customer_ids) != 1:
        return {'status': 'ambiguous', 'customer_id': None, 'contact_id': None, 'customer': {}, 'contact': {}}
    customer_id = next(iter(customer_ids))
    ordered = sorted(rows, key=lambda row: (
        0 if _normalize_email(row.get('email')) == emails[0] else 1,
        -int(row.get('is_primary') or 0), row['id'],
    ))
    contact = ordered[0]
    return {
        'status': 'matched',
        'customer_id': customer_id,
        'contact_id': contact['id'],
        'customer': {'id': customer_id, 'name': contact.get('customer_name') or '', 'company': contact.get('customer_company') or ''},
        'contact': {'id': contact['id'], 'name': contact.get('name') or '', 'email': contact.get('email') or ''},
    }


def _find_match(user, message):
    old_user = get_current_user()
    set_db_user(user)
    conn = get_db()
    try:
        return _matches_for_emails(conn.cursor(), message.get('external_emails') or [])
    finally:
        conn.close()
        set_db_user(old_user)


def _ai_configured():
    if not _truthy(os.environ.get('GMAIL_AI_SUMMARIZE', 'true')):
        return False
    try:
        # Use the same runtime source as the interactive AI features, including
        # the private settings-page file. This keeps Gmail summaries in the
        # single shared AI connection instead of maintaining a second check.
        return bool(get_ai_config_status().get('configured'))
    except Exception:
        # Keep Gmail synchronization resilient if the optional AI status probe
        # itself is unavailable.
        if str(os.environ.get('LLM_BACKEND') or '').strip().lower() in {'lmstudio', 'ollama'}:
            return True
        return any(str(os.environ.get(name) or '').strip() for name in (
            'DEEPSEEK_API_KEY', 'DASHSCOPE_API_KEY', 'ZHIPU_API_KEY', 'OPENAI_API_KEY',
        ))


def _fallback_summary(message):
    subject = str(message.get('subject') or '').strip()
    text = re.sub(r'\s+', ' ', str(message.get('text') or message.get('snippet') or '')).strip()
    text = text[:280] + ('…' if len(text) > 280 else '')
    external = str(message.get('primary_external_name') or message.get('primary_external_email') or '对方').strip()
    if message.get('direction') == 'outbound':
        prefix = f'我方邮件发给 {external}'
    elif message.get('direction') == 'inbound':
        prefix = f'{external} 来信'
    else:
        prefix = f'与 {external} 的邮件往来'
    details = ' · '.join(item for item in (subject, text) if item)
    return (prefix + ('：' + details if details else '。'))[:700]


def _summary_from_ai(message, customer, contact):
    fallback = _fallback_summary(message)
    if not _ai_configured():
        return fallback, False
    original = _clean_text(message.get('text') or message.get('snippet') or '', 12000)
    if not original:
        return fallback, False
    direction = message.get('direction') or 'unknown'
    direction_instruction = {
        'outbound': '这是我方发出的邮件；不得把我方说过的话改写成客户需求或确认。',
        'inbound': '这是客户发来的邮件；不得把客户说过的话改写成我方行动或承诺。',
        'unknown': '邮件收发方向不确定；只客观描述邮件内容，不要猜测责任方。',
    }.get(direction, '')
    prompt = '''你是 Trosa CRM 的既有“沟通整理”能力。把一封已经通过精确联系人邮箱匹配的 Gmail 邮件整理为严格 JSON，不输出 Markdown。
字段只有 summary（1-2 句中文，最多 320 字）和 key_facts（字符串数组，最多 6 项）。
只能根据原文描述已发生的事实；不要编造产品、规格、数量、价格、交期、承诺、下一步或客户身份。不要生成待办或建议。
''' + direction_instruction + '\n已知客户：' + str(customer.get('company') or customer.get('name') or '未命名') + \
        '\n已知联系人：' + str(contact.get('name') or '') + \
        '\n邮件主题：' + str(message.get('subject') or '') + \
        '\n邮件原文：\n' + original
    try:
        raw = ask_llm(prompt)
    except Exception:
        return fallback, False
    if not raw or str(raw).lstrip().startswith(('[ERROR', '[错误]')):
        return fallback, False
    try:
        start, end = raw.find('{'), raw.rfind('}') + 1
        parsed = json.loads(raw[start:end]) if start >= 0 and end > start else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = {}
    summary = _clean_text(parsed.get('summary') if isinstance(parsed, dict) else '', 700)
    return (summary or fallback), bool(summary)


def _message_processed(user, message_id):
    old_user = get_current_user()
    set_db_user(user)
    conn = get_db()
    try:
        row = conn.execute('SELECT provider_message_id FROM gmail_message_states WHERE provider_message_id=?',
                           (message_id,)).fetchone()
        return bool(row)
    finally:
        conn.close()
        set_db_user(old_user)


def _source_payload(message):
    return {
        'provider': 'gmail',
        'message_id': message.get('message_id', ''),
        'thread_id': message.get('thread_id', ''),
        'time': message.get('time', ''),
        'direction': message.get('direction', 'unknown'),
        'sender': message.get('sender', ''),
        'sender_email': message.get('sender_email', ''),
        'to': message.get('to', []),
        'subject': message.get('subject', ''),
        'text': message.get('text', ''),
        'raw_text': message.get('text', ''),
        'snippet': message.get('snippet', ''),
        'attachments': message.get('attachments', []),
        'source_url': message.get('source_url', ''),
    }


def _capture_payload(message, account=''):
    payload = _source_payload(message)
    return {
        'channel': 'gmail',
        'platform': 'Gmail',
        'account': _normalize_email(account),
        'source_url': message.get('source_url', ''),
        'conversation_identity': message.get('thread_id') or message.get('message_id') or '',
        'email': message.get('primary_external_email', ''),
        'direction': message.get('direction', 'unknown'),
        'messages': [payload],
    }


def _store_state(cursor, message, match, *, activity_id=None, inbox_item_id=None, error=''):
    now = _now_text()
    source = _source_payload(message)
    cursor.execute('''INSERT INTO gmail_message_states
                    (provider_message_id, provider_thread_id, message_time, sender_email, recipient_emails,
                     subject, customer_id, contact_id, match_status, activity_id, inbox_item_id, raw_payload,
                     last_error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                   (message.get('message_id', ''), message.get('thread_id', ''), message.get('time', ''),
                    message.get('sender_email', ''), json.dumps(message.get('to') or [], ensure_ascii=False),
                    message.get('subject', ''), match.get('customer_id'), match.get('contact_id'), match.get('status'),
                    activity_id, inbox_item_id, json.dumps(source, ensure_ascii=False), error[:500], now, now))


def _insert_source(cursor, activity_id, account, message):
    source = _source_payload(message)
    cursor.execute('''INSERT INTO communication_sources
                    (activity_id, channel, source_url, account, conversation_identity, adapter_version,
                     extraction_scope, warnings, raw_payload, cleaned_payload, captured_at)
                    VALUES (?, 'gmail', ?, ?, ?, 'gmail-v0.1', 'gmail_api', '[]', ?, ?, ?)''',
                   (activity_id, message.get('source_url', ''), account, message.get('thread_id', ''),
                    json.dumps(source, ensure_ascii=False), json.dumps([source], ensure_ascii=False), _now_text()))
    cursor.execute('''INSERT INTO communication_source_items
                    (source_fingerprint, activity_id, message_time, direction, raw_text)
                    VALUES (?, ?, ?, ?, ?)''',
                   ('gmail:' + account + ':' + message.get('message_id', ''), activity_id,
                    message.get('time', ''), message.get('direction', 'unknown'), message.get('text', '')[:12000]))


def _store_message(user, account, message, summary):
    """Persist one message atomically after a fresh exact-email match."""
    old_user = get_current_user()
    set_db_user(user)
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT provider_message_id FROM gmail_message_states WHERE provider_message_id=?',
                     (message.get('message_id'),)).fetchone():
            conn.rollback()
            return {'state': 'duplicate'}
        match = _matches_for_emails(c, message.get('external_emails') or [])
        now = _now_text()
        if match['status'] == 'matched':
            escaped_summary = html.escape(str(summary or _fallback_summary(message)), quote=False)
            c.execute('''INSERT INTO follow_up_logs
                        (customer_id, content, follow_date, result, next_plan, activity_type, direction,
                         contact_id, source, created_at)
                        VALUES (?, ?, ?, '', '', 'email', ?, ?, 'gmail', ?)''',
                      (match['customer_id'], escaped_summary, message.get('date') or now[:10],
                       message.get('direction', 'unknown'), match.get('contact_id'), now))
            activity_id = c.lastrowid
            _insert_source(c, activity_id, account, message)
            follow_date = message.get('date') or now[:10]
            c.execute('''UPDATE customers
                         SET last_contact=CASE WHEN COALESCE(last_contact, '')='' OR last_contact<? THEN ? ELSE last_contact END,
                             customer_type='existing',
                             status=CASE WHEN status='未建联' THEN '跟进中' ELSE status END,
                             updated_at=CASE WHEN COALESCE(last_contact, '')='' OR last_contact<? THEN ? ELSE updated_at END
                         WHERE id=?''',
                      (follow_date, follow_date, follow_date, now, match['customer_id']))
            _store_state(c, message, match, activity_id=activity_id)
            conn.commit()
            return {'state': 'matched', 'activity_id': activity_id, 'customer_id': match['customer_id']}

        if match['status'] == 'ignored':
            _store_state(c, message, match)
            conn.commit()
            return {'state': 'ignored'}

        capture = _capture_payload(message, account)
        prefix = 'Gmail 邮件归属有冲突' if match['status'] == 'ambiguous' else '待归属 Gmail 邮件'
        identity = message.get('primary_external_email') or message.get('sender_email') or '未识别对象'
        c.execute('''INSERT OR IGNORE INTO inbox_items
                    (item_type, customer_id, title, content, dedupe_key, status, created_at)
                    VALUES ('gmail_capture', NULL, ?, ?, ?, 'open', ?)''',
                  (prefix + '：' + identity[:180], json.dumps(capture, ensure_ascii=False),
                   'gmail:' + account + ':' + message.get('message_id', ''), now))
        inbox_row = c.execute('SELECT id FROM inbox_items WHERE dedupe_key=?',
                              ('gmail:' + account + ':' + message.get('message_id', ''),)).fetchone()
        _store_state(c, message, match, inbox_item_id=(inbox_row['id'] if inbox_row else None))
        conn.commit()
        return {'state': match['status'], 'inbox_item_id': inbox_row['id'] if inbox_row else None}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        set_db_user(old_user)


def attach_gmail_capture_to_activity(cursor, inbox_item, activity_id, customer_id, contact_id=None):
    """Attach an Inbox-confirmed Gmail source to the shared timeline write."""
    if not inbox_item or inbox_item.get('item_type') != 'gmail_capture':
        return
    try:
        payload = json.loads(inbox_item.get('content') or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    messages = payload.get('messages') if isinstance(payload, dict) else []
    message = messages[0] if isinstance(messages, list) and messages and isinstance(messages[0], dict) else {}
    message_id = str(message.get('message_id') or '').strip()
    if not message_id:
        return
    account = _normalize_email(payload.get('account') or '')
    if not account:
        fingerprint = str(inbox_item.get('dedupe_key') or '')
        parts = fingerprint.split(':', 2)
        account = _normalize_email(parts[1] if len(parts) == 3 else '')
    account = account or 'gmail'
    source_message = {
        'message_id': message_id,
        'thread_id': str(message.get('thread_id') or payload.get('conversation_identity') or ''),
        'time': str(message.get('time') or ''),
        'direction': str(message.get('direction') or payload.get('direction') or 'unknown'),
        'sender': str(message.get('sender') or ''),
        'sender_email': str(message.get('sender_email') or ''),
        'to': message.get('to') if isinstance(message.get('to'), list) else [],
        'subject': str(message.get('subject') or ''),
        'text': _clean_text(message.get('text') or message.get('raw_text') or '', 50000),
        'snippet': str(message.get('snippet') or ''),
        'attachments': message.get('attachments') if isinstance(message.get('attachments'), list) else [],
        'source_url': str(message.get('source_url') or payload.get('source_url') or ''),
    }
    existing = cursor.execute('SELECT id FROM communication_source_items WHERE source_fingerprint=?',
                              ('gmail:' + account + ':' + message_id,)).fetchone()
    if not existing:
        _insert_source(cursor, activity_id, account, source_message)
    cursor.execute('''UPDATE gmail_message_states
                      SET customer_id=?, contact_id=?, match_status='matched', activity_id=?, inbox_item_id=?,
                          last_error='', updated_at=?
                      WHERE provider_message_id=?''',
                   (customer_id, contact_id, activity_id, inbox_item.get('id'), _now_text(), message_id))


def _list_initial_message_refs(access_token, record, limit):
    lookback = _bounded_env_int('GMAIL_INITIAL_LOOKBACK_DAYS', 90, 1, 365)
    after = (datetime.now(_SHANGHAI).date() - timedelta(days=lookback)).strftime('%Y/%m/%d')
    page_token = str(record.get('initial_page_token') or '')
    refs = []
    while len(refs) < limit:
        params = {'q': 'after:' + after, 'maxResults': min(100, limit - len(refs))}
        if page_token:
            params['pageToken'] = page_token
        response = _gmail_json(access_token, '/messages', params=params)
        refs.extend(item for item in response.get('messages') or [] if isinstance(item, dict) and item.get('id'))
        page_token = str(response.get('nextPageToken') or '')
        if not page_token:
            break
    return refs, page_token


def _list_history_message_refs(access_token, record, limit):
    start_history_id = str(record.get('history_id') or '')
    if not start_history_id:
        return [], '', ''
    page_token = str(record.get('history_page_token') or '')
    latest_history_id = str(record.get('pending_history_id') or '')
    refs, seen = [], set()
    while len(refs) < limit:
        params = {'startHistoryId': start_history_id, 'historyTypes': 'messageAdded',
                  'maxResults': min(500, max(1, limit - len(refs)))}
        if page_token:
            params['pageToken'] = page_token
        response = _gmail_json(access_token, '/history', params=params)
        latest_history_id = str(response.get('historyId') or latest_history_id)
        for history in response.get('history') or []:
            if not isinstance(history, dict):
                continue
            for addition in history.get('messagesAdded') or []:
                message = addition.get('message') if isinstance(addition, dict) else {}
                message_id = str(message.get('id') or '') if isinstance(message, dict) else ''
                if message_id and message_id not in seen:
                    seen.add(message_id)
                    refs.append(message)
                    if len(refs) >= limit:
                        break
            if len(refs) >= limit:
                break
        page_token = str(response.get('nextPageToken') or '')
        if not page_token:
            break
    return refs, page_token, latest_history_id


def _get_message(access_token, message_id):
    return _gmail_json(access_token, '/messages/' + str(message_id), params={'format': 'full'})


def _sync_message_batch(user, account, access_token, refs):
    result = {'matched': 0, 'unmatched': 0, 'ambiguous': 0, 'ignored': 0, 'duplicate': 0, 'failed': 0}
    ai_budget = _bounded_env_int('GMAIL_AI_MAX_SUMMARIES_PER_SYNC', 20, 0, 100)
    for ref in refs:
        message_id = str((ref or {}).get('id') or '').strip()
        if not message_id:
            continue
        if _message_processed(user, message_id):
            result['duplicate'] += 1
            continue
        try:
            raw = _get_message(access_token, message_id)
            message = normalize_gmail_message(raw, account)
            if not message.get('message_id'):
                result['failed'] += 1
                continue
            match = _find_match(user, message)
            summary = _fallback_summary(message)
            if match.get('status') == 'matched' and ai_budget > 0:
                summary, used_ai = _summary_from_ai(message, match.get('customer') or {}, match.get('contact') or {})
                if used_ai:
                    ai_budget -= 1
            outcome = _store_message(user, account, message, summary)
            state = outcome.get('state', 'failed')
            result[state] = result.get(state, 0) + 1
        except GmailSyncError as error:
            logger.warning('Gmail message import skipped [%s]: %s', user, error)
            result['failed'] += 1
        except Exception:
            logger.exception('Gmail message import failed [%s]', user)
            result['failed'] += 1
    return result


def _mark_sync_error(user, record, error, needs_reconnect=False):
    record = dict(record or {})
    record['status'] = 'needs_reconnect' if needs_reconnect else 'error'
    record['last_error'] = str(error)[:500]
    record['last_finished_at'] = _now_text()
    _save_record(user, record)
    return _public_status(record)


def sync_gmail_user(user, reason='manual'):
    """Synchronize one connected user. Called only by a bounded background job."""
    if user not in USERS:
        return {'status': 'invalid_user'}
    if not scheduler_enabled():
        return {'status': 'not_configured'}
    lock = _SYNC_LOCKS[user]
    if not lock.acquire(blocking=False):
        return {'status': 'already_running'}
    try:
        record = _load_record(user)
        if not record.get('refresh_token'):
            return {'status': 'not_connected'}
        record['status'] = 'syncing'
        record['last_started_at'] = _now_text()
        record['last_error'] = ''
        _save_record(user, record)
        try:
            access_token = _refresh_access_token(record)
            profile = _gmail_json(access_token, '/profile')
            account = _normalize_email(profile.get('emailAddress')) or _normalize_email(record.get('email'))
            if not account:
                raise GmailSyncError('无法确认已连接的 Gmail 账号')
            record['email'] = account
            incremental_limit = _bounded_env_int('GMAIL_SYNC_MAX_MESSAGES', 100, 1, 1000)
            mode = 'incremental' if record.get('initial_sync_complete') and record.get('history_id') else 'initial'
            if mode == 'initial':
                initial_limit = _bounded_env_int('GMAIL_INITIAL_MAX_MESSAGES', 100, 1, 1000)
                if not record.get('initial_history_id'):
                    record['initial_history_id'] = str(profile.get('historyId') or '')
                    record['initial_sync_started_at'] = _now_text()
                    _save_record(user, record)
                refs, next_page = _list_initial_message_refs(access_token, record, initial_limit)
                state_updates = {
                    'initial_page_token': next_page,
                    'initial_sync_complete': not bool(next_page),
                }
                if not next_page:
                    state_updates.update({
                        'history_id': record.get('initial_history_id') or str(profile.get('historyId') or ''),
                        'initial_history_id': '',
                    })
            else:
                try:
                    refs, next_page, latest_history_id = _list_history_message_refs(access_token, record, incremental_limit)
                    state_updates = {
                        'history_page_token': next_page,
                        'pending_history_id': latest_history_id if next_page else '',
                    }
                    if not next_page and latest_history_id:
                        state_updates['history_id'] = latest_history_id
                except GmailApiError as error:
                    if error.status_code != 404:
                        raise
                    mode = 'resync'
                    record.update({
                        'initial_sync_complete': False,
                        'initial_page_token': '',
                        'initial_history_id': str(profile.get('historyId') or ''),
                        'history_page_token': '',
                        'pending_history_id': '',
                    })
                    initial_limit = _bounded_env_int('GMAIL_INITIAL_MAX_MESSAGES', 100, 1, 1000)
                    refs, next_page = _list_initial_message_refs(access_token, record, initial_limit)
                    state_updates = {
                        'initial_page_token': next_page,
                        'initial_sync_complete': not bool(next_page),
                        'history_id': '' if next_page else record.get('initial_history_id'),
                        'initial_history_id': '' if not next_page else record.get('initial_history_id'),
                    }
            counts = _sync_message_batch(user, account, access_token, refs)
            record.update(state_updates)
            record['status'] = 'connected'
            record['last_success_at'] = _now_text()
            record['last_finished_at'] = _now_text()
            record['last_error'] = ''
            record['last_result'] = {**counts, 'mode': mode, 'processed': len(refs),
                                     'continuing': bool(record.get('initial_page_token') or record.get('history_page_token')),
                                     'reason': reason}
            _save_record(user, record)
            if any(counts.get(key) for key in ('matched', 'unmatched', 'ambiguous')):
                schedule_safety_backup('gmail_sync')
            logger.info('Gmail sync [%s] %s: %s', user, mode, record['last_result'])
            return {'status': 'completed', **record['last_result']}
        except GmailApiError as error:
            return _mark_sync_error(user, record, error, needs_reconnect=error.status_code in (400, 401))
        except GmailSyncError as error:
            return _mark_sync_error(user, record, error, needs_reconnect='授权' in str(error))
        except Exception:
            logger.exception('Gmail sync failed [%s]', user)
            return _mark_sync_error(user, record, 'Gmail 同步失败，请稍后重试')
    finally:
        lock.release()


def start_gmail_sync(user, reason='manual'):
    """Schedule one local background run; never make a browser wait for mail IO."""
    if user not in USERS:
        raise GmailSyncError('无效的 Trosa 用户')
    if not scheduler_enabled():
        raise GmailConfigurationError('Gmail 同步尚未完成部署配置')
    if not _load_record(user).get('refresh_token'):
        raise GmailSyncError('请先连接 Gmail')
    lock = _SYNC_LOCKS[user]
    if lock.locked():
        return {'started': False, 'already_running': True, 'status': gmail_status(user)}

    def run():
        try:
            sync_gmail_user(user, reason=reason)
        finally:
            with _SYNC_THREADS_LOCK:
                _SYNC_THREADS.pop(user, None)

    worker = threading.Thread(target=run, name='gmail-sync-' + user, daemon=True)
    with _SYNC_THREADS_LOCK:
        _SYNC_THREADS[user] = worker
    worker.start()
    return {'started': True, 'already_running': False, 'status': gmail_status(user)}


def enqueue_scheduled_gmail_sync():
    """Scheduler entrypoint. Connections remain account-scoped and optional."""
    results = {}
    if not scheduler_enabled():
        return results
    for user in USERS:
        record = _load_record(user)
        if not record.get('refresh_token'):
            continue
        try:
            results[user] = start_gmail_sync(user, reason='scheduler')
        except GmailSyncError as error:
            logger.warning('Unable to schedule Gmail sync for %s: %s', user, error)
    return results
