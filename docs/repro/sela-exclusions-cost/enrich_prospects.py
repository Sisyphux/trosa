"""Enrich the seeded sela524 prospects so the measurement covers every projection branch.

Loopback rehearsal database only. Adds: email verification rows (one per address, with
likely_deliverable / domain_does_not_accept_mail / invalid_domain / unverified), replies and
bounces on outreach, do-not-contact
suppression, extra contacts, real customer interactions (inbound reply text) and a few
soft-deleted customers (the view returns nothing for those).

    .venv/bin/python docs/repro/sela-prospects-524/enrich_prospects.py
"""
import collections
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import psycopg

DEFAULT_DSN = 'postgresql://luoxin@127.0.0.1:55471/trosa_rehearsal'
TREE = Path(os.environ.get('SELA524_TREE') or Path(__file__).resolve().parents[3])


def configure_environment():
    dsn = os.environ.get('TROSA_REHEARSAL_DATABASE_URL', DEFAULT_DSN).strip()
    parsed = urlparse(dsn)
    if parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise SystemExit('refusing a non-loopback DSN for enrichment')
    os.environ.update({
        'TRADE_OS_DATA_BACKEND': 'postgres',
        'TRADE_OS_DATABASE_URL': dsn,
        'TROSA_REHEARSAL': '1',
        'TROSA_REHEARSAL_DATABASE_URL': dsn,
        'TROSA_REHEARSAL_PORT': str(parsed.port or 55432),
        'TROSA_REHEARSAL_DB': 'trosa_rehearsal',
        'CRM_DB_PATH': os.path.join(tempfile.mkdtemp(prefix='sela524-enrich-'), 'db'),
    })
    os.makedirs(os.environ['CRM_DB_PATH'], exist_ok=True)
    sys.path.insert(0, str(TREE))
    return dsn


def load_app():
    spec = importlib.util.spec_from_file_location('sela524_enrich_app', str(TREE / 'app.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    return module


def main():
    dsn = configure_environment()
    counts = collections.Counter()
    with psycopg.connect(dsn) as conn:
        profiles = conn.execute(
            "SELECT id, organization_id, legacy_user_id, source_id, customer_id FROM trosa.agent_prospect_profiles "
            "WHERE source_id LIKE 'sela524-%' ORDER BY id").fetchall()
        for row_id, org, user, source_id, _customer_id in profiles:
            i = int(source_id.split('-')[1])
            has_contact = i % 10 < 7
            email = f'buyer{i:05d}@{source_id}.example'
            if has_contact:
                # The table allows one verification per (organization, user, normalized
                # email), so each address gets at most one status; a quarter stay unverified.
                branch = i % 4
                statuses = {0: ['likely_deliverable'], 1: ['domain_does_not_accept_mail'],
                            2: ['invalid_domain']}.get(branch, [])
                for status in statuses:
                    conn.execute(
                        '''INSERT INTO trosa.email_verifications
                             (organization_id, legacy_user_id, email, normalized_email, domain,
                              deliverability_status, confidence, address_type, risk_flags, evidence,
                              mx_records, checked_at, expires_at)
                           VALUES (%s, %s, %s, %s, %s, %s, 'high', 'person', '[]'::jsonb, '[]'::jsonb,
                                   '[]'::jsonb, '2026-10-10T09:00:00+08:00', '2026-12-10T09:00:00+08:00')''',
                        (org, user, email, email, f'{source_id}.example', status))
                    counts['verification_rows'] += 1
            if i % 30 == 0:
                cursor = conn.execute(
                    "UPDATE trosa.outreach_messages SET reply_status='replied', "
                    "reply_content='Thanks, we are interested. 意图: INTERESTED', reply_at=now() "
                    "WHERE legacy_payload->>'external_id'=%s", (source_id,))
                counts['replied'] += cursor.rowcount
            elif i % 30 == 15:
                cursor = conn.execute(
                    "UPDATE trosa.outreach_messages SET reply_status='bounced', "
                    "reply_content='Mail delivery failed: 550 user unknown', reply_at=now() "
                    "WHERE legacy_payload->>'external_id'=%s", (source_id,))
                counts['bounced'] += cursor.rowcount
            if i % 50 == 7:
                conn.execute(
                    "UPDATE trosa.agent_prospect_profiles SET contact_permission='do_not_contact', "
                    "suppression_reason='not a target buyer', suppression_at='2026-10-11 10:00:00' "
                    "WHERE id=%s", (row_id,))
                counts['do_not_contact'] += 1
        conn.commit()

    client = load_app().app.test_client()
    assert client.post('/api/auth/login', json={'user': 'hamid'}).status_code == 200
    with psycopg.connect(dsn) as conn:
        profiles = conn.execute(
            "SELECT source_id, customer_id FROM trosa.agent_prospect_profiles "
            "WHERE source_id LIKE 'sela524-%' ORDER BY id").fetchall()
    for source_id, customer_id in profiles:
        i = int(source_id.split('-')[1])
        if i % 10 < 7 and i % 5 == 0:
            response = client.post(f'/api/customers/{customer_id}/contacts', json={
                'name': f'Alt Buyer {i:05d}', 'title': 'Sales', 'email': f'alt{i:05d}@{source_id}.example',
                'is_primary': 0})
            counts[f'extra_contact_{response.status_code}'] += 1
        if i % 40 == 20:
            response = client.post(f'/api/customers/{customer_id}/follow_history', json={
                'content': '我们想要报价，目前价格太高，希望给个目标价', 'direction': 'inbound',
                'activity_type': 'customer_reply', 'follow_date': '2026-10-05', 'source': 'manual'})
            counts[f'real_interaction_{response.status_code}'] += 1
        if i % 97 == 5:
            response = client.delete(f'/api/customers/{customer_id}')
            counts[f'deleted_customer_{response.status_code}'] += 1
    print(json.dumps(dict(counts), ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
