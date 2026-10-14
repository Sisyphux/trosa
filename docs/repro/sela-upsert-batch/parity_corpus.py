"""Old-vs-new behaviour parity for the Sela prospect upsert / matcher on PostgreSQL.

    setup:  build a corpus of Customers (loopback rehearsal DB only)
    run:    run one tree against one database and dump every match decision and
            response body as canonical JSON

Typical use (see README.md): setup once, clone the database twice with
CREATE DATABASE ... TEMPLATE, run the old tree on clone A and the new tree on
clone B, then `cmp` the two outputs.

    parity_corpus.py setup --tree TREE --dsn DSN
    parity_corpus.py run   --tree TREE --dsn DSN --out FILE
"""
import argparse, importlib.util, json, os, sys, tempfile
from urllib.parse import urlparse


def boot(tree, dsn):
    u = urlparse(dsn)
    if u.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise SystemExit('refusing a non-loopback DSN')
    os.environ.update({
        'TRADE_OS_DATA_BACKEND': 'postgres', 'TRADE_OS_DATABASE_URL': dsn, 'TROSA_REHEARSAL': '1',
        'TROSA_REHEARSAL_DATABASE_URL': dsn, 'TROSA_REHEARSAL_PORT': str(u.port),
        'TROSA_REHEARSAL_DB': 'trosa_rehearsal',
        'CRM_DB_PATH': os.path.join(tempfile.mkdtemp(prefix='ub-parity-'), 'db'),
    })
    os.makedirs(os.environ['CRM_DB_PATH'], exist_ok=True)
    sys.path.insert(0, os.path.abspath(tree))
    spec = importlib.util.spec_from_file_location('ub_parity_app', os.path.join(tree, 'app.py'))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    mod.app.config.update(TESTING=True)
    mod._sela_now = lambda: '2026-10-14 12:00:00'  # same clock for both trees
    return mod


def customers_spec():
    """(key, customer values, [contacts])"""
    S = []
    def add(key, company, website='', contacts=(), **extra):
        S.append((key, {'name': company, 'company': company, 'website': website, 'country': 'Brazil',
                        'level': 'C', 'source': 'Manual', **extra}, list(contacts)))
    add('email', 'Exact Email Plastics', 'https://exact-email.example', [{'name': 'Eva', 'email': 'exact@exact-email.example', 'is_primary': 1}])
    add('phone', 'Phone Only Acrylics', 'https://phone-only.example', [{'name': 'Pat', 'phone': '+55 11 98888-7777', 'is_primary': 1}])
    add('whatsapp', 'Whatsapp Signs', 'https://whatsapp-signs.example', [{'name': 'Wes', 'whatsapp': '+49 170 1234567', 'is_primary': 1}])
    add('domain', 'Domain Holder Ltd', 'https://www.domainco.example/about')
    add('namenoweb', 'Same Name Trading Ltd')
    add('namewithweb', 'Plastic World ZA', 'https://plasticworld-za.example')
    add('multi_a', 'Multi A', 'https://multi-a.example', [{'name': 'A', 'email': 'multi@multi-a.example', 'is_primary': 1}])
    add('multi_b', 'Multi B', 'https://multi-b.example', [{'name': 'B', 'phone': '0044 20 7946 0958', 'is_primary': 1}])
    add('deleted', 'Deleted Customer Co', 'https://deleted-co.example', [{'name': 'D', 'email': 'gone@deleted-co.example', 'is_primary': 1}])
    add('extra', 'Many Contacts Inc', 'https://many-contacts.example', [
        {'name': 'First', 'email': 'first@many-contacts.example', 'is_primary': 1},
        {'name': 'Second', 'email': 'second@many-contacts.example', 'phone': '+1 555 0100', 'is_primary': 0},
        {'name': 'Third', 'email': 'Third.Person@Many-Contacts.example', 'is_primary': 0}])
    add('shared', 'Shared Account Co', 'https://shared-account.example', [{'name': 'S', 'email': 'sh@shared-account.example', 'is_primary': 1}])
    add('dup1', 'Dup Dom Alpha', 'https://dupdom.example/', [{'name': 'x', 'email': 'a@dupdom.example', 'is_primary': 1}])
    add('dup2', 'Dup Dom Beta', 'https://www.dupdom.example', [])
    add('amp', 'A & B Trading Co', '')
    add('unicode', 'Müller Kunststoffe GmbH', 'https://mueller-kunststoffe.example', [
        {'name': 'J', 'email': 'jürgen@müller.example', 'phone': '+٩٦٦ ٥٠ ١٢٣', 'is_primary': 1}])
    add('ext', 'External Owned', 'https://external-owned.example', external_source='sela:prospect', external_id='ext-1')
    return S


