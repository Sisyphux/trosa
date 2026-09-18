"""Acceptance tests for the local PostgreSQL rehearsal.

The normal unit-test suite skips this module when no explicitly configured
rehearsal DSN is present.  ``python3 tools/postgres_rehearsal.py test`` starts
the isolated server, creates a fresh database, loads the fixture, and runs the
same tests with real PostgreSQL and the production compatibility adapter.
"""

from __future__ import annotations

import os
import sys
import unittest
import base64
import importlib.util
import io
import json
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _dsn() -> str:
    if os.environ.get("TROSA_REHEARSAL") != "1":
        return ""
    return os.environ.get("TROSA_REHEARSAL_DATABASE_URL", "").strip()


def _loopback(dsn: str) -> bool:
    parsed = urlparse(dsn)
    expected_port = int(os.environ.get("TROSA_REHEARSAL_PORT", "55432"))
    expected_database = os.environ.get("TROSA_REHEARSAL_DB", "trosa_rehearsal")
    return (
        parsed.scheme in {"postgres", "postgresql"}
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and (parsed.port or 5432) == expected_port
        and parsed.path.lstrip("/") == expected_database
    )


class PostgreSQLRehearsalAcceptanceTest(unittest.TestCase):
    """Run canonical Trosa writes and compatibility boundary checks in PG."""

    @classmethod
    def setUpClass(cls):
        dsn = _dsn()
        if not dsn:
            raise unittest.SkipTest(
                "set TROSA_REHEARSAL_DATABASE_URL or run tools/postgres_rehearsal.py test"
            )
        if not _loopback(dsn):
            raise unittest.SkipTest("PostgreSQL rehearsal tests only accept a loopback DSN")
        try:
            import psycopg

            with psycopg.connect(dsn) as connection:
                connection.execute("SELECT 1")
        except Exception as exc:  # pragma: no cover - depends on local service
            raise unittest.SkipTest(f"local PostgreSQL is unavailable: {exc}")

        os.environ["TRADE_OS_DATA_BACKEND"] = "postgres"
        os.environ["TRADE_OS_DATABASE_URL"] = dsn
        import db
        from tools.unified_postgres_migration import verify_schema

        db.init_postgres_store()
        contract = verify_schema(dsn)
        if not contract["ok"]:
            raise AssertionError(f"schema contract is incomplete: {contract}")
        cls.dsn = dsn

    def setUp(self):
        import db

        db.set_db_user("hamid")
        self.connection = db.get_db()

    def tearDown(self):
        try:
            self.connection.close()
        finally:
            import db

            db.set_db_user(None)

    @classmethod
    def _app_module(cls):
        """Load the Flask surface after the rehearsal backend is configured."""
        module = getattr(cls, "_loaded_app", None)
        if module is None:
            spec = importlib.util.spec_from_file_location(
                "trosa_postgres_rehearsal_app", ROOT / "app.py"
            )
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            module.app.config.update(TESTING=True)
            cls._loaded_app = module
        return module

    def test_batch_customer_reads_preserve_results_with_constant_query_count(self):
        from trosa_domain import active_customers, customer_facts, customer_contacts, customer_tasks, customer_interactions

        ids = [row['id'] for row in active_customers(self.connection)]
        self.assertTrue(ids)
        ids = ids[:5] + [987654321]
        expected = {}
        for customer_id in ids:
            expected.update(customer_facts(self.connection, [customer_id]))
        with mock.patch.object(self.connection, 'execute', wraps=self.connection.execute) as execute:
            self.assertEqual(customer_facts(self.connection, ids), expected)
            self.assertEqual(execute.call_count, 2)
        for reader in (customer_contacts, customer_tasks, customer_interactions):
            batch = reader(self.connection, None, customer_ids=ids)
            for customer_id in ids:
                self.assertEqual([row for row in batch if row['customer_id'] == customer_id],
                                 reader(self.connection, customer_id))
        module = self._app_module()
        with mock.patch.object(self.connection, 'execute', wraps=self.connection.execute) as execute:
            module._customer_search_match_contexts(self.connection, ids, ['rehearsal'])
            self.assertEqual(execute.call_count, 5)

    def test_schema_contract_and_migration_ledger(self):
        import db
        from tools.unified_postgres_migration import SCHEMA_PATHS
        from tools.unified_postgres_migration import verify_schema

        result = verify_schema(self.dsn)
        self.assertTrue(result["ok"], result)
        self.assertTrue(all(result["migrations"].values()), result["migrations"])
        # Keep the two startup/apply registries aligned with the migration
        # directory.  A new forward migration is not rehearsal-complete if
        # one runtime path silently omits it.
        migration_files = sorted(path.name for path in (ROOT / "migrations").glob("*.sql"))
        self.assertEqual(
            migration_files,
            sorted(Path(path).name for path in db._postgres_migration_paths()),
        )
        self.assertEqual(
            migration_files,
            sorted(Path(path).name for path in SCHEMA_PATHS),
        )
        self.assertEqual(result["data_integrity"], {
            "orphan_account_legacy_refs": 0,
            "orphan_contact_legacy_refs": 0,
        })

    def test_canonical_customer_contact_interaction_task_inbox_and_state(self):
        import trosa_domain
        from tools.postgres_rehearsal import FIXTURE_KEY, load_fixture

        ids = load_fixture()
        customer_id = ids["customer_id"]
        contact_id = ids["contact_id"]
        interaction_id = ids["interaction_id"]
        task_id = ids["task_id"]
        inbox_id = ids["inbox_id"]

        record = trosa_domain.customer_record(self.connection, customer_id)
        self.assertIsNotNone(record)
        self.assertEqual(record["company"], "Rehearsal Acrylic Co")
        self.assertEqual(record["business_stage"], "成交")
        self.assertEqual(record["customer_judgment"], "fixture-qualified")
        self.assertEqual(record["last_interaction_on"], "2026-09-10")
        self.assertEqual(record["next_task_on"], "2026-09-12")

        contacts = trosa_domain.customer_contacts(self.connection, customer_id)
        self.assertTrue(any(item["id"] == contact_id and item["email"] == "buyer@rehearsal.example"
                            for item in contacts))

        interactions = trosa_domain.customer_interactions(self.connection, customer_id)
        interaction = next(item for item in interactions if item["id"] == interaction_id)
        self.assertEqual(interaction["kind"], "communication")
        self.assertEqual(interaction["direction"], "inbound")
        self.assertEqual(interaction["source"], "postgres-rehearsal")

        tasks = trosa_domain.customer_tasks(self.connection, customer_id)
        self.assertTrue(any(item["id"] == task_id and item["is_done"] == 0 for item in tasks))
        today = trosa_domain.today_tasks(self.connection, due_on_or_before="2026-09-12")
        self.assertTrue(any(item["id"] == task_id and item["customer_id"] == customer_id for item in today))

        facts = trosa_domain.customer_facts(self.connection, [customer_id])[customer_id]
        self.assertEqual(facts["contact_state"], "contacted")
        self.assertTrue(facts["has_contact"])
        self.assertEqual(facts["next_task_title"], "Send sample quotation")

        # Exercise the current canonical write paths, not just reads.
        trosa_domain.update_customer(
            self.connection,
            customer_id=customer_id,
            values={
                "name": "PostgreSQL Rehearsal Customer Updated",
                "company": "Rehearsal Acrylic Co Updated",
                "country": "US",
                "level": "B",
                "website": "https://rehearsal.example",
                "profile": "updated profile",
                "field": "acrylic sheet",
                "industry": "manufacturing",
                "company_size": "51-200",
                "annual_revenue": "2000000",
                "tags": "rehearsal,updated",
                "last_contact": "2026-09-11",
                "next_follow_up": "2026-09-13",
                "notes": "canonical update",
                "system_notes": "canonical system update",
                "import_source": "postgres-rehearsal",
                "manual_next_follow": True,
                "business_stage": "成交",
                "business_role": "终端",
                "customer_judgment": "approved",
            },
        )
        trosa_domain.update_contact(
            self.connection,
            contact_id=contact_id,
            values={
                "name": "Updated Rehearsal Buyer",
                "title": "Senior Purchasing Manager",
                "email": "buyer@rehearsal.example",
                "phone": "+1-555-0101",
                "preferred_channel": "email",
                "contact_type": "person",
                "is_primary": True,
                "notes": "updated contact",
            },
        )
        trosa_domain.update_interaction(
            self.connection,
            interaction_id=interaction_id,
            occurred_on="2026-09-11",
            activity_type="customer_reply",
            direction="inbound",
            content="Updated fixture reply",
            result="received",
            next_plan="send revised quotation",
        )
        trosa_domain.set_interaction_flag(
            self.connection, interaction_id=interaction_id, field="is_reported", value=True
        )
        trosa_domain.update_task(
            self.connection,
            task_id=task_id,
            title="Send revised quotation",
            content="Send the revised acrylic sheet quotation",
            reason="canonical update",
            due_on="2026-09-13",
            now="2026-09-11 10:00:00",
        )

        # The old view remains an adapter only: a compatibility write updates
        # canonical details, and must not create another Customer aggregate.
        before_accounts = self.connection.execute(
            "SELECT count(*) FROM trosa.accounts"
        ).fetchone()[0]
        before_details = self.connection.execute(
            "SELECT count(*) FROM trosa.customer_details"
        ).fetchone()[0]
        self.connection.execute(
            "UPDATE trade_os_compat.customers SET notes=?, system_notes=? WHERE id=?",
            ("compatibility note", "compatibility system note", customer_id),
        )
        detail = self.connection.execute(
            """SELECT d.notes, d.system_notes
                 FROM trosa.customer_details d
                 JOIN trosa.account_legacy_refs ref ON ref.account_id=d.account_id
                WHERE ref.legacy_user_id=? AND ref.legacy_customer_id=?""",
            ("hamid", customer_id),
        ).fetchone()
        self.assertEqual(detail["notes"], "compatibility note")
        self.assertEqual(detail["system_notes"], "compatibility system note")
        self.assertEqual(self.connection.execute("SELECT count(*) FROM trosa.accounts").fetchone()[0], before_accounts)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM trosa.customer_details").fetchone()[0], before_details)

        compatibility_customer = self.connection.execute(
            "SELECT id, notes FROM trade_os_compat.customers WHERE id=?", (customer_id,)
        ).fetchone()
        self.assertEqual(compatibility_customer["notes"], "compatibility note")
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM trade_os_compat.follow_up_logs WHERE customer_id=?",
                (customer_id,),
            ).fetchone()[0],
            1,
        )

        # Inbox and Task lifecycle actions still operate on canonical facts.
        trosa_domain.assign_inbox_customer(
            self.connection, inbox_item_id=inbox_id, customer_id=customer_id
        )
        trosa_domain.resolve_inbox_item(
            self.connection,
            inbox_item_id=inbox_id,
            resolved_at="2026-09-11",
            resolution_reason="accepted",
            resolution_note="fixture reviewed",
        )
        trosa_domain.complete_task(
            self.connection, task_id=task_id, completed_at="2026-09-11"
        )
        self.connection.commit()

        canonical = self.connection.execute(
            """SELECT a.display_name, a.priority_level, c.canonical_name,
                       d.notes, s.business_role, s.customer_judgment
                 FROM trosa.accounts a
                 JOIN core.companies c ON c.id=a.company_id
                 JOIN trosa.customer_details d ON d.account_id=a.id
                 JOIN trosa.account_legacy_refs ref ON ref.account_id=a.id
                 JOIN trosa.customer_states s
                   ON s.organization_id=ref.organization_id
                  AND s.legacy_user_id=ref.legacy_user_id
                  AND s.legacy_customer_id=ref.legacy_customer_id
                WHERE ref.legacy_user_id=? AND ref.legacy_customer_id=?""",
            ("hamid", customer_id),
        ).fetchone()
        self.assertEqual(canonical["display_name"], "PostgreSQL Rehearsal Customer Updated")
        self.assertEqual(canonical["priority_level"], "B")
        self.assertEqual(canonical["canonical_name"], "Rehearsal Acrylic Co Updated")
        self.assertEqual(canonical["notes"], "compatibility note")
        self.assertEqual(canonical["business_role"], "终端")
        self.assertEqual(canonical["customer_judgment"], "approved")

        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM trosa.tasks task JOIN trosa.legacy_row_refs ref ON ref.target_id=task.id "
                "WHERE ref.table_name='reminders' AND ref.legacy_id=?", (task_id,)
            ).fetchone()[0],
            "done",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM trosa.inbox_items item JOIN trosa.legacy_row_refs ref ON ref.target_id=item.id "
                "WHERE ref.table_name='inbox_items' AND ref.legacy_id=?", (inbox_id,)
            ).fetchone()[0],
            "resolved",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM trade_os_compat.inbox_items WHERE id=?", (inbox_id,)
            ).fetchone()[0],
            "resolved",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM trosa.account_legacy_refs "
                "WHERE legacy_payload->>'rehearsal_fixture'=?", (FIXTURE_KEY,)
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT jsonb_exists(legacy_payload, 'business_stage') OR "
                "jsonb_exists(legacy_payload, 'customer_judgment') "
                "FROM trosa.account_legacy_refs WHERE legacy_customer_id=?", (customer_id,)
            ).fetchone()[0],
            False,
        )

    def test_same_company_is_shared_but_ownership_stays_single(self):
        """Two users may share a canonical company; each account has one owner."""
        import db
        import trosa_domain
        from tools.postgres_rehearsal import load_fixture

        ids = load_fixture()
        hamid_customer_id = ids['customer_id']
        hamid_account = self.connection.execute(
            '''SELECT account_id FROM trosa.account_legacy_refs
                WHERE organization_id=trosa.compat_org_id() AND legacy_user_id='hamid'
                  AND legacy_customer_id=?''', (hamid_customer_id,),
        ).fetchone()['account_id']
        company_id = self.connection.execute(
            'SELECT company_id FROM trosa.accounts WHERE id=?', (hamid_account,)).fetchone()[0]
        amy_customer_id = 900001
        amy_account = self.connection.execute(
            "SELECT trosa.compat_uuid(?)", ('owner-test:amy-account',)).fetchone()[0]
        amy_uid = self.connection.execute(
            "SELECT id FROM identity.users WHERE organization_id=trosa.compat_org_id() AND legacy_user_id='amy'",
        ).fetchone()[0]
        try:
            self.connection.execute(
                '''INSERT INTO trosa.accounts (id, organization_id, company_id, owner_user_id, display_name)
                   VALUES (?, trosa.compat_org_id(), ?, ?, 'Amy original')''',
                (amy_account, company_id, amy_uid),
            )
            self.connection.execute(
                '''INSERT INTO trosa.account_legacy_refs
                       (organization_id, legacy_user_id, legacy_customer_id, account_id, source_db, legacy_payload)
                   VALUES (trosa.compat_org_id(), 'amy', ?, ?, 'owner-test-amy',
                           '{"name":"Amy original","company":"Amy Co","country":"CA","last_contact":"2026-01-01"}'::jsonb)''',
                (amy_customer_id, amy_account),
            )
            self.connection.commit()

            # One shared company, two accounts, one owner each.
            owner_rows = self.connection.execute(
                '''SELECT usr.legacy_user_id FROM trosa.accounts a
                    JOIN identity.users usr ON usr.id=a.owner_user_id
                   WHERE a.organization_id=trosa.compat_org_id() AND a.company_id=?''',
                (company_id,),
            ).fetchall()
            self.assertEqual({row['legacy_user_id'] for row in owner_rows}, {'hamid', 'amy'})

            trosa_domain.update_customer(self.connection, customer_id=hamid_customer_id, values={
                'name': 'Hamid changed', 'company': 'Hamid Co', 'country': 'US', 'level': 'A',
                'website': 'https://hamid.example', 'profile': 'hamid', 'field': 'hamid field',
                'industry': 'hamid industry', 'company_size': '51-200', 'annual_revenue': '20',
                'tags': 'hamid', 'status': 'Hamid status', 'notes': 'Hamid note',
                'system_notes': 'Hamid system', 'import_source': 'hamid', 'last_contact': '2026-02-01',
                'next_follow_up': '2026-02-02', 'manual_next_follow': True,
                'business_stage': '成交', 'business_role': '终端', 'customer_judgment': 'hamid judgment',
            })
            trosa_domain.set_customer_deleted(self.connection, customer_id=hamid_customer_id,
                                              deleted=True, changed_at='2026-02-04')
            self.connection.commit()

            db.set_db_user('amy')
            amy_connection = db.get_db()
            try:
                amy_record = trosa_domain.customer_record(amy_connection, amy_customer_id)
                self.assertIsNotNone(amy_record)
                self.assertEqual(amy_record['name'], 'Amy original')
                self.assertEqual(amy_record['company'], 'Amy Co')
                self.assertIsNotNone(trosa_domain.customer_record(amy_connection, amy_customer_id))
            finally:
                amy_connection.close()
                db.set_db_user('hamid')

            self.assertIsNone(trosa_domain.customer_record(self.connection, hamid_customer_id))
            trosa_domain.set_customer_deleted(self.connection, customer_id=hamid_customer_id, deleted=False)
            self.assertEqual(trosa_domain.customer_record(self.connection, hamid_customer_id)['name'], 'Hamid changed')
            self.connection.commit()
        finally:
            self.connection.execute('DELETE FROM trosa.customer_details WHERE account_id=?', (amy_account,))
            self.connection.execute('DELETE FROM trosa.account_legacy_refs WHERE account_id=?', (amy_account,))
            self.connection.execute('DELETE FROM trosa.accounts WHERE id=?', (amy_account,))
            self.connection.commit()

    def test_customer_history_binding_keeps_account_aliases_apart(self):
        """A bound fact must not fan out to a sibling alias of the same account."""
        import trosa_domain
        from tools.postgres_rehearsal import load_fixture

        ids = load_fixture()
        base_customer_id = ids['customer_id']
        account_id = self.connection.execute(
            '''SELECT account_id FROM trosa.account_legacy_refs
                WHERE organization_id=trosa.compat_org_id() AND legacy_user_id='hamid'
                  AND legacy_customer_id=?''', (base_customer_id,),
        ).fetchone()['account_id']
        alias_customer_id = 910001
        self.connection.execute(
            '''INSERT INTO trosa.account_legacy_refs
                   (organization_id, legacy_user_id, legacy_customer_id, account_id,
                    source_db, legacy_payload)
               VALUES (trosa.compat_org_id(), 'hamid', ?, ?, 'binding-test',
                       '{"name":"Historical alias","company":"Binding Alias Co"}'::jsonb)''',
            (alias_customer_id, account_id),
        )

        def ident(seed):
            return self.connection.execute("SELECT trosa.compat_uuid(?)", (seed,)).fetchone()[0]

        def bind(customer_id):
            payload = {'is_reported': True}
            if customer_id is not None:
                payload['customer_id'] = customer_id
            return json.dumps(payload)

        base_event = ident('binding-test:event:base')
        alias_event = ident('binding-test:event:alias')
        unbound_event = ident('binding-test:event:unbound')
        base_task = ident('binding-test:task:base')
        alias_task = ident('binding-test:task:alias')
        unbound_task = ident('binding-test:task:unbound')
        target_ids = [base_event, alias_event, unbound_event, base_task, alias_task, unbound_task]
        try:
            for target, content, customer_id in (
                (base_event, 'BASE BOUND', base_customer_id),
                (alias_event, 'ALIAS BOUND', alias_customer_id),
                (unbound_event, 'ALIAS UNBOUND', None),
            ):
                self.connection.execute(
                    '''INSERT INTO trosa.timeline_events
                           (id, account_id, event_type, direction, content, source_module,
                            source_reference, occurred_at, payload)
                       VALUES (?, ?, 'email', 'inbound', ?, 'binding-test', ?, trosa.compat_time('2026-08-01'), ?::jsonb)''',
                    (target, account_id, content, target, bind(customer_id)),
                )
            for idx, (target, content, customer_id) in enumerate((
                (base_task, 'BASE TASK', base_customer_id),
                (alias_task, 'ALIAS TASK', alias_customer_id),
                (unbound_task, 'UNBOUND TASK', None),
            )):
                self.connection.execute(
                    '''INSERT INTO trosa.tasks
                           (id, account_id, title, content, reason, due_at, status, task_type, legacy_payload)
                       VALUES (?, ?, ?, ?, 'binding test', trosa.compat_time(?), 'open',
                               'follow_up', ?::jsonb)''',
                    (target, account_id, content, content, '2026-09-%02d' % (2 + idx), bind(customer_id)),
                )
            for idx, target in enumerate((base_event, alias_event, unbound_event)):
                self.connection.execute(
                    '''INSERT INTO trosa.legacy_row_refs
                           (organization_id, legacy_user_id, table_name, legacy_id, target_id)
                       VALUES (trosa.compat_org_id(), 'hamid', 'follow_up_logs', ?, ?)''',
                    (950000 + idx, target),
                )
            for idx, target in enumerate((base_task, alias_task, unbound_task)):
                self.connection.execute(
                    '''INSERT INTO trosa.legacy_row_refs
                           (organization_id, legacy_user_id, table_name, legacy_id, target_id)
                       VALUES (trosa.compat_org_id(), 'hamid', 'reminders', ?, ?)''',
                    (960000 + idx, target),
                )
            self.connection.commit()

            base_interactions = {item['content'] for item in
                                 trosa_domain.customer_interactions(self.connection, base_customer_id)}
            self.assertIn('BASE BOUND', base_interactions)
            self.assertNotIn('ALIAS BOUND', base_interactions)
            self.assertNotIn('ALIAS UNBOUND', base_interactions)
            alias_interactions = {item['content'] for item in
                                  trosa_domain.customer_interactions(self.connection, alias_customer_id)}
            self.assertIn('ALIAS BOUND', alias_interactions)
            self.assertNotIn('BASE BOUND', alias_interactions)
            self.assertNotIn('ALIAS UNBOUND', alias_interactions)

            base_tasks = {item['title'] for item in
                          trosa_domain.customer_tasks(self.connection, base_customer_id)}
            self.assertIn('BASE TASK', base_tasks)
            self.assertNotIn('ALIAS TASK', base_tasks)
            self.assertNotIn('UNBOUND TASK', base_tasks)
            alias_tasks = {item['title'] for item in
                           trosa_domain.customer_tasks(self.connection, alias_customer_id)}
            self.assertIn('ALIAS TASK', alias_tasks)
            self.assertNotIn('BASE TASK', alias_tasks)
            self.assertNotIn('UNBOUND TASK', alias_tasks)
        finally:
            marks = ','.join('?' for _ in target_ids)
            self.connection.execute(f'DELETE FROM trosa.legacy_row_refs WHERE target_id IN ({marks})', target_ids)
            self.connection.execute(f'DELETE FROM trosa.timeline_events WHERE id IN ({marks})', target_ids)
            self.connection.execute(f'DELETE FROM trosa.tasks WHERE id IN ({marks})', target_ids)
            self.connection.execute(
                '''DELETE FROM trosa.account_legacy_refs
                    WHERE organization_id=trosa.compat_org_id() AND legacy_user_id='hamid'
                      AND legacy_customer_id=?''', (alias_customer_id,))
            self.connection.commit()

    def test_timeline_identity_does_not_collide_across_customers_or_users(self):
        """A shared source reference or per-user legacy id must not share one UUID."""
        import db
        import trosa_domain
        from tools.postgres_rehearsal import load_fixture

        ids = load_fixture()
        first_customer = ids['customer_id']
        second_customer = trosa_domain.create_customer(self.connection, values={
            'name': 'Second Identity Customer',
            'company': 'Second Identity Co',
            'website': 'https://second-identity.example',
        })
        self.connection.commit()

        first = trosa_domain.record_external_interaction(
            self.connection, customer_id=first_customer, content='FIRST SAME REF',
            occurred_on='2026-08-10', direction='inbound', source='gmail',
            source_reference='shared-message-id',
        )
        second = trosa_domain.record_external_interaction(
            self.connection, customer_id=second_customer, content='SECOND SAME REF',
            occurred_on='2026-08-10', direction='inbound', source='gmail',
            source_reference='shared-message-id',
        )
        # Replaying the same natural fact stays idempotent for its own customer.
        self.assertEqual(first, trosa_domain.record_external_interaction(
            self.connection, customer_id=first_customer, content='FIRST SAME REF',
            occurred_on='2026-08-10', direction='inbound', source='gmail',
            source_reference='shared-message-id',
        ))
        self.assertNotEqual(first, second)
        self.connection.commit()

        ref_rows = {
            row['legacy_customer_id']: row['account_id']
            for row in self.connection.execute(
                '''SELECT legacy_customer_id, account_id FROM trosa.account_legacy_refs
                    WHERE organization_id=trosa.compat_org_id() AND legacy_user_id='hamid'
                      AND legacy_customer_id IN (?, ?)''', (first_customer, second_customer),
            ).fetchall()
        }
        first_account = ref_rows[first_customer]
        second_account = ref_rows[second_customer]

        def target_for(legacy_id):
            return self.connection.execute(
                '''SELECT target_id FROM trosa.legacy_row_refs
                    WHERE organization_id=trosa.compat_org_id() AND legacy_user_id='hamid'
                      AND table_name='follow_up_logs' AND legacy_id=?''', (legacy_id,),
            ).fetchone()['target_id']

        first_target, second_target = target_for(first), target_for(second)
        self.assertNotEqual(first_target, second_target)
        # The canonical identity is derived from organization, user, account,
        # source and reference -- not source reference alone.
        expected_first = self.connection.execute(
            '''SELECT trosa.compat_uuid('interaction:' || trosa.compat_org_id()::text || ':hamid:'
                   || ?::text || ':gmail:shared-message-id')''', (str(first_account),),
        ).fetchone()[0]
        self.assertEqual(first_target, expected_first)

        first_items = {item['content'] for item in trosa_domain.customer_interactions(self.connection, first_customer)}
        self.assertIn('FIRST SAME REF', first_items)
        self.assertNotIn('SECOND SAME REF', first_items)
        second_items = {item['content'] for item in trosa_domain.customer_interactions(self.connection, second_customer)}
        self.assertIn('SECOND SAME REF', second_items)
        self.assertNotIn('FIRST SAME REF', second_items)

        # Two users whose per-user legacy id sequence both produce the same
        # integer must still get distinct canonical events.
        db.set_db_user('amy')
        amy_connection = db.get_db()
        try:
            amy_customer = trosa_domain.create_customer(amy_connection, values={
                'name': 'Amy Identity Customer',
                'company': 'Amy Identity Co',
                'website': 'https://amy-identity.example',
            })
            amy_legacy = trosa_domain.record_external_interaction(
                amy_connection, customer_id=amy_customer, content='AMY MANUAL',
                occurred_on='2026-08-10', direction='outbound', source='manual',
            )
            amy_connection.commit()
            amy_target = amy_connection.execute(
                '''SELECT target_id FROM trosa.legacy_row_refs
                    WHERE organization_id=trosa.compat_org_id() AND legacy_user_id='amy'
                      AND table_name='follow_up_logs' AND legacy_id=?''', (amy_legacy,),
            ).fetchone()['target_id']
            amy_items = {item['content'] for item in trosa_domain.customer_interactions(amy_connection, amy_customer)}
            self.assertIn('AMY MANUAL', amy_items)
        finally:
            amy_connection.close()
            db.set_db_user('hamid')
        self.assertNotIn(amy_target, {first_target, second_target})

        # Direct database guard: a second event for the same natural identity
        # must be rejected instead of silently dropped.
        duplicate_id = self.connection.execute(
            "SELECT trosa.compat_uuid(?)", ('r2-duplicate',)
        ).fetchone()[0]
        with self.assertRaises(Exception):
            self.connection.execute(
                '''INSERT INTO trosa.timeline_events
                       (id, account_id, event_type, direction, content, source_module,
                        source_reference, occurred_at, payload)
                   VALUES (?, ?, 'email', 'inbound', 'DUPLICATE', 'gmail',
                           'shared-message-id', trosa.compat_time('2026-08-11'), '{}'::jsonb)''',
                (duplicate_id, first_account),
            )
        self.connection.rollback()

    def test_single_active_owner_and_explicit_transfer(self):
        """A Customer has one owner; transfer moves it explicitly."""
        import db
        import trosa_domain
        from tools.postgres_rehearsal import load_fixture

        load_fixture()
        customer_id = trosa_domain.create_customer(self.connection, values={
            'name': 'Single Owner Customer',
            'company': 'Single Owner Co',
            'website': 'https://single-owner.example',
        })
        self.connection.commit()
        owner = self.connection.execute(
            '''SELECT usr.legacy_user_id FROM trosa.account_legacy_refs ar
                JOIN trosa.accounts a ON a.id=ar.account_id
                JOIN identity.users usr ON usr.id=a.owner_user_id
               WHERE ar.organization_id=trosa.compat_org_id() AND ar.legacy_user_id='hamid'
                 AND ar.legacy_customer_id=?''', (customer_id,),
        ).fetchone()['legacy_user_id']
        self.assertEqual(owner, 'hamid')

        # A reference for a different user is rejected by the database.
        with self.assertRaises(Exception):
            self.connection.execute(
                '''INSERT INTO trosa.account_legacy_refs
                       (organization_id, legacy_user_id, legacy_customer_id, account_id, source_db)
                   SELECT organization_id, 'amy', 931001, account_id, 'guard-probe'
                     FROM trosa.account_legacy_refs
                    WHERE organization_id=trosa.compat_org_id() AND legacy_user_id='hamid'
                      AND legacy_customer_id=?''', (customer_id,),
            )
        self.connection.rollback()

        moved = trosa_domain.transfer_customer(self.connection, customer_id=customer_id, to_user='amy')
        self.connection.commit()
        self.assertEqual(
            self.connection.execute(
                '''SELECT usr.legacy_user_id FROM trosa.account_legacy_refs ar
                    JOIN trosa.accounts a ON a.id=ar.account_id
                    JOIN identity.users usr ON usr.id=a.owner_user_id
                   WHERE ar.organization_id=trosa.compat_org_id() AND ar.legacy_user_id='amy'
                     AND ar.legacy_customer_id=?''', (moved,),
            ).fetchone()['legacy_user_id'],
            'amy',
        )
        db.set_db_user('amy')
        amy_connection = db.get_db()
        try:
            self.assertIsNotNone(trosa_domain.customer_record(amy_connection, moved))
        finally:
            amy_connection.close()
            db.set_db_user('hamid')
        self.assertIsNone(trosa_domain.customer_record(self.connection, customer_id))

    def test_owner_heal_splits_multi_user_accounts(self):
        """The drift heal gives each user their own account for a shared company."""
        from tools.postgres_rehearsal import load_fixture

        ids = load_fixture()
        customer_id = ids['customer_id']
        hamid_account = self.connection.execute(
            '''SELECT account_id FROM trosa.account_legacy_refs
                WHERE organization_id=trosa.compat_org_id() AND legacy_user_id='hamid'
                  AND legacy_customer_id=?''', (customer_id,),
        ).fetchone()['account_id']
        company_id = self.connection.execute(
            'SELECT company_id FROM trosa.accounts WHERE id=?', (hamid_account,)).fetchone()[0]
        # Seed the pre-invariant drift, then let the heal split it.
        self.connection.execute(
            'ALTER TABLE trosa.account_legacy_refs DISABLE TRIGGER trosa_account_legacy_refs_owner_guard')
        self.connection.execute(
            '''INSERT INTO trosa.account_legacy_refs
                   (organization_id, legacy_user_id, legacy_customer_id, account_id, source_db, legacy_payload)
               VALUES (trosa.compat_org_id(), 'amy', 932001, ?, 'drift-probe', '{}'::jsonb)''',
            (hamid_account,),
        )
        self.connection.execute(
            'ALTER TABLE trosa.account_legacy_refs ENABLE TRIGGER trosa_account_legacy_refs_owner_guard')
        split_count = self.connection.execute(
            'SELECT trosa.enforce_single_account_owner()').fetchone()[0]
        self.assertGreaterEqual(split_count, 1)
        amy_account = self.connection.execute(
            '''SELECT a.id, a.owner_user_id FROM trosa.account_legacy_refs ar
                JOIN trosa.accounts a ON a.id=ar.account_id
               WHERE ar.organization_id=trosa.compat_org_id() AND ar.legacy_user_id='amy'
                 AND ar.legacy_customer_id=932001''',
        ).fetchone()
        self.assertIsNotNone(amy_account)
        self.assertNotEqual(amy_account['id'], hamid_account)
        owner_legacy = self.connection.execute(
            'SELECT legacy_user_id FROM identity.users WHERE id=?', (amy_account['owner_user_id'],),
        ).fetchone()[0]
        self.assertEqual(owner_legacy, 'amy')
        self.assertEqual(
            self.connection.execute(
                'SELECT company_id FROM trosa.accounts WHERE id=?', (amy_account['id'],),
            ).fetchone()[0],
            company_id,
        )
        self.connection.rollback()

    def test_compat_writer_creates_owner_scoped_accounts_for_shared_company(self):
        """The compatibility writer never merges two owners' accounts."""
        import db
        from tools.postgres_rehearsal import load_fixture

        load_fixture()
        self.connection.execute(
            '''INSERT INTO trade_os_compat.customers (name, company, country, website, level)
               VALUES ('Compat Owner A', 'Compat Owner Co', 'US', 'https://compat-owner.example', 'A')''')
        first = int(self.connection.execute(
            "SELECT current_setting('trade_os.lastrowid', true)").fetchone()[0])
        first_account = self.connection.execute(
            '''SELECT account_id FROM trosa.account_legacy_refs
                WHERE organization_id=trosa.compat_org_id() AND legacy_user_id='hamid'
                  AND legacy_customer_id=?''', (first,),
        ).fetchone()['account_id']
        self.connection.commit()

        db.set_db_user('amy')
        amy_connection = db.get_db()
        try:
            amy_connection.execute(
                '''INSERT INTO trade_os_compat.customers (name, company, country, website, level)
                   VALUES ('Compat Owner B', 'Compat Owner Co', 'US', 'https://compat-owner.example', 'B')''')
            second = int(amy_connection.execute(
                "SELECT current_setting('trade_os.lastrowid', true)").fetchone()[0])
            second_account = amy_connection.execute(
                '''SELECT account_id FROM trosa.account_legacy_refs
                    WHERE organization_id=trosa.compat_org_id() AND legacy_user_id='amy'
                      AND legacy_customer_id=?''', (second,),
            ).fetchone()['account_id']
            amy_connection.commit()
        finally:
            amy_connection.close()
            db.set_db_user('hamid')

        self.assertNotEqual(first_account, second_account)
        rows = self.connection.execute(
            '''SELECT a.company_id, usr.legacy_user_id FROM trosa.accounts a
                JOIN identity.users usr ON usr.id=a.owner_user_id
               WHERE a.id IN (?, ?)''', (first_account, second_account),
        ).fetchall()
        self.assertEqual(len({row['company_id'] for row in rows}), 1)
        self.assertEqual({row['legacy_user_id'] for row in rows}, {'hamid', 'amy'})

    def test_contact_delete_and_undo_restore_on_postgres(self):
        """Undo of a deleted contact must re-insert through the compat view."""
        import trosa_domain
        from tools.postgres_rehearsal import load_fixture

        module = self._app_module()
        client = module.app.test_client()
        self.assertEqual(
            client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200
        )
        customer_id = load_fixture()['customer_id']
        contact_id = trosa_domain.create_contact(self.connection, customer_id=customer_id, values={
            'name': 'Undo Target Buyer',
            'title': 'Buyer',
            'email': 'undo-target@example.test',
            'phone': '+1-555-0177',
            'preferred_channel': 'email',
            'contact_type': 'person',
            'is_primary': False,
            'notes': 'created to be deleted and restored',
        })
        self.connection.commit()

        deleted = client.delete(f'/api/contacts/{contact_id}')
        self.assertEqual(deleted.status_code, 200, deleted.get_data(as_text=True))
        token = deleted.get_json()['undo_token']

        restored_response = client.post(f'/api/undo/{token}')
        self.assertEqual(restored_response.status_code, 200, restored_response.get_data(as_text=True))

        restored = trosa_domain.customer_contacts(self.connection, customer_id)
        match = next((item for item in restored if int(item['id']) == int(contact_id)), None)
        self.assertIsNotNone(match, restored)
        self.assertEqual(match['email'], 'undo-target@example.test')
        self.assertEqual(match['name'], 'Undo Target Buyer')

    def test_flask_acceptance_routes_use_canonical_postgres(self):
        """Exercise the normal HTTP workflow against real PostgreSQL."""
        module = self._app_module()
        module._INBOX_CACHE.clear()
        client = module.app.test_client()
        self.assertEqual(
            client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200
        )

        created = client.post('/api/customers', json={
            'name': 'API Acceptance Customer',
            'company': 'API Acceptance Co',
            'country': 'US',
            'level': 'B',
            'website': 'https://api-acceptance.example',
            'field': 'acrylic sheet',
            'industry': 'manufacturing',
            'notes': 'HTTP acceptance fixture',
            'next_follow_up': '2026-09-15',
            'task_title': 'Confirm API acceptance sample',
            'contacts': [{
                'name': 'API Acceptance Buyer',
                'title': 'Purchasing Manager',
                'email': 'api-buyer@api-acceptance.example',
                'phone': '+1-555-0199',
                'preferred_channel': 'email',
                'is_primary': 1,
            }],
        })
        self.assertEqual(created.status_code, 201, created.get_json())
        customer_id = created.get_json()['id']

        detail = client.get(f'/api/customers/{customer_id}')
        self.assertEqual(detail.status_code, 200, detail.get_json())
        self.assertEqual(detail.get_json()['company'], 'API Acceptance Co')
        contact_id = detail.get_json()['contacts'][0]['id']

        updated = client.put(f'/api/customers/{customer_id}', json={
            'name': 'API Acceptance Customer Updated',
            'company': 'API Acceptance Co',
            'country': 'US',
            'level': 'B+',
            'website': 'https://api-acceptance.example',
            'field': 'acrylic sheet',
            'industry': 'manufacturing',
            'notes': 'updated through HTTP',
            'next_follow_up': '2026-09-16',
        })
        self.assertEqual(updated.status_code, 200, updated.get_json())
        contact_update = client.put(f'/api/contacts/{contact_id}', json={
            'name': 'API Acceptance Buyer Updated',
            'title': 'Senior Purchasing Manager',
            'email': 'api-buyer@api-acceptance.example',
            'phone': '+1-555-0198',
            'preferred_channel': 'email',
            'notes': 'updated contact',
        })
        self.assertEqual(contact_update.status_code, 200, contact_update.get_json())

        communication = client.post(f'/api/customers/{customer_id}/follow_history', json={
            'activity_content': 'Buyer confirmed the sample specification.',
            'activity_result': 'received',
            'activity_type': 'customer_reply',
            'direction': 'inbound',
            'follow_date': '2026-09-14',
            'next_plan': 'Confirm API acceptance sample',
            'next_follow_up': '2026-09-18',
            'contact_id': contact_id,
            'source': 'postgres-rehearsal-http',
            'is_reported': True,
        })
        self.assertEqual(communication.status_code, 200, communication.get_json())

        outreach = client.post(f'/api/customers/{customer_id}/outreach', json={
            'subject': 'API acceptance quotation',
            'content': 'Quotation sent from the rehearsal workflow.',
            'sent_date': '2026-09-13',
            'reply_status': 'pending',
        })
        self.assertEqual(outreach.status_code, 201, outreach.get_json())
        outreach_id = outreach.get_json()['outreach']['id']
        self.assertEqual(
            client.put(f'/api/outreach/{outreach_id}', json={
                'reply_status': 'replied', 'reply_content': 'Received', 'reply_date': '2026-09-14',
            }).status_code,
            200,
        )
        self.assertEqual(
            client.post(f'/api/customers/{customer_id}/priority', json={'action': 'pin'}).status_code,
            200,
        )

        browser_capture = client.post('/api/extension/communications', json={
            'customer_id': customer_id,
            'contact_id': contact_id,
            'content': 'Browser capture confirms the buyer conversation.',
            'direction': 'inbound',
            'follow_date': '2026-09-14',
            'channel': 'whatsapp',
            'source_url': 'https://chat.api-acceptance.example/thread/1',
            'account': 'api-buyer@api-acceptance.example',
            'conversation_identity': 'api-acceptance-thread',
            'adapter_version': 'rehearsal-extension',
            'messages': [{
                'fingerprint': 'api-acceptance-browser-1', 'time': '2026-09-14 12:00:00',
                'direction': 'inbound', 'sender': 'API Acceptance Buyer', 'text': 'Confirmed',
            }],
        })
        self.assertEqual(browser_capture.status_code, 200, browser_capture.get_json())
        module._INBOX_CACHE.clear()

        inbox_reply = client.post('/api/inbox/reply', json={
            'customer_id': customer_id, 'content': 'A pasted reply for API acceptance.',
        })
        self.assertEqual(inbox_reply.status_code, 200, inbox_reply.get_json())
        inbox_id = inbox_reply.get_json()['id']
        recorded = client.post(f'/api/inbox/{inbox_id}/record-reply')
        self.assertEqual(recorded.status_code, 200, recorded.get_json())
        module._INBOX_CACHE.clear()

        upload = client.post(
            f'/api/customers/{customer_id}/files',
            data={'category': 'quote', 'files': (io.BytesIO(b'postgres rehearsal file'), 'acceptance.txt')},
            content_type='multipart/form-data',
        )
        self.assertEqual(upload.status_code, 200, upload.get_json())
        file_id = upload.get_json()['created'][0]['id']
        files = client.get(f'/api/customers/{customer_id}/files')
        self.assertEqual(files.status_code, 200, files.get_json())
        self.assertTrue(any(item['id'] == file_id for item in files.get_json()['files']))
        download = client.get(f'/api/customers/{customer_id}/files/{file_id}/download')
        self.assertEqual(download.status_code, 200)
        download.close()
        self.assertEqual(client.delete(f'/api/customers/{customer_id}/files/{file_id}').status_code, 200)
        self.assertEqual(client.post(f'/api/customers/{customer_id}/files/{file_id}/restore').status_code, 200)

        get_routes = (
            f'/api/customers?search=API+Acceptance', f'/api/customers/{customer_id}/summary',
            f'/api/customers/{customer_id}/timeline', f'/api/customers/{customer_id}/tasks',
            f'/api/customers/{customer_id}/contacts', f'/api/customers/{customer_id}/outreach',
            f'/api/customers/{customer_id}/context', '/api/inbox', '/api/inbox/counts',
            '/api/agent/brief/today', f'/api/agent/customers/{customer_id}/workspace',
            f'/api/agent/customers/{customer_id}/timeline', '/api/agent/messages/search?q=Confirmed',
            '/api/stats', '/api/overview/stats', '/api/overview/all-customers',
            f'/api/overview/customers/hamid/{customer_id}', '/api/calendar/refresh',
            '/api/system', '/api/health',
        )
        for route in get_routes:
            response = client.post(route) if route == '/api/calendar/refresh' else client.get(route)
            self.assertEqual(response.status_code, 200, (route, response.get_json(silent=True)))

        # Email validation is a normal Trosa fact and must persist in the
        # canonical verification relation, including the cached read path.
        verification_result = {
            'email': 'api-verify@api-acceptance.example',
            'normalized': 'api-verify@api-acceptance.example',
            'status': 'valid', 'category': '可以尝试发送',
            'deliverability_status': 'likely_deliverable', 'confidence': 'medium',
            'address_type': 'person', 'risk_flags': [], 'reasons': ['rehearsal'],
            'evidence': [], 'mx': [], 'checked_at': '2026-09-14 12:00:00',
        }
        with mock.patch.object(module, '_verify_email_with_original_rules', return_value=verification_result):
            first_validation = client.post('/api/emails/validate', json={'emails': [verification_result['email']]})
        self.assertEqual(first_validation.status_code, 200, first_validation.get_json())
        second_validation = client.post('/api/emails/validate', json={'emails': [verification_result['email']]})
        self.assertEqual(second_validation.status_code, 200, second_validation.get_json())
        self.assertEqual(second_validation.get_json()['results'][0]['deliverability_status'], 'likely_deliverable')

        row = self.connection.execute(
            '''SELECT customer.name, customer.company, customer.notes, customer.level,
                      count(DISTINCT event.id) AS interactions,
                      count(DISTINCT task.id) FILTER (WHERE task.status='open') AS open_tasks,
                      count(DISTINCT file_object.id) AS files
                 FROM trosa.account_legacy_refs ref
                 JOIN trosa.customer_records customer ON customer.id=ref.legacy_customer_id
                 LEFT JOIN trosa.timeline_events event ON event.account_id=ref.account_id
                 LEFT JOIN trosa.tasks task ON task.account_id=ref.account_id
                 LEFT JOIN core.entity_files entity_file ON entity_file.account_id=ref.account_id
                 LEFT JOIN core.file_objects file_object ON file_object.id=entity_file.file_object_id
                WHERE ref.organization_id=trosa.compat_org_id()
                  AND ref.legacy_user_id=? AND ref.legacy_customer_id=?
                GROUP BY customer.name, customer.company, customer.notes, customer.level''',
            ('hamid', customer_id),
        ).fetchone()
        self.assertEqual(row['name'], 'API Acceptance Customer Updated')
        self.assertEqual(row['company'], 'API Acceptance Co')
        self.assertEqual(row['level'], 'B+')
        self.assertGreaterEqual(row['interactions'], 3)
        self.assertGreaterEqual(row['open_tasks'], 1)
        self.assertEqual(row['files'], 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM trosa.email_verifications WHERE email=?",
                (verification_result['email'],),
            ).fetchone()[0],
            1,
        )

    def test_gmail_and_smtp_worker_write_canonical_facts(self):
        """Exercise Gmail ingestion and the SMTP worker without external calls."""
        import email_verifier
        import gmail_sync
        from config import EMAIL_VERIFICATION_CONFIG

        encoded = base64.urlsafe_b64encode(b'Please confirm the rehearsal quotation.').decode().rstrip('=')
        raw = {
            'id': 'pg-rehearsal-gmail-1', 'threadId': 'pg-rehearsal-thread',
            'internalDate': '1780000000000',
            'payload': {'mimeType': 'multipart/alternative', 'headers': [
                {'name': 'From', 'value': 'Rehearsal Buyer <buyer@rehearsal.example>'},
                {'name': 'To', 'value': 'Owner <owner@rehearsal.example>'},
                {'name': 'Subject', 'value': 'Rehearsal quotation'},
            ], 'parts': [{'mimeType': 'text/plain', 'body': {'data': encoded}}]},
        }
        message = gmail_sync.normalize_gmail_message(raw, 'owner@rehearsal.example')
        stored = gmail_sync._store_message('hamid', 'owner@rehearsal.example', message, 'PG Gmail rehearsal')
        self.assertEqual(stored['state'], 'matched', stored)
        self.assertEqual(
            self.connection.execute(
                "SELECT match_status FROM trosa.email_message_receipts WHERE provider_message_id=?",
                ('pg-rehearsal-gmail-1',),
            ).fetchone()[0],
            'matched',
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM trosa.communication_sources WHERE channel='gmail'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(gmail_sync._store_message('hamid', 'owner@rehearsal.example', message, 'duplicate')['state'], 'duplicate')

        bounce = gmail_sync.normalize_gmail_message({
            'id': 'pg-rehearsal-gmail-bounce', 'threadId': 'pg-rehearsal-thread-bounce',
            'internalDate': '1780000000000',
            'payload': {'mimeType': 'multipart/alternative', 'headers': [
                {'name': 'From', 'value': 'Mail Delivery Subsystem <mailer-daemon@googlemail.com>'},
                {'name': 'To', 'value': 'Owner <owner@rehearsal.example>'},
                {'name': 'Subject', 'value': 'Delivery Status Notification'},
            ], 'parts': [{'mimeType': 'text/plain', 'body': {'data': base64.urlsafe_b64encode((
                '由于系统找不到电子邮件地址 buyer@rehearsal.example，无法递送该邮件。'
                '响应如下：550 5.1.1 The email account that you tried to reach does not exist.'
            ).encode()).decode().rstrip('=')}}]},
        }, 'owner@rehearsal.example')
        bounce_result = gmail_sync._store_message('hamid', 'owner@rehearsal.example', bounce, 'PG Gmail bounce')
        self.assertEqual(bounce_result['state'], 'delivery_notice', bounce_result)
        self.assertIsNone(bounce_result.get('inbox_item_id'))
        bounce_rows = self.connection.execute(
            """SELECT event_type, source, count(*) FROM trosa.email_delivery_events
                WHERE source='gmail-bounce' GROUP BY event_type, source"""
        ).fetchall()
        normalized = [
            tuple(dict(row).values())
            if not isinstance(row, (tuple, list)) else tuple(row)
            for row in bounce_rows
        ]
        self.assertEqual(sorted(normalized), [('bounced', 'gmail-bounce', 1)])
        self.assertEqual(
            self.connection.execute(
                '''SELECT count(*) FROM trosa.inbox_items
                    WHERE item_type='gmail_capture' AND status='open' '''
            ).fetchone()[0],
            0,
        )
        # 历史上已 open 的同类噪声会被 GET /api/inbox 的一次性清扫自动归档。
        module = self._app_module()
        module._create_inbox_item(
            self.connection, item_type='gmail_capture', title='待归属 Gmail 邮件：Mail Delivery Subsystem',
            content=json.dumps({'messages': [{
                'sender_email': 'mailer-daemon@googlemail.com',
                'sender': 'Mail Delivery Subsystem', 'subject': 'Delivery Status Notification',
                'text': '550 5.1.1 buyer@rehearsal.example does not exist.',
            }]}),
            dedupe_key='gmail:owner@rehearsal.example:legacy-noise-1',
            status='open', created_at='2026-09-17 09:00:00',
        )
        self.assertEqual(module._archive_noise_gmail_captures(self.connection), 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM trosa.inbox_items WHERE item_type='gmail_capture' AND status='open'"
            ).fetchone()[0],
            0,
        )

        verification = {
            'email': 'smtp-worker@rehearsal.example',
            'normalized': 'smtp-worker@rehearsal.example',
            'deliverability_status': 'likely_deliverable', 'confidence': 'medium',
            'address_type': 'person', 'risk_flags': [], 'reasons': [],
            'evidence': [], 'mx': [{'host': 'mx.rehearsal.example', 'priority': 10}],
            'checked_at': '2026-09-14 12:00:00',
        }
        module = self._app_module()
        with mock.patch.dict(EMAIL_VERIFICATION_CONFIG, {
            'smtp_probe_enabled': True, 'smtp_helo_host': 'rehearsal.local',
            'smtp_mail_from': 'verify@rehearsal.example',
        }, clear=False):
            connection = self.connection
            module._save_email_verification(connection, verification)
            module._queue_smtp_verification(connection, verification)
            connection.commit()
            with mock.patch.object(email_verifier, '_probe_mx', return_value={
                'outcome': 'accepted', 'smtp_code': '250', 'enhanced_status': '2.1.5',
                'diagnostic_text': 'accepted by rehearsal MTA', 'remote_mta': 'mx.rehearsal.example',
            }):
                worker = email_verifier.process_pending_email_verification_jobs(5)
        self.assertEqual(worker['processed'], 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM trosa.email_verification_jobs WHERE email=?",
                (verification['email'],),
            ).fetchone()[0],
            'completed',
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM trosa.email_delivery_events WHERE source='smtp_worker'"
            ).fetchone()[0],
            1,
        )

    def test_sela_integrations_use_canonical_facts(self):
        """Exercise the complete Sela bridge against canonical PostgreSQL facts."""
        module = self._app_module()
        module._INBOX_CACHE.clear()
        client = module.app.test_client()
        self.assertEqual(
            client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200
        )

        source_id = 'pg-sela-canonical-acceptance'
        prospect = {
            'source_id': source_id,
            'company': 'Canonical Sela Prospect Co',
            'website': 'https://canonical-sela.example',
            'country': 'US',
            'business_type': 'acrylic signage manufacturer',
            'contact': {
                'name': 'Canonical Prospect Buyer',
                'title': 'Purchasing Manager',
                'email': 'canonical-prospect@canonical-sela.example',
                'phone': '+1-555-0201',
            },
            'status': 'qualified',
            'research_status': 'complete',
            'confidence': 'high',
            'reason': 'Public product catalogue confirms acrylic-sheet demand.',
            'evidence': [{
                'text': 'Acrylic sheet product catalogue',
                'source_url': 'https://canonical-sela.example/catalogue',
            }],
            'source_urls': ['https://canonical-sela.example/catalogue'],
            'subject': 'Acrylic sheet supply introduction',
            'email_draft': 'A reviewed introduction for the rehearsal only.',
            'outreach_status': 'SENT',
            'sent_at': '2026-09-20T10:00:00+08:00',
            'gmail_message_id': 'pg-sela-canonical-outbound-1',
        }
        prospect_headers = {
            'X-Idempotency-Key': 'pg-sela-canonical-prospect-1',
        }
        created = client.post(
            '/api/integrations/sela/prospects',
            headers=prospect_headers,
            json={'prospect': prospect},
        )
        self.assertEqual(created.status_code, 200, created.get_json())
        created_body = created.get_json()
        self.assertTrue(created_body['created'])
        prospect_customer_id = int(created_body['trosa_id'])
        replay = client.post(
            '/api/integrations/sela/prospects',
            headers=prospect_headers,
            json={'prospect': prospect},
        )
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertEqual(replay.get_json()['trosa_id'], prospect_customer_id)

        prospect_list = client.get('/api/integrations/sela/prospects?limit=100')
        self.assertEqual(prospect_list.status_code, 200, prospect_list.get_json())
        prospect_view = next(
            item for item in prospect_list.get_json()['prospects']
            if item['id'] == source_id
        )
        self.assertEqual(prospect_view['trosa_id'], prospect_customer_id)
        self.assertEqual(prospect_view['outreach_status'], 'SENT')

        # A confirmed outreach must close the prospect's legacy routine
        # development task instead of leaving it in Today as an overdue follow-up.
        import db
        db.set_db_user('hamid')
        dev_task_id = module._merge_open_task(
            self.connection, customer_id=prospect_customer_id,
            title='开发新客户: Canonical Sela Prospect Co',
            content='开发新客户。\n备注：新开发流程实验中。',
            reason='官网导入，待首次联系', due_on='2026-09-19', now='2026-08-04 10:00:00',
        )
        self.connection.commit()
        self.assertEqual(
            self.connection.execute(
                '''SELECT task.status FROM trosa.tasks task
                    JOIN trosa.legacy_row_refs ref ON ref.target_id=task.id
                   WHERE ref.table_name='reminders' AND ref.legacy_user_id='hamid'
                     AND ref.legacy_id=?''', (dev_task_id,)
            ).fetchone()['status'],
            'open',
        )
        sent_again = dict(prospect)
        sent_again['sent_at'] = '2026-09-21T10:00:00+08:00'
        confirmed_again = client.post(
            '/api/integrations/sela/prospects',
            headers={'X-Idempotency-Key': 'pg-sela-canonical-prospect-2'},
            json={'prospect': sent_again},
        )
        self.assertEqual(confirmed_again.status_code, 200, confirmed_again.get_json())
        self.assertEqual(
            self.connection.execute(
                '''SELECT task.status FROM trosa.tasks task
                    JOIN trosa.legacy_row_refs ref ON ref.target_id=task.id
                   WHERE ref.table_name='reminders' AND ref.legacy_user_id='hamid'
                     AND ref.legacy_id=?''', (dev_task_id,)
            ).fetchone()['status'],
            'done',
        )

        # Verification is a shared Trosa fact, not an Agent-side cache.
        verification_result = {
            'email': 'canonical-prospect@canonical-sela.example',
            'normalized': 'canonical-prospect@canonical-sela.example',
            'status': 'valid', 'category': '可以尝试发送',
            'deliverability_status': 'likely_deliverable', 'confidence': 'medium',
            'address_type': 'person', 'risk_flags': [], 'reasons': ['rehearsal'],
            'evidence': [], 'mx': [], 'checked_at': '2026-09-20 11:00:00',
        }
        with mock.patch.object(
            module, '_verify_email_with_original_rules', return_value=verification_result
        ):
            verified = client.post(
                f'/api/integrations/sela/prospects/{source_id}/email-verification',
                json={'email': verification_result['email']},
            )
        self.assertEqual(verified.status_code, 200, verified.get_json())
        self.assertEqual(
            self.connection.execute(
                "SELECT deliverability_status FROM trosa.email_verifications WHERE email=?",
                (verification_result['email'],),
            ).fetchone()['deliverability_status'],
            'likely_deliverable',
        )

        canonical = self.connection.execute(
            '''SELECT a.id AS account_id, a.display_name, c.canonical_name,
                      profile.source_id, message.subject, message.provider_message_id,
                      message.reply_status, count(delivery.id) AS delivery_events
                 FROM trosa.account_legacy_refs ref
                 JOIN trosa.accounts a ON a.id=ref.account_id
                 JOIN core.companies c ON c.id=a.company_id
                 JOIN trosa.agent_prospect_profiles profile
                   ON profile.organization_id=ref.organization_id
                  AND profile.legacy_user_id=ref.legacy_user_id
                  AND profile.customer_id=ref.legacy_customer_id
                 JOIN trosa.outreach_messages message ON message.account_id=a.id
                 LEFT JOIN trosa.email_delivery_events delivery
                   ON delivery.outreach_message_id=message.id
                WHERE ref.organization_id=trosa.compat_org_id()
                  AND ref.legacy_user_id=? AND ref.legacy_customer_id=?
                GROUP BY a.id, a.display_name, c.canonical_name,
                         profile.source_id, message.subject, message.provider_message_id,
                         message.reply_status''',
            ('hamid', prospect_customer_id),
        ).fetchone()
        self.assertEqual(canonical['display_name'], 'Canonical Sela Prospect Co')
        self.assertEqual(canonical['canonical_name'], 'Canonical Sela Prospect Co')
        self.assertEqual(canonical['source_id'], source_id)
        self.assertEqual(canonical['provider_message_id'], 'pg-sela-canonical-outbound-1')
        self.assertEqual(canonical['reply_status'], 'pending')
        self.assertEqual(canonical['delivery_events'], 1)

        # Existing-customer work is a proposal/confirmation flow; it must not
        # bypass the canonical audit and Task relations.
        context = client.get('/api/integrations/sela/customers/1/context')
        self.assertEqual(context.status_code, 200, context.get_json())
        follow_up = client.post(
            '/api/integrations/sela/follow-up',
            headers={'X-Idempotency-Key': 'pg-sela-follow-up-1'},
            json={
                'customer_id': 1,
                'revision': context.get_json()['revision'],
                'assessment': 'The fixture customer needs a confirmed quotation follow-up.',
                'evidence': [{
                    'source': 'trosa.customer_record',
                    'quote': 'The customer has an open quotation task.',
                }],
                'action': 'create_task',
                'payload': {
                    'title': 'Sela canonical quotation follow-up',
                    'due_date': '2026-09-26',
                    'reason': 'rehearsal evidence',
                },
            },
        )
        self.assertEqual(follow_up.status_code, 201, follow_up.get_json())
        proposal_id = follow_up.get_json()['proposal_id']
        confirmed = client.post(f'/api/agent/proposals/{proposal_id}/confirm')
        self.assertEqual(confirmed.status_code, 200, confirmed.get_json())
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM audit.agent_proposals WHERE id=trosa.compat_uuid(?)",
                (f'agent-proposal:hamid:{proposal_id}',),
            ).fetchone()['status'],
            'confirmed',
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM trosa.tasks WHERE title=?",
                ('Sela canonical quotation follow-up',),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT item.status FROM trosa.inbox_items item "
                "JOIN trosa.legacy_row_refs ref ON ref.target_id=item.id "
                "WHERE ref.table_name='inbox_items' AND ref.legacy_user_id=? "
                "AND item.legacy_payload->>'compat_dedupe_key'=?",
                ('hamid', f'sela_proposal:{proposal_id}'),
            ).fetchone()['status'],
            'resolved',
        )

        # Replies update the canonical timeline, outreach message, and the
        # integration receipt exactly once, even when replayed.
        reply_payload = {
            'candidate_id': source_id,
            'trosa_id': prospect_customer_id,
            'reply': {
                'body': 'Please send the acrylic sheet specification.',
                'subject': 'Re: Acrylic sheet supply introduction',
                'from': 'Canonical Prospect Buyer <canonical-prospect@canonical-sela.example>',
                'message_id': 'pg-sela-canonical-reply-1',
                'received_at': '2026-09-21T09:30:00+08:00',
            },
            'action': {
                'route': 'FOLLOW_UP',
                'intent': 'INTERESTED',
                'event': 'INTERESTED',
                'name': 'send specification',
                'reason': 'Prospect asked for the specification.',
                'next_task': {
                    'title': 'Send canonical prospect specification',
                    'due_date': '2026-09-27',
                },
            },
            'idempotency_key': 'pg-sela-reply-1',
        }
        replied = client.post(
            '/api/integrations/sela/reply',
            headers={'X-Idempotency-Key': 'pg-sela-reply-1'},
            json=reply_payload,
        )
        self.assertEqual(replied.status_code, 200, replied.get_json())
        replayed_reply = client.post(
            '/api/integrations/sela/reply',
            headers={'X-Idempotency-Key': 'pg-sela-reply-1'},
            json=reply_payload,
        )
        self.assertEqual(replayed_reply.status_code, 200, replayed_reply.get_json())
        self.assertEqual(replayed_reply.get_json()['activity_id'], replied.get_json()['activity_id'])
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM trosa.timeline_events event "
                "JOIN trosa.account_legacy_refs ref ON ref.account_id=event.account_id "
                "WHERE ref.legacy_user_id=? AND ref.legacy_customer_id=? "
                "AND event.event_type='customer_reply' AND event.source_reference=?",
                ('hamid', prospect_customer_id, 'pg-sela-canonical-reply-1'),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT reply_status FROM trosa.outreach_messages WHERE provider_message_id=?",
                ('pg-sela-canonical-outbound-1',),
            ).fetchone()['reply_status'],
            'replied',
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM audit.integration_receipts WHERE integration=? AND idempotency_key=?",
                ('sela', 'hamid:pg-sela-reply-1'),
            ).fetchone()[0],
            1,
        )

        # Sela human needs are Inbox facts and their resolution is an explicit
        # timeline decision, not Agent-local state.
        need = client.post(
            '/api/integrations/sela/needs',
            headers={'X-Idempotency-Key': 'pg-sela-need-1'},
            json={
                'request': {
                    'source_id': source_id,
                    'customer_id': prospect_customer_id,
                    'need': 'Approve sending the requested specification.',
                    'context': 'The prospect replied with a concrete request.',
                    'proposal': 'Send the current specification PDF.',
                    'kind': 'DECISION',
                    'severity': 'AMBER',
                    'dedupe_key': 'pg-sela-need-dedupe-1',
                },
            },
        )
        self.assertEqual(need.status_code, 200, need.get_json())
        need_id = need.get_json()['item']['trosa_inbox_id']
        listed_needs = client.get('/api/integrations/sela/needs?status=open')
        self.assertEqual(listed_needs.status_code, 200, listed_needs.get_json())
        self.assertTrue(any(item['trosa_inbox_id'] == need_id for item in listed_needs.get_json()['needs']))
        resolved_need = client.post(
            f'/api/integrations/sela/needs/{need_id}/resolve',
            headers={'X-Idempotency-Key': 'pg-sela-need-resolve-1'},
            json={
                'action': 'approve',
                'resolution': 'Approved after reviewing the concrete request.',
            },
        )
        self.assertEqual(resolved_need.status_code, 200, resolved_need.get_json())
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM trosa.inbox_items item "
                "JOIN trosa.legacy_row_refs ref ON ref.target_id=item.id "
                "WHERE ref.table_name='inbox_items' AND ref.legacy_user_id=? AND ref.legacy_id=?",
                ('hamid', need_id),
            ).fetchone()['status'],
            'resolved',
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM trosa.timeline_events event "
                "JOIN trosa.account_legacy_refs ref ON ref.account_id=event.account_id "
                "WHERE ref.legacy_user_id=? AND ref.legacy_customer_id=? "
                "AND event.event_type='agent_decision'",
                ('hamid', prospect_customer_id),
            ).fetchone()[0],
            1,
        )

        exclusion = client.post(
            '/api/integrations/sela/exclusions',
            headers={'X-Idempotency-Key': 'pg-sela-exclusion-1'},
            json={'record': {
                'source': 'sela_registry',
                'source_id': 'pg-sela-exclusion-1',
                'canonical_name': 'Canonical Excluded Supplier',
                'domains': ['excluded-supplier.example'],
                'reason': 'Supplier is outside the target market.',
            }},
        )
        self.assertEqual(exclusion.status_code, 200, exclusion.get_json())
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM trosa.business_exclusions WHERE source=? AND source_id=?",
                ('sela_registry', 'pg-sela-exclusion-1'),
            ).fetchone()[0],
            1,
        )
        exclusions = client.get('/api/integrations/sela/exclusions')
        self.assertEqual(exclusions.status_code, 200, exclusions.get_json())
        self.assertTrue(any(item['source_id'] == 'pg-sela-exclusion-1'
                            for item in exclusions.get_json()['records']))

    def test_z_agent_gateway_undo_and_operation_audit_boundary(self):
        """Agent/audit writes stay canonical while old integer views remain projections."""
        from tools.postgres_rehearsal import load_fixture

        customer_id = load_fixture()['customer_id']
        module = self._app_module()
        session_client = module.app.test_client()
        self.assertEqual(session_client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)
        proposal_token = session_client.post(
            '/api/agent-gateway/tokens', json={'scopes': ['crm:propose']}
        ).get_json()['data']['token']
        write_token = session_client.post(
            '/api/agent-gateway/tokens', json={'scopes': ['crm:write']}
        ).get_json()['data']['token']
        read_token = session_client.post(
            '/api/agent-gateway/tokens', json={'scopes': ['crm:read']}
        ).get_json()['data']['token']
        gateway = module.app.test_client()

        proposal_body = {
            'action': 'record_communication', 'customer_id': customer_id,
            'payload': {
                'content': 'Agent audit rehearsal fact', 'direction': 'inbound',
                'activity_type': 'email', 'follow_date': '2026-09-23',
                'source': 'agent-audit-rehearsal',
            },
        }
        proposal_headers = {
            'Authorization': 'Bearer ' + proposal_token,
            'Idempotency-Key': 'agent-audit-proposal-1',
        }
        created = gateway.post('/api/gateway/proposals', headers=proposal_headers, json=proposal_body)
        self.assertEqual(created.status_code, 201, created.get_json())
        proposal_id = created.get_json()['data']['proposal']['id']
        self.assertEqual(
            gateway.post('/api/gateway/proposals', headers=proposal_headers, json=proposal_body).status_code,
            200,
        )
        fetched = session_client.get(f'/api/agent/proposals/{proposal_id}')
        self.assertEqual(fetched.status_code, 200, fetched.get_json())
        confirmed = session_client.post(f'/api/agent/proposals/{proposal_id}/confirm')
        self.assertEqual(confirmed.status_code, 200, confirmed.get_json())

        task_body = {
            'action': 'create_task', 'customer_id': 1,
            'payload': {'title': 'Agent audit rehearsal task', 'due_date': '2026-09-24'},
        }
        task_headers = {
            'Authorization': 'Bearer ' + write_token,
            'Idempotency-Key': 'agent-audit-write-1',
        }
        written = gateway.post('/api/gateway/actions', headers=task_headers, json=task_body)
        self.assertEqual(written.status_code, 201, written.get_json())
        action_id = written.get_json()['data']['action']['id']
        replay = gateway.post('/api/gateway/actions', headers=task_headers, json=task_body)
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertEqual(replay.get_json()['data']['action']['id'], action_id)
        recent = gateway.get(
            '/api/gateway/actions/recent', headers={'Authorization': 'Bearer ' + read_token}
        )
        self.assertEqual(recent.status_code, 200, recent.get_json())
        self.assertTrue(any(row['action_id'] == action_id for row in recent.get_json()['data']['actions']))

        undone = gateway.post(
            f'/api/gateway/actions/{action_id}/undo',
            headers={'Authorization': 'Bearer ' + write_token},
        )
        self.assertEqual(undone.status_code, 200, undone.get_json())

        # An old client write is still accepted, but the bridge records the
        # canonical operation fact and only then updates the integer adapter.
        self.connection.execute(
            '''INSERT INTO trade_os_compat.operation_logs
               (action, target_type, target_id, details, created_at)
               VALUES (?, ?, ?, ?, ?)''',
            ('COMPAT_AUDIT_REHEARSAL', 'customer', 1, 'compatibility boundary', '2026-09-24 10:00:00'),
        )
        self.connection.commit()
        operation = self.connection.execute(
            '''SELECT action, target_type, target_id, details
                 FROM audit.operation_log_events
                WHERE organization_id=trosa.compat_org_id()
                  AND legacy_user_id=? AND action=?
                ORDER BY occurred_at DESC, legacy_id DESC LIMIT 1''',
            ('hamid', 'COMPAT_AUDIT_REHEARSAL'),
        ).fetchone()
        self.assertEqual(dict(operation), {
            'action': 'COMPAT_AUDIT_REHEARSAL', 'target_type': 'customer',
            'target_id': 1, 'details': 'compatibility boundary',
        })
        import psycopg
        compat_id = self.connection.execute(
            '''SELECT id FROM trade_os_compat.operation_logs
                WHERE user_id=? AND action=?
                ORDER BY id DESC LIMIT 1''',
            ('hamid', 'COMPAT_AUDIT_REHEARSAL'),
        ).fetchone()[0]

        with self.assertRaises(psycopg.errors.RaiseException):
            self.connection.execute(
                '''DELETE FROM trade_os_compat.operation_logs
                   WHERE id=?''',
                (compat_id,),
            )
        self.connection.rollback()
        self.assertIsNotNone(
            self.connection.execute(
                '''SELECT 1 FROM audit.operation_log_events
                    WHERE organization_id=trosa.compat_org_id()
                      AND legacy_user_id=? AND action=?''',
                ('hamid', 'COMPAT_AUDIT_REHEARSAL'),
            ).fetchone()
        )

        rows = self.connection.execute(
            '''SELECT p.status, a.status AS action_status, u.status AS undo_status
                 FROM audit.agent_proposals p
                 JOIN audit.agent_actions a
                   ON a.organization_id=p.organization_id
                  AND a.legacy_user_id=?
                 JOIN audit.undo_snapshots u
                   ON u.organization_id=p.organization_id
                  AND u.legacy_user_id=?
                WHERE p.organization_id=trosa.compat_org_id()
                  AND p.id=trosa.compat_uuid(?)
                ORDER BY a.created_at DESC, u.created_at DESC LIMIT 1''',
            ('hamid', 'hamid', f'agent-proposal:hamid:{proposal_id}'),
        ).fetchone()
        self.assertIsNotNone(rows)
        self.assertEqual(rows['status'], 'confirmed')
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM audit.agent_actions WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=? AND action_id=?",
                ('hamid', action_id),
            ).fetchone()['status'],
            'undone',
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM audit.undo_snapshots WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=? AND token=?",
                ('hamid', written.get_json()['data']['action']['undo_token']),
            ).fetchone()['status'],
            'undone',
        )
        operation_logs = session_client.get('/api/logs?action=COMPAT_AUDIT_REHEARSAL')
        self.assertEqual(operation_logs.status_code, 200, operation_logs.get_json())
        self.assertTrue(operation_logs.get_json())


    def test_concurrent_same_day_task_merge_creates_single_task(self):
        """Two writers racing on one due date must not duplicate the Task."""
        import threading

        import db
        import trosa_domain
        from tools.postgres_rehearsal import load_fixture

        customer_id = load_fixture()['customer_id']
        due_on = '2031-05-01'
        results, errors = [], []

        def merge(index):
            try:
                db.set_db_user('hamid')
                connection = db.get_db()
                try:
                    connection.execute('BEGIN')
                    results.append(trosa_domain.merge_open_task(
                        connection, customer_id=customer_id, title=f'race {index}',
                        content='race', reason='race', due_on=due_on,
                        now='2026-09-14 10:00:00',
                    ))
                    connection.commit()
                finally:
                    connection.close()
            except Exception as exc:  # pragma: no cover - serialized path
                errors.append(repr(exc))
            finally:
                db.set_db_user(None)

        threads = [threading.Thread(target=merge, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(len(set(results)), 1, results)
        count = self.connection.execute(
            """SELECT count(*) FROM trosa.tasks task
                 JOIN trosa.account_legacy_refs ref ON ref.account_id=task.account_id
                WHERE ref.organization_id=trosa.compat_org_id()
                  AND ref.legacy_user_id='hamid' AND ref.legacy_customer_id=?
                  AND task.status='open' AND task.task_type='follow_up'
                  AND trosa.compat_local_date(task.due_at)=?""",
            (customer_id, due_on),
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_today_projection_dedupes_same_task_across_customer_aliases(self):
        """One canonical task must not fan out into two Today rows."""
        import trosa_domain
        from tools.postgres_rehearsal import load_fixture

        ids = load_fixture()
        customer_id = ids['customer_id']
        task_id = ids['task_id']
        alias_customer_id = 990001
        alias_task_id = 990001
        account = self.connection.execute(
            '''SELECT account_id FROM trosa.account_legacy_refs
                WHERE organization_id=trosa.compat_org_id()
                  AND legacy_user_id='hamid' AND legacy_customer_id=?''',
            (customer_id,),
        ).fetchone()['account_id']
        try:
            self.connection.execute(
                '''INSERT INTO trosa.account_legacy_refs
                   (organization_id, legacy_user_id, legacy_customer_id, account_id,
                    source_db, legacy_payload)
                   VALUES (trosa.compat_org_id(), 'hamid', ?, ?,
                           'today-alias-probe', ?::jsonb)''',
                (alias_customer_id, account,
                 '{"name":"Historical alias","company":"Historical alias"}'),
            )
            self.connection.execute(
                '''INSERT INTO trosa.legacy_row_refs
                   (organization_id, legacy_user_id, table_name, legacy_id, target_id)
                   SELECT trosa.compat_org_id(), 'hamid', 'reminders', ?, target_id
                     FROM trosa.legacy_row_refs
                    WHERE organization_id=trosa.compat_org_id()
                      AND legacy_user_id='hamid' AND table_name='reminders'
                      AND legacy_id=?''',
                (alias_task_id, task_id),
            )
            self.connection.commit()

            rows = self.connection.execute(
                '''SELECT id, customer_id FROM trosa.today_tasks
                    WHERE id IN (?, ?)''',
                (task_id, alias_task_id),
            ).fetchall()
            self.assertEqual(len(rows), 1, rows)
            self.assertEqual(rows[0]['id'], task_id)
            self.assertEqual(rows[0]['customer_id'], customer_id)
            module = self._app_module()
            self.assertEqual(
                module._reminder_with_customer(self.connection, task_id)['customer_id'],
                customer_id,
            )
            self.assertEqual(
                module._snapshot_entity(self.connection, 'reminders', task_id)['customer_id'],
                customer_id,
            )
            projected = [
                row for row in trosa_domain.today_tasks(
                    self.connection, due_on_or_before='2031-12-31'
                )
                if row['id'] in (task_id, alias_task_id)
            ]
            self.assertEqual(len(projected), 1, projected)
        finally:
            self.connection.execute(
                '''DELETE FROM trosa.legacy_row_refs
                    WHERE organization_id=trosa.compat_org_id()
                      AND legacy_user_id='hamid' AND table_name='reminders'
                      AND legacy_id=?''',
                (alias_task_id,),
            )
            self.connection.execute(
                '''DELETE FROM trosa.account_legacy_refs
                    WHERE organization_id=trosa.compat_org_id()
                      AND legacy_user_id='hamid' AND legacy_customer_id=?''',
                (alias_customer_id,),
            )
            self.connection.commit()

    def test_archiving_customer_removes_its_task_from_today(self):
        """An archived Customer's open Task must leave 今日跟进 immediately.

        Archiving is the per-user soft delete in set_customer_deleted, so the
        Today view must honor that per-alias payload instead of only the
        shared accounts.deleted_at.
        """
        import trosa_domain
        from tools.postgres_rehearsal import load_fixture

        ids = load_fixture()
        customer_id = ids['customer_id']
        task_id = ids['task_id']
        try:
            today = trosa_domain.today_tasks(self.connection, due_on_or_before='2031-12-31')
            self.assertTrue(
                any(row['id'] == task_id and row['customer_id'] == customer_id for row in today)
            )

            trosa_domain.set_customer_deleted(
                self.connection, customer_id=customer_id, deleted=True,
                changed_at='2026-09-14T00:00:00',
            )
            self.connection.commit()

            rows = self.connection.execute(
                '''SELECT id, customer_id FROM trosa.today_tasks WHERE id=?''',
                (task_id,),
            ).fetchall()
            self.assertEqual(rows, [])
            today = trosa_domain.today_tasks(self.connection, due_on_or_before='2031-12-31')
            self.assertFalse(
                any(row['id'] == task_id and row['customer_id'] == customer_id for row in today)
            )

            trosa_domain.set_customer_deleted(
                self.connection, customer_id=customer_id, deleted=False,
                changed_at='2026-09-18T00:00:00',
            )
            self.connection.commit()
            today = trosa_domain.today_tasks(self.connection, due_on_or_before='2031-12-31')
            self.assertTrue(
                any(row['id'] == task_id and row['customer_id'] == customer_id for row in today)
            )
        finally:
            trosa_domain.set_customer_deleted(
                self.connection, customer_id=customer_id, deleted=False,
                changed_at='2026-09-18T00:00:00',
            )
            self.connection.commit()

    def test_customer_history_stays_bound_to_its_own_customer_on_merged_account(self):
        """A merged account must never leak history between Customer aliases.

        The Kaze regression: two legacy customers of the same user share one
        canonical account, and the customer history views used to fan every
        event out to both aliases.  Each interaction, outreach, and task must
        be attributed to the customer recorded in its own payload binding.
        """
        import trosa_domain
        from tools.postgres_rehearsal import load_fixture

        ids = load_fixture()
        customer_id = ids['customer_id']
        alias_customer_id = 990002
        account = self.connection.execute(
            '''SELECT account_id FROM trosa.account_legacy_refs
                WHERE organization_id=trosa.compat_org_id()
                  AND legacy_user_id='hamid' AND legacy_customer_id=?''',
            (customer_id,),
        ).fetchone()['account_id']
        try:
            self.connection.execute(
                '''INSERT INTO trosa.account_legacy_refs
                   (organization_id, legacy_user_id, legacy_customer_id, account_id,
                    source_db, legacy_payload)
                   VALUES (trosa.compat_org_id(), 'hamid', ?, ?,
                           'customer-binding-probe', '{"name":"Merged sibling"}')
                   ON CONFLICT (organization_id, legacy_user_id, legacy_customer_id)
                   DO NOTHING''',
                (alias_customer_id, account),
            )
            self.connection.commit()

            alias_interaction_id = trosa_domain.record_external_interaction(
                self.connection,
                customer_id=alias_customer_id,
                content='Alias customer own follow-up',
                occurred_on='2026-09-15',
                direction='outbound',
                source='customer-binding-probe',
                source_reference='customer-binding-probe:interaction',
                activity_type='follow_up',
            )
            alias_task_id = trosa_domain.merge_open_task(
                self.connection, customer_id=alias_customer_id,
                title='Alias customer next step', content='', reason='binding probe',
                due_on='2026-10-01', now='2026-09-15 09:00:00',
            )
            alias_outreach_id = trosa_domain.create_outreach_message(
                self.connection, customer_id=alias_customer_id,
                subject='Alias quote', content='Alias body',
                sent_on='2026-09-14', reply_status='pending',
                created_at='2026-09-15 09:00:00',
            )
            self.connection.commit()

            # New runtime writes carry the explicit customer binding.
            event_row = self.connection.execute(
                '''SELECT payload->>'customer_id' AS bound
                     FROM trosa.timeline_events event
                     JOIN trosa.legacy_row_refs r ON r.target_id=event.id
                      AND r.table_name='follow_up_logs'
                    WHERE r.legacy_user_id='hamid' AND r.legacy_id=?''',
                (alias_interaction_id,),
            ).fetchone()
            self.assertEqual(event_row['bound'], str(alias_customer_id))
            task_row = self.connection.execute(
                '''SELECT legacy_payload->>'customer_id' AS bound
                     FROM trosa.tasks task
                     JOIN trosa.legacy_row_refs r ON r.target_id=task.id
                      AND r.table_name='reminders'
                    WHERE r.legacy_user_id='hamid' AND r.legacy_id=?''',
                (alias_task_id,),
            ).fetchone()
            self.assertEqual(task_row['bound'], str(alias_customer_id))

            # Customer history contains no fan-out: each row lands under
            # exactly one customer, and never under the sibling alias.
            rows = self.connection.execute(
                '''SELECT id, customer_id FROM trosa.customer_interactions
                    WHERE id=?''', (alias_interaction_id,),
            ).fetchall()
            self.assertEqual(len(rows), 1, rows)
            self.assertEqual(rows[0]['customer_id'], alias_customer_id)
            outreach_rows = self.connection.execute(
                '''SELECT customer_id FROM trosa.customer_interactions
                    WHERE kind='email' AND id=?''', (alias_outreach_id,),
            ).fetchall()
            self.assertEqual([row['customer_id'] for row in outreach_rows],
                             [alias_customer_id])

            task_rows = self.connection.execute(
                '''SELECT customer_id FROM trosa.customer_tasks WHERE id=?''',
                (alias_task_id,),
            ).fetchall()
            self.assertEqual([row['customer_id'] for row in task_rows], [alias_customer_id])

            today_rows = self.connection.execute(
                '''SELECT customer_id FROM trosa.today_tasks WHERE id=?''',
                (alias_task_id,),
            ).fetchall()
            self.assertEqual([row['customer_id'] for row in today_rows], [alias_customer_id])

            # The sibling customer sees none of the alias customer's history.
            self.assertEqual(
                self.connection.execute(
                    '''SELECT count(*) FROM trosa.customer_interactions
                        WHERE customer_id=? AND id=?''',
                    (customer_id, alias_interaction_id),
                ).fetchone()[0], 0,
            )
            self.assertEqual(
                self.connection.execute(
                    '''SELECT count(*) FROM trosa.customer_tasks
                        WHERE customer_id=? AND id=?''',
                    (customer_id, alias_task_id),
                ).fetchone()[0], 0,
            )
            facts = trosa_domain.customer_facts(
                self.connection, [customer_id, alias_customer_id],
            )
            self.assertNotEqual(
                facts[customer_id]['next_task_title'], 'Alias customer next step',
            )
            self.assertEqual(
                facts[alias_customer_id]['next_task_title'], 'Alias customer next step',
            )
        finally:
            self.connection.execute(
                '''DELETE FROM trosa.timeline_events event
                     USING trosa.legacy_row_refs r
                    WHERE event.id=r.target_id AND r.table_name='follow_up_logs'
                      AND r.legacy_user_id='hamid' AND r.legacy_id=?''',
                (alias_interaction_id,),
            )
            self.connection.execute(
                '''DELETE FROM trosa.tasks task
                     USING trosa.legacy_row_refs r
                    WHERE task.id=r.target_id AND r.table_name='reminders'
                      AND r.legacy_user_id='hamid' AND r.legacy_id=?''',
                (alias_task_id,),
            )
            self.connection.execute(
                '''DELETE FROM trosa.outreach_messages message
                     USING trosa.legacy_row_refs r
                    WHERE message.id=r.target_id AND r.table_name='outreach_emails'
                      AND r.legacy_user_id='hamid' AND r.legacy_id=?''',
                (alias_outreach_id,),
            )
            self.connection.execute(
                '''DELETE FROM trosa.legacy_row_refs
                    WHERE organization_id=trosa.compat_org_id()
                      AND legacy_user_id='hamid'
                      AND table_name='follow_up_logs' AND legacy_id=?''',
                (alias_interaction_id,),
            )
            self.connection.execute(
                '''DELETE FROM trosa.legacy_row_refs
                    WHERE organization_id=trosa.compat_org_id()
                      AND legacy_user_id='hamid'
                      AND table_name='reminders' AND legacy_id=?''',
                (alias_task_id,),
            )
            self.connection.execute(
                '''DELETE FROM trosa.legacy_row_refs
                    WHERE organization_id=trosa.compat_org_id()
                      AND legacy_user_id='hamid'
                      AND table_name='outreach_emails' AND legacy_id=?''',
                (alias_outreach_id,),
            )
            self.connection.execute(
                '''DELETE FROM trosa.account_legacy_refs
                    WHERE organization_id=trosa.compat_org_id()
                      AND legacy_user_id='hamid' AND legacy_customer_id=?''',
                (alias_customer_id,),
            )
            self.connection.commit()

    def test_customer_boundary_audit_reports_clean_history(self):
        """The full-boundary audit must report zero view or binding issues."""
        from tools import customer_boundary_audit
        from tools.postgres_rehearsal import load_fixture

        load_fixture()
        issues = customer_boundary_audit.verify_views(self.connection, ['hamid', 'amy'])
        self.assertEqual(issues, [], issues)

    def test_compat_view_same_day_insert_merges_into_one_row(self):
        """Direct compat-view writes obey the same one-task invariant."""
        from tools.postgres_rehearsal import load_fixture

        customer_id = load_fixture()["customer_id"]
        due_on = "2031-06-15"
        self.connection.execute(
            """INSERT INTO trosa.reminders
                (customer_id, title, content, reason, remind_date, reminder_type)
               VALUES (%s, 'compat one', 'compat one', 'reason one', %s, 'follow_up')""",
            (customer_id, due_on),
        )
        first = int(self.connection.execute(
            "SELECT current_setting('trade_os.lastrowid', true)"
        ).fetchone()[0])
        self.connection.execute(
            """INSERT INTO trosa.reminders
                (customer_id, title, content, reason, remind_date, reminder_type)
               VALUES (%s, 'compat two', 'compat two', 'reason two', %s, 'follow_up')""",
            (customer_id, due_on),
        )
        second = int(self.connection.execute(
            "SELECT current_setting('trade_os.lastrowid', true)"
        ).fetchone()[0])
        self.assertEqual(second, first)
        count = self.connection.execute(
            """SELECT count(*) FROM trosa.tasks task
                 JOIN trosa.account_legacy_refs ref ON ref.account_id=task.account_id
                WHERE ref.organization_id=trosa.compat_org_id()
                  AND ref.legacy_user_id='hamid' AND ref.legacy_customer_id=%s
                  AND task.status='open' AND task.task_type='follow_up'
                  AND trosa.compat_local_date(task.due_at)=%s""",
            (customer_id, due_on),
        ).fetchone()[0]
        self.assertEqual(count, 1)
        reason = self.connection.execute(
            "SELECT reason FROM trosa.customer_tasks WHERE id=%s", (first,)
        ).fetchone()[0]
        self.assertIn("reason one", reason)
        self.assertIn("reason two", reason)

    def test_concurrent_inbox_dedupe_returns_single_item(self):
        """A replayed dedupe key must resolve to one row, never a 500."""
        import threading

        import db
        import trosa_domain
        from tools.postgres_rehearsal import load_fixture

        customer_id = load_fixture()['customer_id']
        dedupe_key = 'rehearsal-race-dedupe-1'
        results, errors = [], []

        def create(index):
            try:
                db.set_db_user('hamid')
                connection = db.get_db()
                try:
                    connection.execute('BEGIN')
                    results.append(trosa_domain.create_inbox_item(
                        connection, item_type='customer_reply', title=f'race {index}',
                        content='race', customer_id=customer_id, dedupe_key=dedupe_key,
                    ))
                    connection.commit()
                finally:
                    connection.close()
            except Exception as exc:  # pragma: no cover - dedupe must hold
                errors.append(repr(exc))
            finally:
                db.set_db_user(None)

        threads = [threading.Thread(target=create, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 1, results)
        count = self.connection.execute(
            """SELECT count(*) FROM trosa.inbox_items item
                 JOIN trosa.legacy_row_refs ref ON ref.target_id=item.id
                WHERE ref.organization_id=trosa.compat_org_id()
                  AND ref.legacy_user_id='hamid' AND ref.table_name='inbox_items'
                  AND item.legacy_payload->>'compat_dedupe_key'=?""",
            (dedupe_key,),
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_inbox_archive_accepts_visible_dedupe_key(self):
        """The API-visible dedupe_key is the functional raw key and archives the row.

        Canonical storage namespaces the key as ``compat:<user>:<raw>`` for the
        unique index.  The API/UI must expose the same raw key the compatibility
        view exposes; otherwise the visible value cannot be matched on write
        (archive created a second, already-archived row) and raw-key consumers
        such as the Sela request parser and the frontend proposal regex break.
        """
        import db
        from tools.postgres_rehearsal import load_fixture

        load_fixture()
        module = self._app_module()
        raw_key = 'gmail:archive-visible-key@example.com:rehearsal-message-1'

        db.set_db_user('hamid')
        connection = db.get_db()
        try:
            connection.execute('BEGIN')
            item_id = module._create_inbox_item(
                connection, item_type='gmail_capture', title='待归属 Gmail 回复：Archive probe',
                content=json.dumps({'messages': [{'text': 'probe'}]}),
                dedupe_key=raw_key, status='open', created_at='2026-09-17 09:00:00',
            )
            connection.commit()
        finally:
            connection.close()
            db.set_db_user(None)

        module._INBOX_CACHE.clear()
        client = module.app.test_client()
        self.assertEqual(client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)

        visible = next(
            item for item in client.get('/api/inbox').get_json()['items']
            if item['id'] == item_id
        )
        self.assertEqual(visible['dedupe_key'], raw_key)
        archived = client.post('/api/inbox/archive', json={
            'dedupe_key': visible['dedupe_key'],
            'customer_id': visible['customer_id'],
            'item_type': visible['item_type'],
        })
        self.assertEqual(archived.status_code, 200, archived.get_json())

        module._INBOX_CACHE.clear()
        self.assertFalse(any(
            item['id'] == item_id for item in client.get('/api/inbox').get_json()['items']
        ))
        rows = self.connection.execute(
            """SELECT item.status, count(*) AS total FROM trosa.inbox_items item
                 JOIN trosa.legacy_row_refs ref ON ref.target_id=item.id
                WHERE ref.organization_id=trosa.compat_org_id()
                  AND ref.legacy_user_id='hamid' AND ref.table_name='inbox_items'
                  AND COALESCE(item.legacy_payload->>'compat_dedupe_key', item.dedupe_key)=?
                GROUP BY item.status""",
            (raw_key,),
        ).fetchall()
        self.assertEqual([(row['status'], row['total']) for row in rows], [('archived', 1)])

    def test_failed_write_leaves_no_partial_state(self):
        """A mid-transaction failure must roll back the whole business action."""
        import db
        import trosa_domain
        from tools.postgres_rehearsal import load_fixture

        customer_id = load_fixture()['customer_id']
        before_events = self.connection.execute('SELECT count(*) FROM trosa.timeline_events').fetchone()[0]
        before_tasks = self.connection.execute('SELECT count(*) FROM trosa.tasks').fetchone()[0]
        module = self._app_module()

        def boom(connection, cursor, result):
            raise RuntimeError('injected mid-transaction failure')

        db.set_db_user('hamid')
        try:
            with module.app.test_request_context('/'):
                module.g.current_user = 'hamid'
                with self.assertRaises(RuntimeError):
                    module.record_customer_communication(
                        customer_id,
                        {'activity_content': 'atomicity probe', 'follow_date': '2026-09-14'},
                        before_commit=boom,
                    )
        finally:
            db.set_db_user(None)
        self.assertEqual(
            self.connection.execute('SELECT count(*) FROM trosa.timeline_events').fetchone()[0],
            before_events,
        )
        self.assertEqual(
            self.connection.execute('SELECT count(*) FROM trosa.tasks').fetchone()[0],
            before_tasks,
        )
        # The rolled-back legacy id must be reusable without unique conflicts.
        db.set_db_user('hamid')
        retry_connection = db.get_db()
        try:
            retry_connection.execute('BEGIN')
            retry_id = trosa_domain.merge_open_task(
                retry_connection, customer_id=customer_id, title='after rollback',
                content='after rollback', reason='probe', due_on='2031-06-01',
                now='2026-09-14 10:00:00',
            )
            retry_connection.commit()
        finally:
            retry_connection.close()
            db.set_db_user(None)
        self.assertTrue(retry_id)

    def test_reschedule_completed_task_is_rejected_atomically(self):
        """Completing a task wins over a concurrent reschedule; no resurrection."""
        import db
        import trosa_domain
        from tools.postgres_rehearsal import load_fixture

        customer_id = load_fixture()['customer_id']
        db.set_db_user('hamid')
        connection = db.get_db()
        try:
            connection.execute('BEGIN')
            task_id = trosa_domain.merge_open_task(
                connection, customer_id=customer_id, title='reschedule guard',
                content='reschedule guard', reason='probe', due_on='2031-07-01',
                now='2026-09-14 10:00:00',
            )
            connection.commit()
            connection.execute('BEGIN')
            trosa_domain.complete_task(connection, task_id=task_id, completed_at='2026-09-14 10:00:00')
            connection.commit()
            connection.execute('BEGIN')
            with self.assertRaises(ValueError):
                trosa_domain.update_task(
                    connection, task_id=task_id, title='resurrected', content='resurrected',
                    reason='probe', due_on='2031-07-02', now='2026-09-14 10:00:00',
                )
            connection.rollback()
        finally:
            connection.close()
            db.set_db_user(None)
        status = self.connection.execute(
            """SELECT task.status FROM trosa.tasks task
                 JOIN trosa.legacy_row_refs ref ON ref.target_id=task.id
                WHERE ref.organization_id=trosa.compat_org_id()
                  AND ref.legacy_user_id='hamid' AND ref.table_name='reminders'
                  AND ref.legacy_id=?""",
            (task_id,),
        ).fetchone()[0]
        self.assertEqual(status, 'done')

    def test_permanent_delete_removes_children_and_protects_shared_account(self):
        """Permanent delete cascades in FK order; a shared account survives."""
        import db
        import trosa_domain
        from tools.postgres_rehearsal import load_fixture

        load_fixture()
        module = self._app_module()
        module._INBOX_CACHE.clear()
        client = module.app.test_client()
        self.assertEqual(client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)

        created = client.post('/api/customers', json={
            'name': 'Delete Cascade Customer', 'company': 'Delete Cascade Co',
            'country': 'US', 'next_follow_up': '2026-10-01',
            'task_title': 'Cascade task',
            'contacts': [{'name': 'Cascade Buyer', 'email': 'cascade@delete.example'}],
        })
        self.assertEqual(created.status_code, 201, created.get_json())
        customer_id = created.get_json()['id']
        contact_id = client.get(f'/api/customers/{customer_id}').get_json()['contacts'][0]['id']
        self.assertEqual(client.post(f'/api/customers/{customer_id}/follow_history', json={
            'activity_content': 'Cascade fact', 'follow_date': '2026-09-14',
        }).status_code, 200)
        self.assertEqual(client.post(f'/api/customers/{customer_id}/outreach', json={
            'subject': 'Cascade quote', 'content': 'Cascade body', 'sent_date': '2026-09-13',
        }).status_code, 201)
        inbox_id = client.post('/api/inbox/reply', json={
            'customer_id': customer_id, 'content': 'Cascade inbox reply.',
        }).get_json()['id']
        hamid_account = self.connection.execute(
            '''SELECT account_id FROM trosa.account_legacy_refs
                WHERE organization_id=trosa.compat_org_id() AND legacy_user_id='hamid'
                  AND legacy_customer_id=?''', (customer_id,),
        ).fetchone()['account_id']
        company_id = self.connection.execute(
            'SELECT company_id FROM trosa.accounts WHERE id=?', (hamid_account,)).fetchone()[0]

        # Amy owns a separate account for the same shared company.
        amy_customer_id = 920001
        amy_account = self.connection.execute(
            "SELECT trosa.compat_uuid(?)", ('delete-cascade:amy-account',)).fetchone()[0]
        amy_uid = self.connection.execute(
            "SELECT id FROM identity.users WHERE organization_id=trosa.compat_org_id() AND legacy_user_id='amy'",
        ).fetchone()[0]
        self.connection.execute(
            '''INSERT INTO trosa.accounts (id, organization_id, company_id, owner_user_id, display_name)
               VALUES (?, trosa.compat_org_id(), ?, ?, 'Amy same-company customer')''',
            (amy_account, company_id, amy_uid),
        )
        self.connection.execute(
            '''INSERT INTO trosa.account_legacy_refs
                   (organization_id, legacy_user_id, legacy_customer_id, account_id, source_db, legacy_payload)
               VALUES (trosa.compat_org_id(), 'amy', ?, ?, 'delete-cascade', '{}'::jsonb)''',
            (amy_customer_id, amy_account),
        )
        self.connection.execute(
            '''INSERT INTO trosa.customer_details (account_id, notes) VALUES (?, 'Amy detail')''',
            (amy_account,),
        )
        self.connection.execute(
            '''INSERT INTO trosa.tasks (id, account_id, title, due_at, status, task_type)
               VALUES (trosa.compat_uuid('delete-cascade:amy-task'), ?, 'Amy task',
                       trosa.compat_time('2026-10-02'), 'open', 'follow_up')''',
            (amy_account,),
        )
        self.connection.commit()

        deleted = client.delete(f'/api/customers/{customer_id}/permanent')
        self.assertEqual(deleted.status_code, 200, deleted.get_json())
        self.assertEqual(client.get(f'/api/customers/{customer_id}').status_code, 404)
        # Hamid's account is gone; Amy's same-company account and task survive.
        self.assertEqual(self.connection.execute(
            'SELECT count(*) FROM trosa.accounts WHERE id=?', (hamid_account,)).fetchone()[0], 0)
        db.set_db_user('amy')
        amy_connection = db.get_db()
        try:
            self.assertIsNotNone(trosa_domain.customer_record(amy_connection, amy_customer_id))
            self.assertEqual(amy_connection.execute(
                'SELECT count(*) FROM trosa.tasks WHERE account_id=?', (amy_account,)).fetchone()[0], 1)
        finally:
            amy_connection.close()
            db.set_db_user('hamid')
        # The shared company and its identity rows survive Hamid's delete.
        self.assertEqual(self.connection.execute(
            'SELECT count(*) FROM core.companies WHERE id=?', (company_id,)).fetchone()[0], 1)
        orphans = self.connection.execute(
            '''SELECT count(*) FROM trosa.account_legacy_refs ref
                 LEFT JOIN trosa.accounts account ON account.id=ref.account_id
                WHERE account.id IS NULL''',
        ).fetchone()[0]
        self.assertEqual(orphans, 0)

        # Amy's own permanent delete is exclusive and removes her rows.
        amy_client = module.app.test_client()
        self.assertEqual(amy_client.post('/api/auth/login', json={'user': 'amy'}).status_code, 200)
        self.assertEqual(
            amy_client.delete(f'/api/customers/{amy_customer_id}/permanent').status_code, 200,
        )
        self.assertEqual(
            self.connection.execute(
                'SELECT count(*) FROM trosa.accounts WHERE id=?', (amy_account,),
            ).fetchone()[0],
            0,
        )
        remaining = self.connection.execute(
            '''SELECT (SELECT count(*) FROM trosa.tasks WHERE account_id=?)
                    + (SELECT count(*) FROM trosa.timeline_events WHERE account_id=?)
                    + (SELECT count(*) FROM trosa.outreach_messages WHERE account_id=?)
                    + (SELECT count(*) FROM trosa.inbox_items WHERE account_id=?)''',
            (amy_account, amy_account, amy_account, amy_account),
        ).fetchone()[0]
        self.assertEqual(remaining, 0)
        orphans = self.connection.execute(
            '''SELECT count(*) FROM trosa.account_legacy_refs ref
                 LEFT JOIN trosa.accounts account ON account.id=ref.account_id
                WHERE account.id IS NULL''',
        ).fetchone()[0]
        self.assertEqual(orphans, 0)
        module._INBOX_CACHE.clear()

    def test_permanent_delete_clears_contact_linked_communication_and_receipts(self):
        """Permanent delete must remove every child the contact method/account anchors.

        Regression: the delete removed ``core.contact_methods`` before the
        timeline/outreach/delivery/receipt rows that reference it, and removed
        ``trosa.accounts`` before its integration receipts.  A real customer
        with a contact-linked communication, an outreach message and a Sela
        receipt therefore returned 500 and removed nothing.
        """
        import db
        from tools.postgres_rehearsal import load_fixture

        load_fixture()
        module = self._app_module()
        module._INBOX_CACHE.clear()
        client = module.app.test_client()
        self.assertEqual(client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)

        marker = 'delete-fk-contact'
        created = client.post('/api/customers', json={
            'name': 'Contact Linked Delete Customer', 'company': 'Contact Linked Delete Co',
            'country': 'US',
            'contacts': [{'name': 'Linked Buyer', 'email': 'linked@delete-fk.example'}],
        })
        self.assertEqual(created.status_code, 201, created.get_json())
        customer_id = created.get_json()['id']
        contact_id = client.get(f'/api/customers/{customer_id}').get_json()['contacts'][0]['id']

        account_id = self.connection.execute(
            '''SELECT account_id FROM trosa.account_legacy_refs
                WHERE organization_id=trosa.compat_org_id() AND legacy_user_id='hamid'
                  AND legacy_customer_id=?''', (customer_id,),
        ).fetchone()['account_id']
        contact = self.connection.execute(
            '''SELECT person_id, contact_method_id FROM trosa.contact_legacy_refs
                WHERE organization_id=trosa.compat_org_id() AND legacy_user_id='hamid'
                  AND legacy_contact_id=?''', (contact_id,),
        ).fetchone()
        contact_method_id = contact['contact_method_id']
        person_id = contact['person_id']
        self.assertTrue(contact_method_id)

        def uid(name):
            return self.connection.execute("SELECT trosa.compat_uuid(?)", (name,)).fetchone()[0]

        timeline_id = uid(marker + ':timeline')
        outreach_id = uid(marker + ':outreach')
        receipt_id = uid(marker + ':receipt')
        self.connection.execute(
            '''INSERT INTO trosa.timeline_events
                   (id, account_id, contact_method_id, event_type, direction, content, occurred_at)
               VALUES (?, ?, ?, 'communication', 'outbound', 'linked fact', now())''',
            (timeline_id, account_id, contact_method_id),
        )
        self.connection.execute(
            '''INSERT INTO trosa.outreach_messages
                   (id, account_id, contact_method_id, subject, body, provider_message_id, sent_at)
               VALUES (?, ?, ?, 'Linked quote', 'Linked body', ?, now())''',
            (outreach_id, account_id, contact_method_id, marker + '-msg'),
        )
        self.connection.execute(
            '''INSERT INTO trosa.email_delivery_events
                   (id, organization_id, contact_method_id, outreach_message_id, event_type, occurred_at)
               VALUES (?, trosa.compat_org_id(), ?, ?, 'delivered', now())''',
            (uid(marker + ':delivery'), contact_method_id, outreach_id),
        )
        self.connection.execute(
            '''INSERT INTO trosa.email_message_receipts
                   (id, organization_id, provider_message_id, account_id, contact_method_id, timeline_event_id)
               VALUES (?, trosa.compat_org_id(), ?, ?, ?, ?)''',
            (receipt_id, marker + '-receipt-msg', account_id, contact_method_id, timeline_id),
        )
        self.connection.execute(
            '''INSERT INTO audit.integration_receipts
                   (id, organization_id, integration, idempotency_key, request_sha256,
                    account_id, response_payload)
               VALUES (?, trosa.compat_org_id(), 'sela', ?, 'sha', ?, '{}'::jsonb)''',
            (uid(marker + ':integration'), marker + '-idem', account_id),
        )
        self.connection.commit()

        deleted = client.delete(f'/api/customers/{customer_id}/permanent')
        self.assertEqual(deleted.status_code, 200, deleted.get_json())

        for table in ('trosa.timeline_events', 'trosa.outreach_messages',
                      'trosa.email_message_receipts', 'audit.integration_receipts'):
            self.assertEqual(
                self.connection.execute(
                    f'SELECT count(*) FROM {table} WHERE account_id=?', (account_id,),
                ).fetchone()[0],
                0,
                table,
            )
        self.assertEqual(self.connection.execute(
            'SELECT count(*) FROM trosa.email_delivery_events WHERE contact_method_id=?',
            (contact_method_id,)).fetchone()[0], 0)
        self.assertEqual(self.connection.execute(
            'SELECT count(*) FROM core.contact_methods WHERE id=?', (contact_method_id,)).fetchone()[0], 0)
        if person_id:
            self.assertEqual(self.connection.execute(
                'SELECT count(*) FROM core.people WHERE id=?', (person_id,)).fetchone()[0], 0)
        self.assertEqual(self.connection.execute(
            'SELECT count(*) FROM trosa.accounts WHERE id=?', (account_id,)).fetchone()[0], 0)
        orphans = self.connection.execute(
            '''SELECT count(*) FROM trosa.account_legacy_refs ref
                 LEFT JOIN trosa.accounts account ON account.id=ref.account_id
                WHERE account.id IS NULL''',
        ).fetchone()[0]
        self.assertEqual(orphans, 0)
        module._INBOX_CACHE.clear()

    def test_sela_agent_request_visible_key_recovers_candidate_id(self):
        """A Sela human request keeps its source id through the PG Inbox projection.

        The canonical storage key is ``compat:<user>:<raw>``; if the API exposed
        that instead of the functional raw key, the anchored
        ``sela:agent-request:`` parser returned an empty candidate_id and the
        frontend review regex never matched.
        """
        import db
        from tools.postgres_rehearsal import load_fixture

        load_fixture()
        module = self._app_module()
        module._INBOX_CACHE.clear()
        client = module.app.test_client()
        self.assertEqual(client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)

        source_id = 'pg-sela-visible-key-source'
        created = client.post('/api/integrations/sela/needs', json={'request': {
            'source_id': source_id,
            'need': 'Confirm the visible Inbox key contract.',
            'context': 'The dedupe key must round-trip through the API.',
        }})
        self.assertEqual(created.status_code, 200, created.get_json())
        need_id = created.get_json()['item']['trosa_inbox_id']

        listed = client.get('/api/integrations/sela/needs?status=open')
        self.assertEqual(listed.status_code, 200, listed.get_json())
        view = next(item for item in listed.get_json()['needs'] if item['trosa_inbox_id'] == need_id)
        self.assertEqual(view['candidate_id'], source_id)

        row = next(item for item in client.get('/api/inbox').get_json()['items'] if item['id'] == need_id)
        self.assertTrue(row['dedupe_key'].startswith(f'sela:agent-request:{source_id}:'), row['dedupe_key'])
        self.assertFalse(row['dedupe_key'].startswith('compat:'), row['dedupe_key'])

    def test_customer_profile_update_preserves_external_identity_and_last_contact(self):
        """An ordinary profile save must not blank the Sela link or last contact.

        ``customer_records`` treats a present payload key as authoritative, so a
        partial update that defaulted ``external_source``/``external_id``/
        ``last_contact`` to '' silently erased canonical data.
        """
        import db
        from tools.postgres_rehearsal import load_fixture

        load_fixture()
        module = self._app_module()
        module._INBOX_CACHE.clear()
        client = module.app.test_client()
        self.assertEqual(client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)

        source_id = 'pg-profile-preserve-source'
        created = client.post(
            '/api/integrations/sela/prospects',
            headers={'X-Idempotency-Key': 'pg-profile-preserve-1'},
            json={'prospect': {
                'source_id': source_id, 'company': 'Profile Preserve Co',
                'website': 'https://profile-preserve.example', 'country': 'US',
                'business_type': 'acrylic', 'status': 'qualified',
                'outreach_status': 'SENT', 'sent_at': '2026-09-20T10:00:00+08:00',
                'gmail_message_id': 'pg-profile-preserve-outbound-1',
                'contact': {'email': 'preserve@profile-preserve.example'},
            }},
        )
        self.assertEqual(created.status_code, 200, created.get_json())
        customer_id = int(created.get_json()['trosa_id'])

        self.connection.execute(
            """UPDATE trosa.accounts SET last_contact_at=trosa.compat_time('2026-09-16 09:00:00')
                WHERE id=(SELECT account_id FROM trosa.account_legacy_refs
                           WHERE organization_id=trosa.compat_org_id()
                             AND legacy_user_id='hamid' AND legacy_customer_id=?)""",
            (customer_id,),
        )
        self.connection.commit()

        before = self.connection.execute(
            'SELECT external_source, external_id, last_interaction_on '
            'FROM trosa.customer_records WHERE id=?', (customer_id,),
        ).fetchone()
        self.assertEqual(before['external_source'], 'sela')
        self.assertEqual(before['external_id'], source_id)
        self.assertEqual(before['last_interaction_on'], '2026-09-16')

        updated = client.put(f'/api/customers/{customer_id}', json={'notes': 'profile edit probe'})
        self.assertEqual(updated.status_code, 200, updated.get_json())

        after = self.connection.execute(
            'SELECT external_source, external_id, last_interaction_on '
            'FROM trosa.customer_records WHERE id=?', (customer_id,),
        ).fetchone()
        self.assertEqual(after['external_source'], 'sela')
        self.assertEqual(after['external_id'], source_id)
        self.assertEqual(after['last_interaction_on'], '2026-09-16')

    def test_compat_time_is_business_timezone_independent_of_session(self):
        """Naive compatibility timestamps mean Trosa business time on every server.

        The production PostgreSQL service runs with a UTC session while the
        rehearsal inherits Asia/Shanghai; ``compat_local_date`` is hardcoded to
        Asia/Shanghai.  A session-dependent ``compat_time`` shifted writes by
        eight hours and rolled the calendar day for late local times.
        """
        import db
        from tools.postgres_rehearsal import load_fixture

        load_fixture()
        db.set_db_user('hamid')
        connection = db.get_db()
        try:
            self.assertEqual(connection.execute('SHOW TimeZone').fetchone()[0], 'Asia/Shanghai')
            connection.raw.execute("SET TIME ZONE 'UTC'")
            connection.raw.commit()
            naive_date = connection.execute(
                "SELECT trosa.compat_local_date(trosa.compat_time('2026-09-17 23:30:00')) AS d"
            ).fetchone()['d']
            self.assertEqual(naive_date, '2026-09-17')
            offset_date = connection.execute(
                "SELECT trosa.compat_local_date(trosa.compat_time('2026-09-17T23:30:00+03:00')) AS d"
            ).fetchone()['d']
            self.assertEqual(offset_date, '2026-09-18')
        finally:
            connection.close()
            db.set_db_user(None)

    def test_outreach_modern_read_matches_compat_view_date_shape(self):
        """The modern Outreach read must return the same date-shaped value as the view."""
        import db
        from tools.postgres_rehearsal import load_fixture

        load_fixture()
        module = self._app_module()
        module._INBOX_CACHE.clear()
        client = module.app.test_client()
        self.assertEqual(client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)

        source_id = 'pg-outreach-date-shape'
        created = client.post(
            '/api/integrations/sela/prospects',
            headers={'X-Idempotency-Key': 'pg-outreach-date-shape-1'},
            json={'prospect': {
                'source_id': source_id, 'company': 'Outreach Date Shape Co',
                'website': 'https://outreach-date-shape.example', 'country': 'US',
                'business_type': 'acrylic', 'status': 'qualified',
                'outreach_status': 'SENT', 'sent_at': '2026-09-20T19:30:00+08:00',
                'gmail_message_id': 'pg-outreach-date-shape-outbound-1',
                'contact': {'email': 'date-shape@outreach-date-shape.example'},
            }},
        )
        self.assertEqual(created.status_code, 200, created.get_json())
        customer_id = int(created.get_json()['trosa_id'])

        db.set_db_user('hamid')
        connection = db.get_db()
        try:
            modern = next(row for row in module._modern_outreach_rows(connection, customer_id=customer_id)
                          if row['external_id'] == source_id or row['message_id'] == source_id)
            view = connection.execute(
                'SELECT sent_date FROM outreach_emails WHERE customer_id=? AND subject=?',
                (customer_id, modern['subject']),
            ).fetchone()
        finally:
            connection.close()
            db.set_db_user(None)
        self.assertEqual(modern['sent_date'], '2026-09-20')
        self.assertEqual(view['sent_date'], '2026-09-20')

    def test_concurrent_team_invitation_accept_has_single_winner(self):
        """The guarded canonical UPDATE admits exactly one concurrent winner.

        The compatibility view's INSTEAD OF trigger always upserts and
        PostgreSQL reports the number of matched view rows, so a rowcount gate
        on the view let two racing accepts both succeed.
        """
        import threading

        import db
        from tools.postgres_rehearsal import load_fixture

        load_fixture()
        invitation_id = 'pg-invite-race-1'
        token_hash = 'pg-invite-race-hash-1'
        self.connection.execute(
            """INSERT INTO identity.team_invitations
                   (id, organization_id, token_hash, created_by, created_at, expires_at,
                    accepted_at, accepted_user_id, revoked_at)
               VALUES (?, trosa.compat_org_id(), ?, 'hamid', '2099-01-01T00:00:00+00:00',
                       '2099-01-02T00:00:00+00:00', '', '', '')
               ON CONFLICT (id) DO UPDATE SET accepted_at='', accepted_user_id='', revoked_at=''""",
            (invitation_id, token_hash),
        )
        self.connection.commit()

        barrier = threading.Barrier(2)
        rowcounts, errors = [], []

        def attempt(index):
            try:
                connection = db.get_system_db()
                try:
                    connection.execute('BEGIN')
                    row = connection.execute(
                        """SELECT id FROM trade_os_compat.team_invitations
                            WHERE token_hash=? AND COALESCE(accepted_at,'')=''
                              AND COALESCE(revoked_at,'')=''""",
                        (token_hash,),
                    ).fetchone()
                    if row is None:
                        connection.rollback()
                        rowcounts.append(0)
                        return
                    barrier.wait(timeout=10)
                    updated = connection.execute(
                        """UPDATE identity.team_invitations SET accepted_at=?, accepted_user_id=?
                            WHERE organization_id=trosa.compat_org_id()
                              AND id=? AND COALESCE(accepted_at,'')=''""",
                        (f'2099-01-01T00:00:0{index}+00:00', f'user{index}', invitation_id),
                    )
                    rowcounts.append(updated.rowcount)
                    connection.commit()
                finally:
                    connection.close()
            except Exception as exc:  # pragma: no cover - concurrency must hold
                errors.append(repr(exc))

        threads = [threading.Thread(target=attempt, args=(index,)) for index in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(rowcounts), [0, 1])
        accepted = self.connection.execute(
            "SELECT count(*) FROM identity.team_invitations WHERE id=? AND COALESCE(accepted_at,'')<>''",
            (invitation_id,),
        ).fetchone()[0]
        self.assertEqual(accepted, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
