"""Give the seeded rehearsal customers external-contact evidence for the exclusions feed.

seed_prospects.py + enrich_prospects.py create prospects (cold), replies, bounces,
do-not-contact rows and soft-deleted customers. The exclusions snapshot only lists Customers
with a durable contact fact, so this adds, through the real HTTP API:

  * a recorded communication for every 3rd prospect customer (i % 3 == 1)  (~2,000)
  * an open inbound Gmail capture for every 40th prospect that has a contact email

Loopback rehearsal DSN only.
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


def main():
    dsn = os.environ.get('TROSA_REHEARSAL_DATABASE_URL', DEFAULT_DSN).strip()
    parsed = urlparse(dsn)
    if parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise SystemExit('refusing a non-loopback DSN')
    os.environ.update({
        'TRADE_OS_DATA_BACKEND': 'postgres', 'TRADE_OS_DATABASE_URL': dsn, 'TROSA_REHEARSAL': '1',
        'TROSA_REHEARSAL_DATABASE_URL': dsn, 'TROSA_REHEARSAL_PORT': str(parsed.port or 55432),
        'TROSA_REHEARSAL_DB': 'trosa_rehearsal',
        'CRM_DB_PATH': os.path.join(tempfile.mkdtemp(prefix='excl-enrich-'), 'db'),
    })
    os.makedirs(os.environ['CRM_DB_PATH'], exist_ok=True)
    sys.path.insert(0, str(TREE))
    spec = importlib.util.spec_from_file_location('excl_enrich_app', str(TREE / 'app.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    client = module.app.test_client()
    assert client.post('/api/auth/login', json={'user': 'hamid'}).status_code == 200
    with psycopg.connect(dsn) as conn:
        profiles = conn.execute(
            "SELECT source_id, customer_id FROM trosa.agent_prospect_profiles "
            "WHERE source_id LIKE 'sela524-%' ORDER BY id").fetchall()
    counts = collections.Counter()
    for source_id, customer_id in profiles:
        i = int(source_id.split('-')[1])
        if i % 3 == 1:
            r = client.post(f'/api/customers/{customer_id}/follow_history', json={
                'activity_content': '已发送报价并跟进', 'direction': 'outbound',
                'follow_date': '2026-10-03'})
            counts[f'communication_{r.status_code}'] += 1
        if i % 40 == 2 and i % 10 < 7:
            conn_db = module.get_db()
            try:
                module._create_inbox_item(
                    conn_db, item_type='gmail_capture', title='来信',
                    content=json.dumps({'direction': 'inbound', 'channel': 'gmail', 'messages': [
                        {'direction': 'inbound', 'sender': f'Buyer <buyer{i:05d}@{source_id}.example>',
                         'text': 'Hello, please send the price list.'}]}),
                    dedupe_key=f'excl-capture:{source_id}', status='open',
                    created_at='2026-10-12 10:00:00')
                conn_db.commit()
                counts['capture'] += 1
            finally:
                conn_db.close()
    print(json.dumps(dict(counts), sort_keys=True))


if __name__ == '__main__':
    main()