def prospect(sid, company, website='', email='', phone='', whatsapp='', **extra):
    contact = {k: v for k, v in (('name', 'Probe'), ('email', email), ('phone', phone), ('whatsapp', whatsapp)) if v}
    if not (email or phone or whatsapp):
        contact = {}
    body = {'source_id': sid, 'company': company, 'country': 'Brazil', 'website': website,
            'business_type': 'Acrylic fabricator', 'status': 'READY TO CONTACT', 'research_status': 'VERIFIED',
            'confidence': 'HIGH', 'reason': 'parity', 'source_urls': ['https://x.example/a'], 'contact': contact}
    if email:
        body['email'] = email
    body.update(extra)
    return body


def probes():
    P = []
    def add(name, body): P.append((name, body))
    add('exact_email', prospect('par-1', 'Brand New 1', 'https://new-1.example', email='EXACT@exact-email.example'))
    add('exact_phone_format', prospect('par-2', 'Brand New 2', 'https://new-2.example', phone='+5511988887777'))
    add('phone_without_plus', prospect('par-2b', 'Brand New 2b', 'https://new-2b.example', phone='55 11 98888 7777'))
    add('whatsapp_vs_phone', prospect('par-3', 'Brand New 3', '', whatsapp='+49 170 1234567'))
    add('same_domain', prospect('par-4', 'Different Words Here', 'https://DomainCo.example/contact'))
    add('same_name_no_web_on_customer', prospect('par-5', 'Same Name Trading Inc.', ''))
    add('same_name_other_domain', prospect('par-6', 'Plastic World ZA', 'https://plasticworld-au.example'))
    add('same_name_same_domain', prospect('par-7', 'Plastic World ZA (Pty)', 'https://plasticworld-za.example'))
    add('name_only_with_web_on_customer', prospect('par-8', 'Plastic World ZA', ''))
    add('multiple_hits', prospect('par-9', 'Multi X', 'https://multi-x.example', email='multi@multi-a.example', phone='+44 20 7946 0958'))
    add('deleted_customer_email', prospect('par-10', 'Resurrect Try', 'https://resurrect.example', email='gone@deleted-co.example'))
    add('additional_email_second', prospect('par-11', 'Brand New 11', '', email='second@many-contacts.example'))
    add('additional_email_case', prospect('par-12', 'Brand New 12', '', email='third.person@many-contacts.example'))
    add('additional_phone', prospect('par-13', 'Brand New 13', '', phone='+1 (555) 0100'))
    add('shared_account_email', prospect('par-14', 'Brand New 14', '', email='sh@shared-account.example'))
    add('shared_account_domain', prospect('par-15', 'Shared Words', 'https://shared-account.example'))
    add('canonical_duplicate_domain', prospect('par-16', 'Dup Dom Beta', 'https://dupdom.example'))
    add('duplicate_domain_other_name', prospect('par-17', 'Whatever', 'dupdom.example'))
    add('amp_name', prospect('par-18', 'A and B Trading', ''))
    add('unicode_email', prospect('par-19', 'Brand New 19', '', email='JÜRGEN@müller.example'))
    add('unicode_phone', prospect('par-20', 'Brand New 20', '', phone='+٩٦٦ ٥٠ ١٢٣'))
    add('unicode_name', prospect('par-21', 'MÜLLER Kunststoffe', ''))
    add('external_id_again_same_domain', prospect('ext-1', 'External Owned', 'https://external-owned.example'))
    add('external_id_other_domain', prospect('ext-1', 'External Owned', 'https://elsewhere.example'))
    add('external_id_with_other_email', prospect('ext-1', 'External Owned', '', email='exact@exact-email.example'))
    add('no_contact_no_web', prospect('par-22', 'Totally Unique Name Zeta', ''))
    add('brand_new_with_contact', prospect('par-23', 'Totally Unique Name Eta', 'https://eta-unique.example', email='eta@eta-unique.example'))
    add('percent_underscore_name', prospect('par-24', '100%_Pure Acrylic', ''))
    add('repeat_of_new', prospect('par-23', 'Totally Unique Name Eta', 'https://eta-unique.example', email='eta@eta-unique.example'))
    add('contact_only_email_same_company', prospect('par-25', 'Exact Email Plastics', 'https://exact-email.example', email='other@exact-email.example'))
    add('company_name_only_suffixes', prospect('par-26', 'Exact Email Plastics LLC', ''))
    return P


