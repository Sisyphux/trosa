"""Byte-for-byte check of GET /api/integrations/sela/exclusions on SQLite (development path).

    sqlite_parity.py --tree <tree> --db-dir <tmp dir> --out a.json --seed   # seeds, then reads
    sqlite_parity.py --tree <other tree> --db-dir <same dir> --out b.json   # reads only
    cmp a.json b.json

``generated_at`` is the only volatile field and is removed. Never point --db-dir at data/.
"""
import argparse
import importlib.util
import json
import os
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tree', required=True)
    ap.add_argument('--db-dir', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--seed', action='store_true')
    args = ap.parse_args()
    tree = os.path.abspath(args.tree)
    os.environ['CRM_DB_PATH'] = os.path.abspath(args.db_dir)
    os.environ.pop('CRM_SEED_DEMO_DATA', None)
    sys.path.insert(0, tree)
    import db
    db.init_all_dbs()
    spec = importlib.util.spec_from_file_location('parity_app', os.path.join(tree, 'app.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_a, **_k: None
    if args.seed:
        db.set_db_user('hamid')
        conn = db.get_db()
        for n in range(1, 61):
            cid = conn.execute(
                "INSERT INTO customers (name, company, country, website, business_stage, updated_at) "
                "VALUES (?, ?, 'Brazil', ?, ?, ?)",
                (f'Buyer {n}', f'Parity Co {n}', f'https://www.parity{n}.example/path',
                 '' if n % 5 else '成交', f'2026-10-{(n % 28) + 1:02d} 10:00:00')).lastrowid
            if n % 2:
                conn.execute('INSERT INTO contacts (customer_id, name, email, is_primary) VALUES (?, ?, ?, 1)',
                             (cid, f'B{n}', f'b{n}@parity{n}.example'))
            if n % 3 == 0:
                conn.execute("INSERT INTO follow_up_logs (customer_id, content, follow_date, direction) "
                             "VALUES (?, 'quote', ?, 'outbound')", (cid, f'2026-09-{(n % 28) + 1:02d}'))
            if n % 4 == 0:
                conn.execute("INSERT INTO outreach_emails (customer_id, subject, sent_date, reply_status) "
                             "VALUES (?, 'Hi', ?, 'replied')", (cid, f'2026-10-{(n % 28) + 1:02d}'))
                conn.execute("INSERT INTO outreach_emails (customer_id, subject, sent_date, reply_status) "
                             "VALUES (?, 'Again', ?, 'pending')", (cid, f'2026-10-{((n + 3) % 28) + 1:02d}'))
            if n % 7 == 0:
                conn.execute("UPDATE customers SET is_deleted=1 WHERE id=?", (cid,))
            if n % 11 == 0:
                conn.execute(
                    "INSERT INTO agent_prospect_profiles (customer_id, legacy_user_id, source, source_id, "
                    "contact_permission, suppression_reason) VALUES (?, 'hamid', 'sela', ?, "
                    "'do_not_contact', 'not a buyer')", (cid, f'parity-{n}'))
        conn.execute(
            "INSERT INTO inbox_items (item_type, title, content, dedupe_key, status) VALUES "
            "('gmail_capture', 'mail', ?, 'cap:parity', 'open')",
            (json.dumps({'direction': 'inbound', 'channel': 'gmail', 'messages': [
                {'direction': 'inbound', 'sender': 'X <b5@parity5.example>', 'text': 'Hello'}]}),))
        conn.commit()
        conn.close()
    client = module.app.test_client()
    assert client.post('/api/auth/login', json={'user': 'hamid'}).status_code == 200
    response = client.get('/api/integrations/sela/exclusions')
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    body.pop('generated_at')
    with open(args.out, 'w', encoding='utf-8') as handle:
        json.dump(body, handle, ensure_ascii=False, sort_keys=True, indent=1)
    print('records', len(body['records']), 'data_version', body['data_version'][:16])


if __name__ == '__main__':
    main()
