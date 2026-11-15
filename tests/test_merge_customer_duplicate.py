import contextlib
import io
import sqlite3
import unittest
from unittest import mock

from tools import merge_customer_duplicate as merge


class ProfileConnection:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute("ATTACH DATABASE ':memory:' AS trosa")
        self.db.execute('CREATE TABLE trosa.agent_prospect_profiles (id INTEGER, organization_id TEXT, legacy_user_id TEXT, customer_id INTEGER, contact_permission TEXT, suppression_reason TEXT, suppression_at TEXT, updated_at TEXT)')
        self.db.create_function('compat_org_id', 0, lambda: 'org')
        self.db.create_function('compat_current_user', 0, lambda: 'amy')
        self.db.executemany('INSERT INTO trosa.agent_prospect_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?)', [
            (1, 'org', 'amy', 157, 'allowed', '', '', ''),
            (2, 'org', 'amy', 158, 'allowed', '', '', ''),
            (3, 'org', 'hamid', 157, 'do_not_contact', 'Hamid suppression', 'old', 'old'),
            (4, 'org', 'hamid', 158, 'allowed', 'Hamid target', '', ''),
            (5, 'other', 'amy', 157, 'do_not_contact', 'other organization', 'old', 'old'),
        ])

    def execute(self, sql, params=()):
        if 'agent_prospect_profiles' in sql:
            return self.db.execute(sql.replace('trosa.compat_org_id()', 'compat_org_id()').replace('trosa.compat_current_user()', 'compat_current_user()'), params)
        if 'SELECT count(*) FROM trosa.contact_legacy_refs' in sql:
            return self.db.execute('SELECT 0')
        raise AssertionError(sql)


class MergeCustomerOwnerTest(unittest.TestCase):
    def test_collision_preserves_other_owner_profiles_and_snapshot(self):
        conn = ProfileConnection()
        source = {'id': 157, 'account_id': 'amy-source', 'company_uuid': 'source-company'}
        target = {'id': 158, 'account_id': 'amy-target', 'company_uuid': 'target-company'}
        before = [tuple(row) for row in conn.db.execute("SELECT * FROM trosa.agent_prospect_profiles WHERE legacy_user_id='hamid' OR organization_id='other' ORDER BY id")]
        original_exec = merge._exec
        def execute(c, sql, params=()):
            if 'agent_prospect_profiles' in sql:
                return original_exec(c, sql, params)
            return 0
        with mock.patch.object(merge, '_exec', side_effect=execute), mock.patch.object(merge, '_rebind_payload', return_value={}), mock.patch.object(merge, '_validate_owner'):
            merge._merge(conn, source, target, 'merged', 'amy')
        after = [tuple(row) for row in conn.db.execute("SELECT * FROM trosa.agent_prospect_profiles WHERE legacy_user_id='hamid' OR organization_id='other' ORDER BY id")]
        self.assertEqual(before, after)
        amy = [tuple(row) for row in conn.db.execute("SELECT customer_id, contact_permission, suppression_reason FROM trosa.agent_prospect_profiles WHERE organization_id='org' AND legacy_user_id='amy'")]
        self.assertEqual(amy, [(158, 'do_not_contact', 'merged')])

    def test_missing_explicit_owner_fails_before_database_access(self):
        with mock.patch.dict('os.environ', {}, clear=True), mock.patch.object(merge.db, 'postgres_mode', return_value=True), mock.patch.object(merge.db, 'get_db') as get_db, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(merge.main(['--source', '157', '--target', '158']), 2)
        get_db.assert_not_called()

    def test_move_profile_when_only_other_owner_has_target(self):
        conn = ProfileConnection()
        conn.db.execute("DELETE FROM trosa.agent_prospect_profiles WHERE legacy_user_id='amy' AND customer_id=158")
        source = {'id': 157, 'account_id': 'a', 'company_uuid': 'c'}
        target = {'id': 158, 'account_id': 'b', 'company_uuid': 'd'}
        before = [tuple(row) for row in conn.db.execute("SELECT * FROM trosa.agent_prospect_profiles WHERE legacy_user_id='hamid' OR organization_id='other' ORDER BY id")]
        original_exec = merge._exec
        def execute(c, sql, params=()):
            return original_exec(c, sql, params) if 'agent_prospect_profiles' in sql else 0
        with mock.patch.object(merge, '_exec', side_effect=execute), mock.patch.object(merge, '_rebind_payload', return_value={}), mock.patch.object(merge, '_validate_owner'):
            merge._merge(conn, source, target, 'merged', 'amy')
        after = [tuple(row) for row in conn.db.execute("SELECT * FROM trosa.agent_prospect_profiles WHERE legacy_user_id='hamid' OR organization_id='other' ORDER BY id")]
        self.assertEqual(before, after)
        amy = conn.db.execute("SELECT customer_id, contact_permission, suppression_reason FROM trosa.agent_prospect_profiles WHERE organization_id='org' AND legacy_user_id='amy'").fetchone()
        self.assertEqual(tuple(amy), (158, 'do_not_contact', 'merged'))

    def test_missing_or_ambiguous_mapping_fails_closed(self):
        customer = {'id': 157, 'account_id': 'account', 'company_uuid': 'company'}
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = None
        with self.assertRaises(ValueError):
            merge._validate_owner(conn, customer, '')
        conn.execute.assert_not_called()
        with self.assertRaises(ValueError):
            merge._validate_owner(conn, customer, 'amy')

    def test_snapshot_filters_customer_id_tables_by_owner(self):
        conn = mock.Mock()
        conn.execute.return_value.fetchall.return_value = []
        source = {'id': 157, 'account_id': 'a', 'company_uuid': 'c'}
        target = {'id': 158, 'account_id': 'b', 'company_uuid': 'd'}
        with mock.patch.object(merge, '_validate_owner'):
            merge._snapshot(conn, source, target, 'amy')
        queries = [call.args[0] for call in conn.execute.call_args_list]
        for table in ('account_legacy_refs', 'customer_states', 'agent_prospect_profiles'):
            query = next(sql for sql in queries if 'FROM trosa.' + table in sql)
            self.assertIn('organization_id=trosa.compat_org_id()', query)
            self.assertIn('legacy_user_id=trosa.compat_current_user()', query)

    def test_snapshot_excludes_colliding_profiles(self):
        conn = ProfileConnection()
        source = {'id': 157, 'account_id': 'a', 'company_uuid': 'c'}
        target = {'id': 158, 'account_id': 'b', 'company_uuid': 'd'}
        def rows(c, sql, params=()):
            if 'FROM trosa.agent_prospect_profiles' in sql:
                return [dict(row) for row in c.execute(sql, params).fetchall()]
            return []
        with mock.patch.object(merge, '_validate_owner'), mock.patch.object(merge, '_rows', side_effect=rows):
            snapshot = merge._snapshot(conn, source, target, 'amy')
        self.assertEqual([row['id'] for row in snapshot['prospect_profiles']], [1, 2])


if __name__ == '__main__':
    unittest.main()
