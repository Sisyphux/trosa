"""Measure GET /api/integrations/sela/exclusions against an isolated PostgreSQL rehearsal DB.

Records per call: wall time, SQL statements (by table), connections, body bytes, and a sha256
of the body with the volatile ``generated_at`` removed, so two trees can be compared byte for
byte on the same database snapshot. Also measures If-None-Match requests (304).

    .venv/bin/python docs/repro/sela-exclusions-cost/measure_exclusions.py \
        --tree /path/to/tree --label before --out docs/repro/sela-exclusions-cost

Loopback rehearsal DSN only (TROSA_REHEARSAL_DATABASE_URL). Never ECS.
"""
import argparse
import collections
import hashlib
import importlib.util
import json
import os
import re
import statistics
import sys
import tempfile
import time
from urllib.parse import urlparse

DEFAULT_DSN = 'postgresql://luoxin@127.0.0.1:55471/trosa_rehearsal'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tree', required=True)
    ap.add_argument('--label', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--calls', type=int, default=10)
    ap.add_argument('--body-out', default=None, help='write the normalized body here for cmp')
    args = ap.parse_args()
    dsn = os.environ.get('TROSA_REHEARSAL_DATABASE_URL', DEFAULT_DSN).strip()
    parsed = urlparse(dsn)
    if parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise SystemExit('refusing a non-loopback DSN')
    os.environ.update({
        'TRADE_OS_DATA_BACKEND': 'postgres', 'TRADE_OS_DATABASE_URL': dsn, 'TROSA_REHEARSAL': '1',
        'TROSA_REHEARSAL_DATABASE_URL': dsn, 'TROSA_REHEARSAL_PORT': str(parsed.port or 55432),
        'TROSA_REHEARSAL_DB': 'trosa_rehearsal',
        'CRM_DB_PATH': os.path.join(tempfile.mkdtemp(prefix='excl-measure-'), 'db'),
    })
    os.makedirs(os.environ['CRM_DB_PATH'], exist_ok=True)
    tree = os.path.abspath(args.tree)
    sys.path.insert(0, tree)

    import postgres_compat
    stats = {'sql': 0, 'connects': 0}
    shapes = collections.Counter()
    orig_exec, orig_connect = postgres_compat.CompatCursor.execute, postgres_compat.connect

    def counted_execute(self, sql, params=None):
        stats['sql'] += 1
        m = re.search(r'\b(?:FROM|INTO|UPDATE)\s+([A-Za-z_][\w\.]*)', str(sql), re.I)
        shapes[m.group(1) if m else ' '.join(str(sql).split())[:40]] += 1
        return orig_exec(self, sql, params)

    def counted_connect(user=None):
        stats['connects'] += 1
        return orig_connect(user)

    postgres_compat.CompatCursor.execute = counted_execute
    postgres_compat.connect = counted_connect

    spec = importlib.util.spec_from_file_location('excl_measure_app', os.path.join(tree, 'app.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    client = module.app.test_client()
    if client.post('/api/auth/login', json={'user': 'hamid'}).status_code != 200:
        raise SystemExit('login failed')

    def call(url, headers=None):
        shapes.clear()
        s0 = stats['sql']
        c0 = stats['connects']
        t = time.perf_counter()
        r = client.get(url, headers=headers or {})
        dt = time.perf_counter() - t
        return r, dt, stats['sql'] - s0, stats['connects'] - c0, list(shapes.most_common(8))

    runs, digest, body0 = [], None, None
    for _ in range(args.calls):
        r, dt, sql, conns, top = call('/api/integrations/sela/exclusions')
        assert r.status_code == 200, r.status_code
        raw = r.get_data()
        body = json.loads(raw)
        body.pop('generated_at', None)
        norm = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        digest = hashlib.sha256(norm).hexdigest()
        body0 = body0 or body
        runs.append({'seconds': round(dt, 4), 'sql': sql, 'connections': conns, 'bytes': len(raw),
                     'etag': r.headers.get('ETag'), 'top_tables': top})
    limit_r, _, _, _, _ = call('/api/integrations/sela/exclusions?limit=100')
    etag = runs[0]['etag']
    r, dt, sql, conns, top = call('/api/integrations/sela/exclusions', {'If-None-Match': etag})
    cond = {'status': r.status_code, 'body_bytes': len(r.get_data()), 'seconds': round(dt, 4), 'sql': sql}
    secs = [x['seconds'] for x in runs]
    summary = {
        'label': args.label, 'tree': tree, 'records': len(body0['records']),
        'customers_in_db': None,
        'median_seconds': round(statistics.median(secs), 4), 'max_seconds': max(secs),
        'sql_per_call': sorted({x['sql'] for x in runs}), 'bytes': runs[0]['bytes'],
        'limit100_bytes': len(limit_r.get_data()),
        'body_sha256_without_generated_at': digest, 'conditional_request': cond,
        'data_version': body0['data_version'], 'top_tables': runs[0]['top_tables'],
    }
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, f'measure-{args.label}.json'), 'w') as fh:
        json.dump({'summary': summary, 'runs': runs}, fh, ensure_ascii=False, indent=2)
    if args.body_out:
        with open(args.body_out, 'w') as fh:
            json.dump(body0, fh, ensure_ascii=False, sort_keys=True, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
