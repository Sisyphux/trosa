#!/usr/bin/env python3
"""Seed follow-up suggestions into an isolated dev database for browser reproductions.

Reads a JSON list from stdin: ``[{"customer_id": 1, "rule": "R1", "reason": "...", "priority": 1}]``.
Each suggestion hangs on the customer's newest record.   Dev SQLite only.

    echo '[...]' | .venv/bin/python tools/repro_seed_followups.py --app-root . --db-dir <scratch>
"""
import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app-root', required=True)
    parser.add_argument('--db-dir', required=True)
    parser.add_argument('--user', default='hamid')
    args = parser.parse_args()
    root = Path(args.app_root).resolve()
    db_dir = Path(args.db_dir).expanduser().resolve()
    if root / 'data' == db_dir or (root / 'data') in db_dir.parents:
        raise SystemExit('refusing to seed a repository data folder')
    os.environ.update({'CRM_ENV': 'development', 'TRADE_OS_DEV_SQLITE': '1', 'TRADE_OS_DATA_BACKEND': 'sqlite'})
    os.environ.pop('TRADE_OS_DATABASE_URL', None)
    sys.path.insert(0, str(root))
    import db
    import followup_store as store
    from followup_candidates import Candidate

    db.DB_DIR = str(db_dir)
    db.set_db_user(args.user)
    conn = db.get_db()
    try:
        candidates = []
        for item in json.load(sys.stdin):
            row = conn.execute('SELECT id FROM follow_up_logs WHERE customer_id=? ORDER BY id DESC LIMIT 1',
                               (item['customer_id'],)).fetchone()
            evidence = str(row[0]) if row else None
            key = f"{args.user}:{item['customer_id']}:{item['rule']}:{evidence}"
            candidates.append(Candidate(
                account_id=str(item['customer_id']), rule=item['rule'], certainty='certain',
                evidence_event_id=evidence, evidence_event_ids=(evidence,) if evidence else (),
                facts={}, reason=item['reason'], owner=args.user,
                priority=int(item.get('priority', 1)), dedupe_key=key))
        counts = store.upsert_candidates(conn, candidates, date.today())
        conn.commit()
    finally:
        conn.close()
    print(json.dumps(counts))


if __name__ == '__main__':
    main()
