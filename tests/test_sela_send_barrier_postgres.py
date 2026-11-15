"""PostgreSQL acceptance specification for the proposed Sela send barrier.

Run only against the loopback rehearsal database. No Gmail calls are made.
The request shape here is a proposed contract, pending implementation.
"""
import os
import unittest
import uuid
from unittest import mock

from tests import test_postgres_rehearsal as rehearsal


class SelaSendBarrierPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = rehearsal._dsn()
        if not dsn or not rehearsal._loopback(dsn):
            raise unittest.SkipTest('requires the isolated PostgreSQL rehearsal database')
        import psycopg
        with psycopg.connect(dsn) as conn:
            conn.execute('SELECT 1')
        os.environ['TRADE_OS_DATA_BACKEND'] = 'postgres'
        os.environ['TRADE_OS_DATABASE_URL'] = dsn
        cls.module = rehearsal.PostgreSQLRehearsalAcceptanceTest._app_module()

    def test_stale_parent_org_cannot_bypass_armed_barrier(self):
        """A repeatable-read snapshot must not use a company's former org to bypass the freeze."""
        import psycopg

        for kind in ('alias', 'domain'):
            with self.subTest(kind=kind):
                org_a, owner_a, company, account, child, send_id = (
                    uuid.uuid4() for _ in range(6))
                unique = uuid.uuid4().hex
                stale = None
                org_b = None
                try:
                    with psycopg.connect(rehearsal._dsn()) as conn:
                        org_b = conn.execute('SELECT trosa.compat_org_id()').fetchone()[0]
                        owner_b = conn.execute(
                            "SELECT id FROM identity.users WHERE organization_id=%s AND legacy_user_id='hamid'",
                            (org_b,)).fetchone()[0]
                        conn.execute('INSERT INTO identity.organizations (id, name) VALUES (%s,%s)',
                                     (org_a, 'Stale barrier ' + unique))
                        conn.execute(
                            'INSERT INTO identity.users (id, organization_id, legacy_user_id, display_name) VALUES (%s,%s,%s,%s)',
                            (owner_a, org_a, 'stale_' + unique, 'Stale barrier owner'))
                        conn.execute(
                            'INSERT INTO core.companies (id, organization_id, canonical_name, normalized_name) VALUES (%s,%s,%s,%s)',
                            (company, org_a, 'Stale ' + unique, 'stale ' + unique))
                        conn.execute(
                            'INSERT INTO trosa.accounts (id, organization_id, company_id, owner_user_id) VALUES (%s,%s,%s,%s)',
                            (account, org_a, company, owner_a))

                    stale = psycopg.connect(rehearsal._dsn())
                    stale.execute('BEGIN ISOLATION LEVEL REPEATABLE READ')
                    self.assertEqual(stale.execute(
                        'SELECT organization_id FROM core.companies WHERE id=%s', (company,)
                    ).fetchone()[0], org_a)
                    with psycopg.connect(rehearsal._dsn()) as mover:
                        mover.execute('UPDATE core.companies SET organization_id=%s WHERE id=%s',
                                      (org_b, company))
                        mover.execute(
                            'UPDATE trosa.accounts SET organization_id=%s, owner_user_id=%s WHERE id=%s',
                            (org_b, owner_b, account))
                    with psycopg.connect(rehearsal._dsn()) as conn:
                        conn.execute("SELECT set_config('trosa.sela_send_barrier_operation', 'prepare', true)")
                        conn.execute(
                            "INSERT INTO audit.sela_send_barriers "
                            "(organization_id, send_id, requested_by, identity_hash, recipient_hash, token, state, expires_at) "
                            "VALUES (%s,%s,%s,%s,%s,%s,'PREPARED',now()+interval '1 hour')",
                            (org_b, send_id, 'rehearsal', unique, unique, unique))
                        conn.execute("SELECT set_config('trosa.sela_send_barrier_operation', 'arm', true)")
                        conn.execute("UPDATE audit.sela_send_barriers SET state='ARMED' WHERE organization_id=%s AND send_id=%s",
                                     (org_b, send_id))

                    if kind == 'alias':
                        sql = ('INSERT INTO core.company_aliases (id, company_id, alias, normalized_alias) '
                               'VALUES (%s,%s,%s,%s)')
                        params = (child, company, unique, unique)
                    else:
                        sql = ('INSERT INTO core.company_domains (id, company_id, normalized_domain) '
                               'VALUES (%s,%s,%s)')
                        params = (child, company, unique + '.example')
                    with self.assertRaises(psycopg.Error) as caught:
                        stale.execute(sql, params)
                    self.assertTrue(
                        caught.exception.sqlstate == '40001'
                        or 'SELA_SEND_BARRIER_ARMED' in str(caught.exception),
                        str(caught.exception))
                finally:
                    if stale is not None:
                        stale.rollback()
                        stale.close()
                    if org_b is not None:
                        with psycopg.connect(rehearsal._dsn()) as conn:
                            conn.execute("SELECT set_config('trosa.sela_send_barrier_operation', 'finalize', true)")
                            conn.execute("UPDATE audit.sela_send_barriers SET state='CONFIRMED_NOT_SENT' "
                                         "WHERE organization_id=%s AND send_id=%s", (org_b, send_id))
                            conn.execute('DELETE FROM core.company_aliases WHERE id=%s', (child,))
                            conn.execute('DELETE FROM core.company_domains WHERE id=%s', (child,))
                            conn.execute('DELETE FROM trosa.accounts WHERE id=%s', (account,))
                            conn.execute('DELETE FROM core.companies WHERE id=%s', (company,))
                            conn.execute('DELETE FROM identity.users WHERE id=%s', (owner_a,))
                            conn.execute('DELETE FROM identity.organizations WHERE id=%s', (org_a,))

    def test_expired_prepared_reissues_but_expired_armed_stays_locked(self):
        import psycopg
        client = self.module.app.test_client()
        headers = {'Authorization': 'Bearer rehearsal-only'}
        send_id = str(uuid.uuid4())
        company = uuid.uuid4()
        name = 'Reissue ' + uuid.uuid4().hex
        payload = {'send_id': send_id, 'name': name, 'recipient_hash': 'a' * 64}
        token = None
        with psycopg.connect(rehearsal._dsn()) as conn:
            org = conn.execute('SELECT trosa.compat_org_id()').fetchone()[0]
            conn.execute('INSERT INTO core.companies (id, organization_id, canonical_name, normalized_name) VALUES (%s,%s,%s,%s)', (company, org, name, name.lower()))

        def post(action, data):
            return client.post('/api/integrations/sela/send-barrier/' + action, headers=headers, json=data)

        try:
            with mock.patch.object(self.module, '_integration_token_matches', return_value=True):
                first = post('prepare', payload)
                self.assertEqual(first.status_code, 200, first.get_json())
                old_token = first.get_json()['token']
                with psycopg.connect(rehearsal._dsn()) as conn:
                    conn.execute("SELECT set_config('trosa.sela_send_barrier_operation', 'prepare', true)")
                    conn.execute("UPDATE audit.sela_send_barriers SET expires_at=now()-interval '1 second' WHERE send_id=%s", (send_id,))
                renewed = post('prepare', payload)
                self.assertEqual(renewed.status_code, 200, renewed.get_json())
                self.assertEqual(renewed.get_json()['state'], 'PREPARED')
                self.assertTrue(renewed.get_json()['fresh'])
                token = renewed.get_json()['token']
                self.assertNotEqual(token, old_token)
                self.assertEqual(post('arm', dict(payload, token=old_token)).status_code, 403)
                self.assertTrue(post('arm', dict(payload, token=token)).get_json()['acquired'])
                with psycopg.connect(rehearsal._dsn()) as conn:
                    conn.execute("UPDATE audit.sela_send_barriers SET expires_at=now()-interval '1 second' WHERE send_id=%s", (send_id,))
                armed = post('prepare', payload).get_json()
                self.assertEqual(armed['state'], 'ARMED')
                self.assertEqual(armed['token'], token)
                self.assertFalse(armed['fresh'])
                self.assertFalse(post('arm', dict(payload, token=token)).get_json()['acquired'])
                final = {'send_id': send_id, 'token': token, 'outcome': 'UNKNOWN'}
                self.assertEqual(post('finalize', final).get_json()['state'], 'ARMED')
                with psycopg.connect(rehearsal._dsn()) as conn:
                    with self.assertRaisesRegex(psycopg.Error, 'SELA_SEND_BARRIER_ARMED'):
                        conn.execute('UPDATE core.companies SET canonical_name=%s WHERE id=%s', ('Blocked', company))
                final['outcome'] = 'CONFIRMED_NOT_SENT'
                self.assertEqual(post('finalize', final).get_json()['state'], 'CONFIRMED_NOT_SENT')
                self.assertEqual(post('finalize', final).get_json()['state'], 'CONFIRMED_NOT_SENT')
                terminal = post('prepare', payload).get_json()
                self.assertEqual(terminal['state'], 'CONFIRMED_NOT_SENT')
                self.assertEqual(terminal['token'], token)
        finally:
            if token:
                with mock.patch.object(self.module, '_integration_token_matches', return_value=True):
                    post('finalize', {'send_id': send_id, 'token': token, 'outcome': 'CONFIRMED_NOT_SENT'})
            with psycopg.connect(rehearsal._dsn()) as conn:
                conn.execute('DELETE FROM core.companies WHERE id=%s', (company,))

    def test_unknown_outcome_keeps_manual_company_edit_blocked(self):
        """A lost provider response must keep even direct database writes blocked."""
        import psycopg
        client = self.module.app.test_client()
        headers = {'Authorization': 'Bearer rehearsal-only'}
        send_id = str(uuid.uuid4())
        company_id = uuid.uuid4()
        with psycopg.connect(rehearsal._dsn()) as conn:
            org = conn.execute('SELECT trosa.compat_org_id()').fetchone()[0]
            conn.execute(
                'INSERT INTO core.companies (id, organization_id, canonical_name, normalized_name) '
                'VALUES (%s, %s, %s, %s)', (company_id, org, 'Barrier Fixture', 'barrier fixture'))
            conn.commit()
        try:
            with mock.patch.object(self.module, '_integration_token_matches', return_value=True):
                prepare = client.post('/api/integrations/sela/send-barrier/prepare', headers=headers,
                    json={'send_id': send_id, 'name': 'Barrier Fixture',
                          'recipient_hash': 'a' * 64})
                self.assertEqual(prepare.status_code, 200, prepare.get_json())
                token = prepare.get_json()['token']
                arm = client.post('/api/integrations/sela/send-barrier/arm', headers=headers,
                    json={'send_id': send_id, 'token': token, 'name': 'Barrier Fixture', 'recipient_hash': 'a' * 64})
                self.assertEqual(arm.status_code, 200, arm.get_json())
                unknown = client.post('/api/integrations/sela/send-barrier/finalize', headers=headers,
                    json={'send_id': send_id, 'token': token, 'outcome': 'UNKNOWN'})
                self.assertEqual(unknown.get_json()['state'], 'ARMED')
            with psycopg.connect(rehearsal._dsn()) as conn:
                with self.assertRaises(psycopg.Error):
                    conn.execute('UPDATE core.companies SET canonical_name=%s WHERE id=%s',
                                 ('Changed manually', company_id))
        finally:
            with mock.patch.object(self.module, '_integration_token_matches', return_value=True):
                if 'token' in locals():
                    client.post('/api/integrations/sela/send-barrier/finalize', headers=headers,
                        json={'send_id': send_id, 'token': token, 'outcome': 'CONFIRMED_NOT_SENT'})
            with psycopg.connect(rehearsal._dsn()) as conn:
                conn.execute('DELETE FROM core.companies WHERE id=%s', (company_id,))


    def test_direct_sql_cannot_release_delete_or_truncate_an_armed_barrier(self):
        import psycopg
        client = self.module.app.test_client()
        headers = {'Authorization': 'Bearer rehearsal-only'}
        send_id = str(uuid.uuid4())
        company_id = uuid.uuid4()
        name = 'Direct Barrier Write ' + uuid.uuid4().hex
        token = None
        with psycopg.connect(rehearsal._dsn()) as conn:
            org = conn.execute('SELECT trosa.compat_org_id()').fetchone()[0]
            conn.execute(
                'INSERT INTO core.companies (id, organization_id, canonical_name, normalized_name) '
                'VALUES (%s, %s, %s, %s)', (company_id, org, name, name.lower()))
            conn.commit()
        def post(action, data):
            return client.post('/api/integrations/sela/send-barrier/' + action, headers=headers, json=data)
        try:
            with mock.patch.object(self.module, '_integration_token_matches', return_value=True):
                prepared = post('prepare', {'send_id': send_id, 'name': name, 'recipient_hash': 'd' * 64})
                self.assertEqual(prepared.status_code, 200, prepared.get_json())
                token = prepared.get_json()['token']
                armed = post('arm', {'send_id': send_id, 'token': token, 'name': name,
                                     'recipient_hash': 'd' * 64})
                self.assertTrue(armed.get_json()['acquired'], armed.get_json())
            operations = [
                ('release', "UPDATE audit.sela_send_barriers SET state='CONFIRMED_NOT_SENT' WHERE organization_id=%s AND send_id=%s",
                 (org, send_id)),
                ('delete', 'DELETE FROM audit.sela_send_barriers WHERE organization_id=%s AND send_id=%s',
                 (org, send_id)),
                ('truncate', 'TRUNCATE audit.sela_send_barriers', ()),
            ]
            for label, sql, params in operations:
                with self.subTest(operation=label), psycopg.connect(rehearsal._dsn()) as conn:
                    blocked = False
                    try:
                        conn.execute(sql, params)
                    except psycopg.Error as exc:
                        blocked = 'SELA_SEND_BARRIER' in str(exc)
                    finally:
                        conn.rollback()
                    self.assertTrue(blocked, f'direct {label} was not rejected')
            with psycopg.connect(rehearsal._dsn()) as conn:
                state = conn.execute(
                    'SELECT state FROM audit.sela_send_barriers WHERE organization_id=%s AND send_id=%s',
                    (org, send_id)).fetchone()[0]
                self.assertEqual(state, 'ARMED')
        finally:
            if token:
                with mock.patch.object(self.module, '_integration_token_matches', return_value=True):
                    post('finalize', {'send_id': send_id, 'token': token, 'outcome': 'CONFIRMED_NOT_SENT'})
            with psycopg.connect(rehearsal._dsn()) as conn:
                conn.execute('DELETE FROM core.companies WHERE id=%s', (company_id,))

    def test_lifecycle_idempotency_expiry_and_terminal_release(self):
        import psycopg
        client = self.module.app.test_client()
        headers = {'Authorization': 'Bearer rehearsal-only'}
        send_id = str(uuid.uuid4())
        name = 'Barrier ' + uuid.uuid4().hex
        company, account, alias, domain = (uuid.uuid4() for _ in range(4))
        with psycopg.connect(rehearsal._dsn()) as conn:
            org = conn.execute('SELECT trosa.compat_org_id()').fetchone()[0]
            conn.execute('INSERT INTO core.companies (id, organization_id, canonical_name, normalized_name) VALUES (%s,%s,%s,%s)', (company, org, name, name.lower()))
            conn.execute('INSERT INTO trosa.accounts (id, organization_id, company_id, owner_user_id) SELECT %s,%s,%s,id FROM identity.users WHERE organization_id=%s AND legacy_user_id=%s', (account, org, company, org, 'hamid'))
            conn.execute('INSERT INTO core.company_aliases (id, company_id, alias, normalized_alias) VALUES (%s,%s,%s,%s)', (alias, company, name + ' alias', name.lower() + ' alias'))
            conn.execute('INSERT INTO core.company_domains (id, company_id, normalized_domain) VALUES (%s,%s,%s)', (domain, company, uuid.uuid4().hex + '.example'))
        def post(action, data):
            return client.post('/api/integrations/sela/send-barrier/' + action, headers=headers, json=data)
        payload = {'send_id': send_id, 'name': name, 'recipient_hash': 'b' * 64}
        token = None
        try:
            with mock.patch.object(self.module, '_integration_token_matches', return_value=True):
                first = post('prepare', payload)
                self.assertEqual(first.status_code, 200, first.get_json())
                token = first.get_json()['token']
                self.assertEqual(post('prepare', payload).get_json()['token'], token)
                self.assertEqual(post('prepare', dict(payload, recipient_hash='c' * 64)).status_code, 409)
                arm_data = {'send_id': send_id, 'token': token, 'name': name, 'recipient_hash': 'b' * 64}
                self.assertTrue(post('arm', arm_data).get_json()['acquired'])
                self.assertFalse(post('arm', arm_data).get_json()['acquired'])
                with psycopg.connect(rehearsal._dsn()) as conn:
                    conn.execute("UPDATE audit.sela_send_barriers SET expires_at=now()-interval '1 hour' WHERE send_id=%s", (send_id,))
                self.assertEqual(post('finalize', {'send_id': send_id, 'token': token, 'outcome': 'UNKNOWN'}).get_json()['state'], 'ARMED')
                statements = [
                    ('UPDATE core.companies SET canonical_name=%s WHERE id=%s', ('Changed', company)),
                    ('DELETE FROM core.companies WHERE id=%s', (company,)),
                    ('UPDATE trosa.accounts SET owner_user_id=NULL WHERE id=%s', (account,)),
                    ('DELETE FROM trosa.accounts WHERE id=%s', (account,)),
                    ('UPDATE core.company_aliases SET alias=%s WHERE id=%s', ('changed', alias)),
                    ('DELETE FROM core.company_aliases WHERE id=%s', (alias,)),
                    ('TRUNCATE core.company_aliases', ()),
                    ('UPDATE core.company_domains SET normalized_domain=%s WHERE id=%s', ('changed.example', domain)),
                    ('DELETE FROM core.company_domains WHERE id=%s', (domain,)),
                    ('INSERT INTO core.company_aliases (id, company_id, alias, normalized_alias) VALUES (%s,%s,%s,%s)', (uuid.uuid4(), company, 'Alias', 'alias')),
                    ('INSERT INTO core.company_domains (id, company_id, normalized_domain) VALUES (%s,%s,%s)', (uuid.uuid4(), company, 'barrier.example')),
                    ('INSERT INTO trosa.accounts (id, organization_id, company_id, owner_user_id) SELECT %s,%s,%s,id FROM identity.users WHERE organization_id=%s LIMIT 1', (uuid.uuid4(), org, company, org)),
                ]
                for sql, args in statements:
                    with self.subTest(sql=sql), psycopg.connect(rehearsal._dsn()) as conn:
                        with self.assertRaisesRegex(psycopg.Error, 'SELA_SEND_BARRIER_ARMED'):
                            conn.execute(sql, args)
                final = {'send_id': send_id, 'token': token, 'outcome': 'SENT'}
                self.assertEqual(post('finalize', final).get_json()['state'], 'SENT')
                self.assertEqual(post('finalize', final).get_json()['state'], 'SENT')
                self.assertEqual(post('finalize', dict(final, outcome='CONFIRMED_NOT_SENT')).status_code, 409)
                with psycopg.connect(rehearsal._dsn()) as conn:
                    conn.execute('UPDATE core.companies SET canonical_name=%s WHERE id=%s', ('Released', company))
                self.assertEqual(post('arm', arm_data).status_code, 409)
        finally:
            if token:
                with mock.patch.object(self.module, '_integration_token_matches', return_value=True):
                    post('finalize', {'send_id': send_id, 'token': token, 'outcome': 'CONFIRMED_NOT_SENT'})
            with psycopg.connect(rehearsal._dsn()) as conn:
                conn.execute('DELETE FROM trosa.accounts WHERE id=%s', (account,))
                conn.execute('DELETE FROM core.company_aliases WHERE id=%s', (alias,))
                conn.execute('DELETE FROM core.company_domains WHERE id=%s', (domain,))
                conn.execute('DELETE FROM core.companies WHERE id=%s', (company,))

    def test_armed_barrier_blocks_owner_identity_changes_until_release(self):
        import psycopg
        client = self.module.app.test_client()
        headers = {'Authorization': 'Bearer rehearsal-only'}
        send_id = str(uuid.uuid4())
        name = 'Identity Barrier ' + uuid.uuid4().hex
        user_id = uuid.uuid4()
        username = 'barrier_' + uuid.uuid4().hex
        token = None
        with psycopg.connect(rehearsal._dsn()) as conn:
            org = conn.execute('SELECT trosa.compat_org_id()').fetchone()[0]
            conn.execute('INSERT INTO identity.users (id, organization_id, legacy_user_id, display_name) VALUES (%s,%s,%s,%s)',
                         (user_id, org, username, 'Barrier Owner'))
        def post(action, data):
            return client.post('/api/integrations/sela/send-barrier/' + action, headers=headers, json=data)
        try:
            with mock.patch.object(self.module, '_integration_token_matches', return_value=True):
                prepared = post('prepare', {'send_id': send_id, 'name': name, 'recipient_hash': 'e' * 64})
                self.assertEqual(prepared.status_code, 200, prepared.get_json())
                token = prepared.get_json()['token']
                armed = post('arm', {'send_id': send_id, 'token': token, 'name': name, 'recipient_hash': 'e' * 64})
                self.assertTrue(armed.get_json()['acquired'], armed.get_json())
                statements = [
                    ('UPDATE identity.users SET legacy_user_id=%s WHERE id=%s', (username + '_changed', user_id)),
                    ('UPDATE identity.users SET display_name=%s WHERE id=%s', ('Changed Owner', user_id)),
                    ('UPDATE identity.users SET id=%s WHERE id=%s', (uuid.uuid4(), user_id)),
                    ('UPDATE identity.users SET organization_id=organization_id WHERE id=%s', (user_id,)),
                    ('DELETE FROM identity.users WHERE id=%s', (user_id,)),
                    ('INSERT INTO identity.users (id, organization_id, legacy_user_id, display_name) VALUES (%s,%s,%s,%s)',
                     (uuid.uuid4(), org, username + '_new', 'New Owner')),
                    ('TRUNCATE identity.users CASCADE', ()),
                ]
                for sql, args in statements:
                    with self.subTest(sql=sql), psycopg.connect(rehearsal._dsn()) as conn:
                        with self.assertRaisesRegex(psycopg.Error, 'SELA_SEND_BARRIER_ARMED'):
                            conn.execute(sql, args)
                with psycopg.connect(rehearsal._dsn()) as conn:
                    conn.execute('UPDATE identity.users SET status=status WHERE id=%s', (user_id,))
                final = post('finalize', {'send_id': send_id, 'token': token, 'outcome': 'CONFIRMED_NOT_SENT'})
                self.assertEqual(final.get_json()['state'], 'CONFIRMED_NOT_SENT')
                with psycopg.connect(rehearsal._dsn()) as conn:
                    conn.execute('UPDATE identity.users SET display_name=%s WHERE id=%s', ('Released Owner', user_id))
        finally:
            if token:
                with mock.patch.object(self.module, '_integration_token_matches', return_value=True):
                    post('finalize', {'send_id': send_id, 'token': token, 'outcome': 'CONFIRMED_NOT_SENT'})
            with psycopg.connect(rehearsal._dsn()) as conn:
                conn.execute('DELETE FROM identity.users WHERE id=%s', (user_id,))

    def test_prepared_can_be_closed_only_when_sender_proves_gmail_was_not_started(self):
        """An ARM response may be lost before/after commit; no Gmail call was made yet."""
        import psycopg
        client = self.module.app.test_client()
        headers = {'Authorization': 'Bearer rehearsal-only'}
        send_id = str(uuid.uuid4())
        company_id = uuid.uuid4()
        name = 'Pre-send close ' + uuid.uuid4().hex
        token = None
        with psycopg.connect(rehearsal._dsn()) as conn:
            org = conn.execute('SELECT trosa.compat_org_id()').fetchone()[0]
            conn.execute(
                'INSERT INTO core.companies (id, organization_id, canonical_name, normalized_name) '
                'VALUES (%s, %s, %s, %s)', (company_id, org, name, name.lower()))
        def post(action, data):
            return client.post('/api/integrations/sela/send-barrier/' + action,
                               headers=headers, json=data)
        try:
            with mock.patch.object(self.module, '_integration_token_matches', return_value=True):
                prepared = post('prepare', {'send_id': send_id, 'name': name,
                                            'recipient_hash': '9' * 64})
                self.assertEqual(prepared.status_code, 200, prepared.get_json())
                token = prepared.get_json()['token']
                finalize = {'send_id': send_id, 'token': token,
                            'outcome': 'CONFIRMED_NOT_SENT'}
                refused = post('finalize', finalize)
                self.assertEqual(refused.status_code, 409, refused.get_json())
                finalize['send_not_started'] = True
                closed = post('finalize', finalize)
                self.assertEqual(closed.status_code, 200, closed.get_json())
                self.assertIs(closed.get_json()['success'], True)
                self.assertEqual(closed.get_json()['state'], 'CONFIRMED_NOT_SENT')
                replay = post('finalize', finalize)
                self.assertEqual(replay.status_code, 200, replay.get_json())
                self.assertEqual(replay.get_json()['state'], 'CONFIRMED_NOT_SENT')
            with psycopg.connect(rehearsal._dsn()) as conn:
                conn.execute('UPDATE core.companies SET canonical_name=%s WHERE id=%s',
                             ('Released after pre-send close', company_id))
        finally:
            with psycopg.connect(rehearsal._dsn()) as conn:
                conn.execute('DELETE FROM core.companies WHERE id=%s', (company_id,))

    def test_second_recipient_domain_conflict_and_hash_binding(self):
        import psycopg
        client = self.module.app.test_client()
        headers = {'Authorization': 'Bearer rehearsal-only'}
        company, account, domain_id = (uuid.uuid4() for _ in range(3))
        domains = [uuid.uuid4().hex + '.example', uuid.uuid4().hex + '.example']
        payload = {'send_id': str(uuid.uuid4()), 'domains': domains, 'recipient_hash': 'f' * 64}
        with psycopg.connect(rehearsal._dsn()) as conn:
            org = conn.execute('SELECT trosa.compat_org_id()').fetchone()[0]
            other = conn.execute("SELECT id FROM identity.users WHERE organization_id=%s AND legacy_user_id <> 'hamid' LIMIT 1", (org,)).fetchone()
            if not other:
                self.skipTest('rehearsal requires another organization member')
            name = 'Cross domain ' + uuid.uuid4().hex
            conn.execute("INSERT INTO core.companies (id, organization_id, canonical_name, normalized_name, identity_status) VALUES (%s,%s,%s,%s,'confirmed')", (company, org, name, name.lower()))
            conn.execute('INSERT INTO core.company_domains (id, company_id, normalized_domain) VALUES (%s,%s,%s)', (domain_id, company, domains[1]))
            conn.execute('INSERT INTO trosa.accounts (id, organization_id, company_id, owner_user_id) VALUES (%s,%s,%s,%s)', (account, org, company, other[0]))
        def post(action, data):
            return client.post('/api/integrations/sela/send-barrier/' + action, headers=headers, json=data)
        try:
            with mock.patch.object(self.module, '_integration_token_matches', return_value=True):
                prepared = post('prepare', payload)
                self.assertEqual(prepared.status_code, 200, prepared.get_json())
                arm = dict(payload, token=prepared.get_json()['token'])
                self.assertEqual(post('arm', dict(arm, recipient_hash='a' * 64)).status_code, 409)
                self.assertEqual(post('arm', dict(arm, domains=domains[:1])).status_code, 409)
                self.assertEqual(post('arm', arm).status_code, 409)
                with psycopg.connect(rehearsal._dsn()) as conn:
                    state = conn.execute('SELECT state FROM audit.sela_send_barriers WHERE send_id=%s', (payload['send_id'],)).fetchone()[0]
                    self.assertEqual(state, 'PREPARED')
        finally:
            with psycopg.connect(rehearsal._dsn()) as conn:
                conn.execute('DELETE FROM trosa.accounts WHERE id=%s', (account,))
                conn.execute('DELETE FROM core.company_domains WHERE id=%s', (domain_id,))
                conn.execute('DELETE FROM core.companies WHERE id=%s', (company,))

    def test_expired_prepare_and_changed_ownership_cannot_arm(self):
        import psycopg
        client = self.module.app.test_client()
        headers = {'Authorization': 'Bearer rehearsal-only'}
        name = 'Barrier Owner ' + uuid.uuid4().hex
        company, account = uuid.uuid4(), uuid.uuid4()
        with psycopg.connect(rehearsal._dsn()) as conn:
            org = conn.execute('SELECT trosa.compat_org_id()').fetchone()[0]
            other = conn.execute("SELECT id FROM identity.users WHERE organization_id=%s AND legacy_user_id <> 'hamid' LIMIT 1", (org,)).fetchone()
            if not other:
                self.skipTest('rehearsal requires another organization member')
            conn.execute('INSERT INTO core.companies (id, organization_id, canonical_name, normalized_name) VALUES (%s,%s,%s,%s)', (company, org, name, name.lower()))
        def post(action, data):
            return client.post('/api/integrations/sela/send-barrier/' + action, headers=headers, json=data)
        try:
            with mock.patch.object(self.module, '_integration_token_matches', return_value=True):
                ids = [str(uuid.uuid4()), str(uuid.uuid4())]
                tokens = []
                for send_id in ids:
                    result = post('prepare', {'send_id': send_id, 'name': name, 'recipient_hash': 'd' * 64})
                    self.assertEqual(result.status_code, 200, result.get_json())
                    tokens.append(result.get_json()['token'])
                with psycopg.connect(rehearsal._dsn()) as conn:
                    conn.execute("SELECT set_config('trosa.sela_send_barrier_operation', 'prepare', true)")
                    conn.execute("UPDATE audit.sela_send_barriers SET expires_at=now()-interval '1 second' WHERE send_id=%s", (ids[0],))
                self.assertEqual(post('arm', {'send_id': ids[0], 'token': tokens[0], 'name': name, 'recipient_hash': 'd' * 64}).status_code, 409)
                with psycopg.connect(rehearsal._dsn()) as conn:
                    conn.execute('INSERT INTO trosa.accounts (id, organization_id, company_id, owner_user_id) VALUES (%s,%s,%s,%s)', (account, org, company, other[0]))
                self.assertEqual(post('arm', {'send_id': ids[1], 'token': tokens[1], 'name': name, 'recipient_hash': 'd' * 64}).status_code, 409)
        finally:
            with psycopg.connect(rehearsal._dsn()) as conn:
                conn.execute('DELETE FROM trosa.accounts WHERE id=%s', (account,))
                conn.execute('DELETE FROM core.companies WHERE id=%s', (company,))


if __name__ == '__main__':
    unittest.main()
