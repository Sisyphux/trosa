"""Seed Sela prospects into an isolated PostgreSQL rehearsal database through the real POST API.

Only ever talks to the loopback rehearsal cluster (tools/postgres_rehearsal.py start).
Creates `sela524-NNNNN` prospects: about 70% have a contact, a third have a Gmail draft.

    .venv/bin/python docs/repro/sela-prospects-524/seed_prospects.py --count 6000

Then run enrich_prospects.py to add verification rows, replies, bounces, do-not-contact
prospects, extra contacts, real customer interactions and soft-deleted customers.
"""
import argparse
import importlib.util
import os
import random
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_DSN = 'postgresql://luoxin@127.0.0.1:55471/trosa_rehearsal'
TREE = Path(os.environ.get('SELA524_TREE') or Path(__file__).resolve().parents[3])


def configure_environment():
    dsn = os.environ.get('TROSA_REHEARSAL_DATABASE_URL', DEFAULT_DSN).strip()
    parsed = urlparse(dsn)
    if parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise SystemExit('refusing a non-loopback DSN for seeding')
    os.environ.update({
        'TRADE_OS_DATA_BACKEND': 'postgres',
        'TRADE_OS_DATABASE_URL': dsn,
        'TROSA_REHEARSAL': '1',
        'TROSA_REHEARSAL_DATABASE_URL': dsn,
        'TROSA_REHEARSAL_PORT': str(parsed.port or 55432),
        'TROSA_REHEARSAL_DB': 'trosa_rehearsal',
        'CRM_DB_PATH': os.path.join(tempfile.mkdtemp(prefix='sela524-seed-'), 'db'),
    })
    os.makedirs(os.environ['CRM_DB_PATH'], exist_ok=True)
    sys.path.insert(0, str(TREE))


def load_app():
    spec = importlib.util.spec_from_file_location('sela524_seed_app', str(TREE / 'app.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    return module


def prospect(i):
    sid = f'sela524-{i:05d}'
    rnd = random.Random(i)
    has_contact = i % 10 < 7
    has_draft = i % 3 == 0
    return {
        'source_id': sid,
        'company': f'Prospect Co {i:05d}',
        'country': rnd.choice(['Brazil', 'Mexico', 'Germany', 'Japan', 'USA']),
        'website': f'https://{sid}.example/',
        'business_type': 'Acrylic sheet fabricator',
        'campaign': 'bulk-524',
        'source_run': 'run-524',
        'status': 'READY TO CONTACT',
        'research_status': 'VERIFIED',
        'confidence': rnd.choice(['HIGH', 'MEDIUM']),
        'reason': 'Public fabrication evidence is present.',
        'angle': 'Reliable PMMA sheet supply',
        'source_urls': [f'https://{sid}.example/about', f'https://{sid}.example/contact'],
        'evidence': [{'type': 'website', 'text': 'Fabricates acrylic displays.',
                      'source_url': f'https://{sid}.example/about'}],
        'contact': ({'name': f'Person {i:05d}', 'title': 'Purchasing',
                     'email': f'buyer{i:05d}@{sid}.example', 'is_primary': 1} if has_contact else {}),
        'email': f'buyer{i:05d}@{sid}.example' if has_contact else '',
        'email_source_url': f'https://{sid}.example/contact',
        'email_type': 'person',
        'outreach_status': 'GMAIL_DRAFTED' if has_draft else '',
        'subject': 'Acrylic sheet supply' if has_draft else '',
        'email_draft': 'Hello, we can support acrylic sheet sourcing.' if has_draft else '',
        'gmail_draft_id': f'draft-{i}' if has_draft else '',
        'gmail_thread_id': f'thread-{i}' if has_draft else '',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--count', type=int, required=True)
    parser.add_argument('--start', type=int, default=0)
    args = parser.parse_args()
    configure_environment()
    client = load_app().app.test_client()
    assert client.post('/api/auth/login', json={'user': 'hamid'}).status_code == 200
    began = time.time()
    failures = 0
    for i in range(args.start, args.start + args.count):
        body = prospect(i)
        response = client.post('/api/integrations/sela/prospects',
                               json={'prospect': body, 'idempotency_key': f'seed-524:{body["source_id"]}'})
        if response.status_code != 200:
            failures += 1
            if failures <= 5:
                print('FAIL', i, response.status_code, response.get_data(as_text=True)[:300], flush=True)
        if (i - args.start + 1) % 500 == 0:
            print(f'seeded {i - args.start + 1} in {time.time() - began:.1f}s', flush=True)
    print(f'done count={args.count} start={args.start} failures={failures} elapsed={time.time() - began:.1f}s',
          flush=True)


if __name__ == '__main__':
    main()
