"""Measure POST /api/integrations/sela/prospects (one upsert) on an isolated rehearsal DB.

Grows the customer table to each --sizes checkpoint (using the real POST API, so the
tree under test does the seeding), then times and counts SQL statements for a few probe
upserts: a brand-new prospect (no match, worst case) and a prospect that matches an
existing contact by exact email (REVIEW/link path).  Statements are counted at
CompatCursor.execute, as in the earlier sela-prospects-524 harness.

    TROSA_REHEARSAL_DATABASE_URL=postgresql://luoxin@127.0.0.1:55461/trosa_rehearsal \
    .venv/bin/python docs/repro/sela-upsert-batch/measure_upsert.py --tree . --label after \
        --out docs/repro/sela-upsert-batch --sizes 100,1000,6000

Never point this at ECS: only a loopback DSN is accepted.
"""
import argparse, collections, importlib.util, json, os, re, statistics, sys, tempfile, time
from urllib.parse import urlparse

DEFAULT_DSN = 'postgresql://luoxin@127.0.0.1:55461/trosa_rehearsal'


def prospect(i, tag='ub'):
    sid = f'{tag}-{i:05d}'
    return {
        'source_id': sid, 'company': f'Upsert Co {tag} {i:05d}', 'country': 'Germany',
        'website': f'https://{sid}.example/', 'business_type': 'Acrylic sheet fabricator',
        'campaign': 'bulk-upsert', 'source_run': 'run-upsert', 'status': 'READY TO CONTACT',
        'research_status': 'VERIFIED', 'confidence': 'HIGH',
        'reason': 'Public fabrication evidence is present.', 'angle': 'Reliable PMMA sheet supply',
        'source_urls': [f'https://{sid}.example/about'],
        'evidence': [{'type': 'website', 'text': 'Fabricates acrylic displays.',
                      'source_url': f'https://{sid}.example/about'}],
        'contact': ({'name': f'Person {i:05d}', 'title': 'Purchasing',
                     'email': f'buyer{i:05d}@{sid}.example', 'is_primary': 1} if i % 10 < 7 else {}),
        'email': f'buyer{i:05d}@{sid}.example' if i % 10 < 7 else '',
        'email_source_url': f'https://{sid}.example/contact', 'email_type': 'person',
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--tree', required=True); ap.add_argument('--label', required=True)
    ap.add_argument('--out', required=True); ap.add_argument('--sizes', default='100,1000,6000')
    ap.add_argument('--probes', type=int, default=5)
    args = ap.parse_args()
    dsn = os.environ.get('TROSA_REHEARSAL_DATABASE_URL', DEFAULT_DSN).strip()
    u = urlparse(dsn)
    if u.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise SystemExit('refusing a non-loopback DSN')
    os.environ.update({
        'TRADE_OS_DATA_BACKEND': 'postgres', 'TRADE_OS_DATABASE_URL': dsn, 'TROSA_REHEARSAL': '1',
        'TROSA_REHEARSAL_DATABASE_URL': dsn, 'TROSA_REHEARSAL_PORT': str(u.port or 55432),
        'TROSA_REHEARSAL_DB': 'trosa_rehearsal',
        'CRM_DB_PATH': os.path.join(tempfile.mkdtemp(prefix='ub-measure-'), 'db'),
    })
    os.makedirs(os.environ['CRM_DB_PATH'], exist_ok=True)
    tree = os.path.abspath(args.tree); sys.path.insert(0, tree)
    import postgres_compat
    stats = {'sql': 0}; shapes = collections.Counter()
    orig = postgres_compat.CompatCursor.execute
    def counted(self, sql, params=None):
        stats['sql'] += 1
        m = re.search(r'\b(?:FROM|INTO|UPDATE)\s+([A-Za-z_][\w\.]*)', str(sql), re.I)
        shapes[m.group(1) if m else ' '.join(str(sql).split())[:40]] += 1
        return orig(self, sql, params)
    postgres_compat.CompatCursor.execute = counted
    spec = importlib.util.spec_from_file_location('ub_app', os.path.join(tree, 'app.py'))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    mod.app.config.update(TESTING=True)
    client = mod.app.test_client()
    assert client.post('/api/auth/login', json={'user': 'hamid'}).status_code == 200

    def count_customers():
        with postgres_compat.connect() as c:  # noqa
            pass
    def customers():
        r = client.get('/api/integrations/sela/prospects?after=0&limit=1')
        return r
    def post(body, key):
        began = time.perf_counter(); before = stats['sql']; shapes.clear()
        r = client.post('/api/integrations/sela/prospects', json={'prospect': body, 'idempotency_key': key})
        return r, time.perf_counter() - began, stats['sql'] - before, dict(shapes)

    def total_customers():
        r = client.get('/api/customers?limit=1')
        j = r.get_json() or {}
        for k in ('total', 'count'):
            if k in j: return j[k]
        return None

    sizes = [int(x) for x in args.sizes.split(',')]
    nxt = int(os.environ.get('UB_SEED_FROM', '0')); results = []
    for size in sizes:
        t0 = time.time()
        while nxt < size:
            r, *_ = post(prospect(nxt), f'ub-seed:{nxt}')
            if r.status_code != 200: raise SystemExit(f'seed failed {nxt}: {r.get_data(as_text=True)[:300]}')
            nxt += 1
        probes = []
        for k in range(args.probes):
            n = 900000 + size * 10 + k
            r, sec, sql, sh = post(prospect(n, 'probe'), f'ub-probe-{args.label}:{size}:{k}')
            probes.append({'kind': 'new', 'status': r.status_code, 'seconds': round(sec, 4), 'sql': sql,
                           'top_tables': sorted(sh.items(), key=lambda kv: -kv[1])[:8]})
        # exact-email match against an existing seeded customer, different source id
        for k in range(args.probes):
            tgt = (k * 7) % 7 if False else k
            body = prospect(size + 500000 + k, 'match'); em = f'buyer{k * 10 + 1:05d}@ub-{k * 10 + 1:05d}.example'
            body['contact'] = {'name': 'X', 'email': em}; body['email'] = em
            r, sec, sql, sh = post(body, f'ub-match-{args.label}:{size}:{k}')
            probes.append({'kind': 'email_match', 'status': r.status_code, 'seconds': round(sec, 4), 'sql': sql,
                           'body_status': (r.get_json() or {}).get('status')})
        new = [p for p in probes if p['kind'] == 'new']
        results.append({'customers_at_least': size, 'probes': probes,
                        'sql_new': sorted({p['sql'] for p in new}),
                        'median_seconds_new': round(statistics.median(p['seconds'] for p in new), 4),
                        'max_seconds_new': round(max(p['seconds'] for p in new), 4),
                        'last_new_top_tables': new[-1]['top_tables']})
        print(size, 'sql', results[-1]['sql_new'], 'median', results[-1]['median_seconds_new'],
              'max', results[-1]['max_seconds_new'], flush=True)
    os.makedirs(args.out, exist_ok=True)
    json.dump({'label': args.label, 'tree': tree, 'results': results},
              open(os.path.join(args.out, f'measure-upsert-{args.label}.json'), 'w'), indent=2)

if __name__ == '__main__':
    main()