def setup(tree, dsn):
    m = boot(tree, dsn)
    import db
    from trosa_domain import create_contact
    db.set_db_user('hamid')
    conn = db.get_db()
    ids = {}
    for key, values, contacts in customers_spec():
        cid = int(m._create_customer_record(conn, values=values))
        ids[key] = cid
        for c in contacts:
            create_contact(conn, customer_id=cid, values={'is_primary': 0, **c}, created_at='2026-10-01 10:00:00')
    for i in range(150):  # noise
        m._create_customer_record(conn, values={'name': f'Noise Co {i:03d}', 'company': f'Noise Co {i:03d}',
                                                'website': f'https://noise-{i:03d}.example', 'country': 'Germany', 'level': 'C', 'source': 'Manual'})
    conn.commit()
    # one account shared by two legacy customers (every_owner)
    row = conn.execute('SELECT * FROM trosa.account_legacy_refs WHERE legacy_customer_id=?', (ids['shared'],)).fetchone()
    cols = [k for k in dict(row).keys()]
    new_id = max(ids.values()) + 100000
    values = dict(row); values['legacy_customer_id'] = new_id
    values = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in values.items()}
    conn.execute('INSERT INTO trosa.account_legacy_refs (' + ','.join(cols) + ') VALUES (' + ','.join('?::jsonb' if c == 'legacy_payload' else '?' for c in cols) + ')',
                 tuple(values[c] for c in cols))
    conn.commit()
    conn.close()
    c = m.app.test_client()
    assert c.post('/api/auth/login', json={'user': 'hamid'}).status_code == 200
    r = c.delete(f"/api/customers/{ids['deleted']}")
    print('delete deleted-customer:', r.status_code)
    print('ids', ids, 'shared second legacy id', new_id)


def run(tree, dsn, out):
    m = boot(tree, dsn)
    import db
    c = m.app.test_client()
    assert c.post('/api/auth/login', json={'user': 'hamid'}).status_code == 200
    db.set_db_user('hamid')
    result = []
    for name, body in probes():
        conn = db.get_db()
        try:
            matches, err = m._sela_match_customers(conn, {
                'candidate_id': body['source_id'], 'company': body['company'], 'website': body.get('website'),
                'contact': body.get('contact') or {}})
            decision = {'error': err, 'matches': [{'id': int(r['id']), 'matched_by': list(r.get('matched_by') or [])} for r in matches]}
        finally:
            conn.close()
        r = c.post('/api/integrations/sela/prospects', json={'prospect': body, 'idempotency_key': f'par:{name}'})
        result.append({'probe': name, 'decision': decision, 'status_code': r.status_code,
                       'body': json.loads(r.get_data(as_text=True))})
    json.dump(result, open(out, 'w'), ensure_ascii=False, sort_keys=True, indent=1)
    print('wrote', out, len(result), 'probes')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('mode', choices=['setup', 'run'])
    ap.add_argument('--tree', required=True); ap.add_argument('--dsn', required=True); ap.add_argument('--out')
    a = ap.parse_args()
    setup(a.tree, a.dsn) if a.mode == 'setup' else run(a.tree, a.dsn, a.out)
