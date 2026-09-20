"""Deterministic customer identity resolution for external facts.

External adapters (Gmail, Sela, Agent and the browser capture) must decide
which Customer a new fact belongs to before it can reach the timeline.  The
rule is deliberately conservative: only exact, conflict-free, deterministic
evidence may auto-attribute a fact.  Fuzzy signals such as company-name
similarity or signature parsing are never accepted here; they can only produce
candidates for a human (see ``_capture_customer_matches`` in ``app.py``).

The module owns two things:

* ``resolve_identity`` -- a read-only decision over explicit evidence
  (emails, domains, thread id, source identity, an already-trusted customer).
* ``identity_link_facts`` -- durable, reusable identity facts.  A human
  confirmation/correction is stored here so the same email/domain/thread never
  asks for a decision twice.

No function in this module creates a Customer.  Identity attribution and
Customer creation stay independent.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from db import postgres_mode
from trosa_domain import active_customers, customer_contacts


PUBLIC_EMAIL_DOMAINS = frozenset({
    'gmail.com', 'googlemail.com', 'outlook.com', 'hotmail.com', 'live.com',
    'yahoo.com', 'icloud.com', 'me.com', 'qq.com', 'foxmail.com',
    '163.com', '126.com', 'yeah.net',
})

IDENTIFIER_TYPES = ('email', 'domain', 'thread', 'source')

_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
_DOMAIN_RE = re.compile(r'^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$')

# Methods that describe *how* a fact became deterministic.  They are surfaced
# verbatim to the user as the attribution basis, so keep them short and factual.
METHOD_LABELS = {
    'confirmed_email': '已确认邮箱',
    'contact_email': '联系人邮箱',
    'confirmed_domain': '已确认域名',
    'website_domain': '客户官网域名',
    'website_domain_conflict': '多个客户共享该域名',
    'confirmed_thread': '已确认邮件线程',
    'thread_history': '历史邮件线程归属',
    'confirmed_source': '已确认来源关联',
    'source_identity': '来源客户关联',
    'trusted_source_customer': '来源已关联客户',
}


def normalize_email(value: Any) -> str:
    return str(value or '').strip().casefold()


def email_domain(value: Any) -> str:
    """Return the lower-case host of an email address, or ''."""
    email = normalize_email(value)
    if '@' not in email:
        return ''
    domain = email.rsplit('@', 1)[-1].strip().strip('>').strip('.')
    return domain if _DOMAIN_RE.match(domain) else ''


def normalize_domain(value: Any) -> str:
    """Reduce a website/URL/domain to a comparable lower-case host."""
    text = str(value or '').strip().casefold()
    if not text:
        return ''
    text = re.sub(r'^[a-z][a-z0-9+.-]*://', '', text).split('/', 1)[0]
    text = text.split('@')[-1].split(':', 1)[0].strip().strip('.').strip('>')
    if text.startswith('www.'):
        text = text[4:]
    return text if _DOMAIN_RE.match(text) else ''


def _normalize_identifier(identifier_type: str, value: Any) -> str:
    if identifier_type == 'email':
        return normalize_email(value)
    if identifier_type == 'domain':
        return normalize_domain(value)
    return str(value or '').strip().casefold()


def _is_public_domain(domain: str) -> bool:
    return not domain or domain in PUBLIC_EMAIL_DOMAINS


def _source_key(source: Any, external_id: Any) -> str:
    return f"{str(source or '').strip().casefold()}:{str(external_id or '').strip().casefold()}"


# ---------------------------------------------------------------------------
# Durable identity facts
# ---------------------------------------------------------------------------

def record_identity_fact(
    conn: Any, *, identifier_type: str, identifier_value: Any, customer_id: int,
    origin: str = 'human_confirmed', method: str = '', resolution: str = '',
    source_inbox_item_id: int | None = None, created_by: str = '',
) -> int | None:
    """Insert or refresh one active identity fact.

    ``identifier_type`` is one of ``email``/``domain``/``thread``/``source``.
    When a fact already exists it is *corrected* to the supplied customer, which
    is exactly the "human correction becomes reusable" contract.
    """
    if identifier_type not in IDENTIFIER_TYPES:
        raise ValueError(f'unsupported identity fact type: {identifier_type}')
    value = _normalize_identifier(identifier_type, identifier_value)
    if not value:
        return None
    customer_id = int(customer_id)
    if postgres_mode():
        return _record_fact_postgres(
            conn, identifier_type=identifier_type, value=value, customer_id=customer_id,
            origin=origin, method=method, resolution=resolution,
            source_inbox_item_id=source_inbox_item_id, created_by=created_by,
        )
    row = conn.execute(
        "SELECT id FROM identity_link_facts WHERE identifier_type=? AND identifier_value=? "
        "AND (revoked_at='' OR revoked_at IS NULL)",
        (identifier_type, value),
    ).fetchone()
    if row:
        conn.execute(
            '''UPDATE identity_link_facts
                  SET customer_id=?, origin=?, method=?, resolution=?, source_inbox_item_id=?,
                      created_by=?, created_at=datetime('now','localtime')
                WHERE id=?''',
            (customer_id, origin, method, resolution, source_inbox_item_id, created_by, row['id']),
        )
        return int(row['id'])
    cursor = conn.execute(
        '''INSERT INTO identity_link_facts
           (identifier_type, identifier_value, customer_id, origin, method, resolution,
            source_inbox_item_id, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))''',
        (identifier_type, value, customer_id, origin, method, resolution,
         source_inbox_item_id, created_by),
    )
    return int(cursor.lastrowid)


def _record_fact_postgres(
    conn: Any, *, identifier_type: str, value: str, customer_id: int, origin: str,
    method: str, resolution: str, source_inbox_item_id: int | None, created_by: str,
) -> int | None:
    account = conn.execute(
        '''SELECT account_id FROM trosa.account_legacy_refs
            WHERE organization_id=trosa.compat_org_id()
              AND legacy_user_id=trosa.compat_current_user()
              AND legacy_customer_id=?''', (customer_id,),
    ).fetchone()
    if not account:
        return None
    inbox_uuid = None
    if source_inbox_item_id:
        inbox_uuid = conn.execute(
            '''SELECT target_id FROM trosa.legacy_row_refs
                WHERE organization_id=trosa.compat_org_id()
                  AND legacy_user_id=trosa.compat_current_user()
                  AND table_name='inbox_items' AND legacy_id=?''', (source_inbox_item_id,),
        ).fetchone()
        inbox_uuid = inbox_uuid['target_id'] if inbox_uuid else None
    conn.execute(
        '''INSERT INTO trosa.identity_link_facts
           (id, organization_id, legacy_user_id, account_id, identifier_type, identifier_value,
            origin, method, resolution, source_inbox_item_id, created_by, revoked_at, created_at)
           VALUES (trosa.compat_uuid(
                       'identity-fact:' || trosa.compat_org_id()::text || ':' ||
                       trosa.compat_current_user() || ':' || ? || ':' || ?),
                   trosa.compat_org_id(), trosa.compat_current_user(), ?, ?, ?,
                   ?, ?, ?, ?, ?, NULL, now())
           ON CONFLICT (id) DO UPDATE SET account_id=excluded.account_id,
                         origin=excluded.origin, method=excluded.method,
                         resolution=excluded.resolution,
                         source_inbox_item_id=excluded.source_inbox_item_id,
                         created_by=excluded.created_by, revoked_at=NULL, created_at=now()''',
        (identifier_type, value, account['account_id'], identifier_type, value, origin,
         method, resolution, inbox_uuid, created_by),
    )
    return None


def revoke_identity_facts(
    conn: Any, *, identifier_type: str | None = None, identifier_value: Any = None,
    customer_id: int | None = None,
) -> int:
    """Revoke active facts matched by the supplied filters (at least one)."""
    clauses = []
    params: list[Any] = []
    if identifier_type:
        clauses.append('identifier_type=?')
        params.append(identifier_type)
        if identifier_value is not None:
            clauses.append('identifier_value=?')
            params.append(_normalize_identifier(identifier_type, identifier_value))
    if customer_id is not None:
        clauses.append('customer_id=?')
        params.append(int(customer_id))
    if not clauses:
        return 0
    where = ' AND '.join(clauses)
    if postgres_mode():
        rows = conn.execute(
            f'''UPDATE trosa.identity_link_facts SET revoked_at=now()
                 WHERE organization_id=trosa.compat_org_id()
                   AND legacy_user_id=trosa.compat_current_user()
                   AND revoked_at IS NULL AND {where}''', params,
        )
        return int(rows.rowcount or 0)
    rows = conn.execute(
        f"UPDATE identity_link_facts SET revoked_at=datetime('now','localtime') "
        f"WHERE (revoked_at='' OR revoked_at IS NULL) AND {where}", params,
    )
    return int(rows.rowcount or 0)


def active_facts_for_identifier(conn: Any, identifier_type: str, identifier_value: Any) -> list[dict]:
    value = _normalize_identifier(identifier_type, identifier_value)
    if not value:
        return []
    if postgres_mode():
        rows = conn.execute(
            '''SELECT ar.legacy_customer_id AS customer_id, fact.origin, fact.method,
                      fact.resolution, fact.created_at::text AS created_at
                 FROM trosa.identity_link_facts fact
                 JOIN trosa.account_legacy_refs ar
                   ON ar.account_id=fact.account_id
                  AND ar.organization_id=fact.organization_id
                  AND ar.legacy_user_id=fact.legacy_user_id
                WHERE fact.organization_id=trosa.compat_org_id()
                  AND fact.legacy_user_id=trosa.compat_current_user()
                  AND fact.identifier_type=? AND fact.identifier_value=?
                  AND fact.revoked_at IS NULL''', (identifier_type, value),
        ).fetchall()
    else:
        rows = conn.execute(
            '''SELECT customer_id, origin, method, resolution, created_at
                 FROM identity_link_facts
                WHERE identifier_type=? AND identifier_value=?
                  AND (revoked_at='' OR revoked_at IS NULL)''', (identifier_type, value),
        ).fetchall()
    return [dict(row) for row in rows]


def facts_for_customer(conn: Any, customer_id: int) -> list[dict]:
    if postgres_mode():
        rows = conn.execute(
            '''SELECT fact.identifier_type, fact.identifier_value, fact.origin, fact.method,
                      fact.resolution, fact.created_at::text AS created_at
                 FROM trosa.identity_link_facts fact
                 JOIN trosa.account_legacy_refs ar
                   ON ar.account_id=fact.account_id
                  AND ar.organization_id=fact.organization_id
                  AND ar.legacy_user_id=fact.legacy_user_id
                WHERE fact.organization_id=trosa.compat_org_id()
                  AND fact.legacy_user_id=trosa.compat_current_user()
                  AND ar.legacy_customer_id=? AND fact.revoked_at IS NULL
                ORDER BY fact.created_at DESC''', (int(customer_id),),
        ).fetchall()
    else:
        rows = conn.execute(
            '''SELECT identifier_type, identifier_value, origin, method, resolution, created_at
                 FROM identity_link_facts
                WHERE customer_id=? AND (revoked_at='' OR revoked_at IS NULL)
                ORDER BY created_at DESC''', (int(customer_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def _fact_customers(conn: Any, identifier_type: str, identifier_value: Any) -> set[int]:
    return {
        int(row['customer_id'])
        for row in active_facts_for_identifier(conn, identifier_type, identifier_value)
        if row.get('customer_id') is not None
    }


# ---------------------------------------------------------------------------
# Deterministic directories
# ---------------------------------------------------------------------------

def _contact_email_index(conn: Any) -> dict[str, dict[int, dict]]:
    index: dict[str, dict[int, dict]] = {}
    for customer in active_customers(conn):
        customer_id = int(customer['id'])
        for contact in customer_contacts(conn, customer_id):
            email = normalize_email(contact.get('email'))
            if not email:
                continue
            index.setdefault(email, {}).setdefault(customer_id, contact)
    return index


def _website_domain_index(conn: Any) -> dict[str, set[int]]:
    index: dict[str, set[int]] = {}
    for customer in active_customers(conn):
        domain = normalize_domain(customer.get('website'))
        if domain:
            index.setdefault(domain, set()).add(int(customer['id']))
    return index


def _external_identity_customers(conn: Any, source: str, external_id: str) -> set[int]:
    customers: set[int] = set()
    if not source or not external_id:
        return customers
    if postgres_mode():
        for row in conn.execute(
                'SELECT id FROM trosa.customer_records WHERE external_source=? AND external_id=?',
                (source, external_id)).fetchall():
            customers.add(int(row['id']))
        for row in conn.execute(
                '''SELECT customer_id FROM trosa.agent_prospect_profiles
                    WHERE organization_id=trosa.compat_org_id()
                      AND legacy_user_id=trosa.compat_current_user()
                      AND source=? AND source_id=?''', (source, external_id)).fetchall():
            customers.add(int(row['customer_id']))
        return customers
    for row in conn.execute(
            '''SELECT id FROM customers
                WHERE external_source=? AND external_id=?
                  AND (is_deleted=0 OR is_deleted IS NULL)''',
            (source, external_id)).fetchall():
        customers.add(int(row['id']))
    for row in conn.execute(
            'SELECT customer_id FROM agent_prospect_profiles WHERE source=? AND source_id=?',
            (source, external_id)).fetchall():
        customers.add(int(row['customer_id']))
    return customers


def _thread_history_customers(conn: Any, thread_id: str) -> set[int]:
    thread_id = str(thread_id or '').strip()
    if not thread_id:
        return set()
    customers: set[int] = set()
    if postgres_mode():
        rows = conn.execute(
            '''SELECT ar.legacy_customer_id AS customer_id
                 FROM trosa.email_message_receipts receipt
                 JOIN trosa.account_legacy_refs ar
                   ON ar.account_id=receipt.account_id
                  AND ar.organization_id=trosa.compat_org_id()
                  AND ar.legacy_user_id=trosa.compat_current_user()
                WHERE receipt.organization_id=trosa.compat_org_id()
                  AND receipt.legacy_user_id=trosa.compat_current_user()
                  AND receipt.provider_thread_id=?
                  AND receipt.account_id IS NOT NULL''', (thread_id,)).fetchall()
    else:
        rows = conn.execute(
            '''SELECT DISTINCT customer_id FROM gmail_message_states
                WHERE provider_thread_id=? AND customer_id IS NOT NULL''',
            (thread_id,)).fetchall()
    for row in rows:
        if row['customer_id'] is not None:
            customers.add(int(row['customer_id']))
    return customers


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def _add_method(bucket: dict[int, list[str]], customer_id: int, method: str) -> None:
    bucket.setdefault(int(customer_id), [])
    if method not in bucket[int(customer_id)]:
        bucket[int(customer_id)].append(method)


def resolve_identity(conn: Any, evidence: dict) -> dict:
    """Return the deterministic identity decision for one external fact.

    ``evidence`` keys (all optional):
      ``emails``      iterable of participant email addresses
      ``domains``     iterable of extra website/domain hints
      ``thread_id``   provider thread id
      ``source``/``external_id``  adapter identity
      ``customer_id`` an already-trusted customer association from the source

    Returns ``{'status', 'customer_id', 'contact_id', 'methods', 'reason',
    'candidates', 'deterministic'}`` where ``status`` is ``matched`` (exactly
    one customer, safe to auto-attribute), ``conflict`` (several customers),
    or ``unmatched``.
    """
    evidence = evidence if isinstance(evidence, dict) else {}
    emails = [email for email in dict.fromkeys(
        normalize_email(value) for value in _as_list(evidence.get('emails'))) if email]
    domains = [domain for domain in dict.fromkeys(
        normalize_domain(value) for value in _as_list(evidence.get('domains'))) if domain]
    for email in emails:
        domain = email_domain(email)
        if domain and not _is_public_domain(domain):
            domains.append(domain)
    domains = list(dict.fromkeys(domains))
    thread_id = str(evidence.get('thread_id') or '').strip()
    source = str(evidence.get('source') or '').strip()
    external_id = str(evidence.get('external_id') or '').strip()

    bucket: dict[int, list[str]] = {}
    contact_index = _contact_email_index(conn)
    contact_id = None

    for email in emails:
        for customer_id in _fact_customers(conn, 'email', email):
            _add_method(bucket, customer_id, 'confirmed_email')
        hits = contact_index.get(email) or {}
        for customer_id, contact in hits.items():
            _add_method(bucket, customer_id, 'contact_email')
            if contact_id is None and len(hits) == 1:
                contact_id = contact.get('id')

    if thread_id:
        for customer_id in _fact_customers(conn, 'thread', thread_id):
            _add_method(bucket, customer_id, 'confirmed_thread')
        for customer_id in _thread_history_customers(conn, thread_id):
            _add_method(bucket, customer_id, 'thread_history')

    if source and external_id:
        for customer_id in _fact_customers(conn, 'source', _source_key(source, external_id)):
            _add_method(bucket, customer_id, 'confirmed_source')
        for customer_id in _external_identity_customers(conn, source, external_id):
            _add_method(bucket, customer_id, 'source_identity')

    trusted = evidence.get('customer_id')
    if trusted is not None:
        _add_method(bucket, int(trusted), 'trusted_source_customer')

    website_index = _website_domain_index(conn)
    for domain in domains:
        for customer_id in _fact_customers(conn, 'domain', domain):
            _add_method(bucket, customer_id, 'confirmed_domain')
        website_customers = website_index.get(domain) or set()
        if len(website_customers) == 1:
            _add_method(bucket, next(iter(website_customers)), 'website_domain')
        elif len(website_customers) > 1:
            # A shared website domain is not unique evidence.  Surface every
            # owner so the fact lands in Inbox as a real conflict instead of
            # being silently attributed to one of them.
            for customer_id in website_customers:
                _add_method(bucket, customer_id, 'website_domain_conflict')

    customer_ids = sorted(bucket)
    candidates = [
        {'customer_id': customer_id, 'methods': bucket[customer_id],
         'reason': _reason(bucket[customer_id])}
        for customer_id in customer_ids
    ]
    if len(customer_ids) == 1:
        methods = bucket[customer_ids[0]]
        return {
            'status': 'matched', 'customer_id': customer_ids[0], 'contact_id': contact_id,
            'methods': methods, 'reason': _reason(methods), 'candidates': candidates,
            'deterministic': True,
        }
    if customer_ids:
        return {
            'status': 'conflict', 'customer_id': None, 'contact_id': None,
            'methods': [], 'reason': '多个客户都有确定依据，需要人工判断',
            'candidates': candidates, 'deterministic': True,
        }
    return {
        'status': 'unmatched', 'customer_id': None, 'contact_id': None,
        'methods': [], 'reason': '', 'candidates': [], 'deterministic': True,
    }


def _reason(methods: Iterable[str]) -> str:
    labels = [METHOD_LABELS.get(method, method) for method in methods]
    return '、'.join(dict.fromkeys(labels))


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def evidence_from_capture(raw_content: Any, *, thread_id: str = '', source: str = '') -> dict:
    """Extract deterministic identity evidence from a stored Inbox capture body.

    Only explicit fields are read.  No name/signature inference happens here.
    """
    try:
        payload = json.loads(raw_content or '{}')
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    emails: list[str] = []
    texts: list[str] = [str(payload.get('conversation_identity') or ''),
                        str(payload.get('email') or ''), str(payload.get('sender') or ''),
                        str(payload.get('from') or '')]
    for message in _as_list(payload.get('messages')):
        if isinstance(message, dict):
            texts.append(str(message.get('sender') or ''))
            texts.append(str(message.get('from') or ''))
    for text in texts:
        for match in _EMAIL_RE.findall(text):
            email = normalize_email(match)
            if email:
                emails.append(email)
    resolved_thread = thread_id or str(payload.get('thread_id') or '').strip()
    resolved_source = source or str(payload.get('provider') or payload.get('channel') or '').strip()
    message_id = str(payload.get('message_id') or payload.get('id') or '').strip()
    evidence: dict[str, Any] = {'emails': list(dict.fromkeys(emails)), 'thread_id': resolved_thread}
    if resolved_source and message_id and resolved_source.casefold() in ('gmail',):
        evidence['source'] = resolved_source
        evidence['external_id'] = message_id
    return evidence
