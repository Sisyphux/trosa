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
            '''SELECT a.display_name, c.canonical_name, d.notes, a.priority_level,
                      count(DISTINCT event.id) AS interactions,
                      count(DISTINCT task.id) FILTER (WHERE task.status='open') AS open_tasks,
                      count(DISTINCT file_object.id) AS files
                 FROM trosa.account_legacy_refs ref
                 JOIN trosa.accounts a ON a.id=ref.account_id
                 JOIN core.companies c ON c.id=a.company_id
                 JOIN trosa.customer_details d ON d.account_id=a.id
                 LEFT JOIN trosa.timeline_events event ON event.account_id=a.id
                 LEFT JOIN trosa.tasks task ON task.account_id=a.id
                 LEFT JOIN core.entity_files entity_file ON entity_file.account_id=a.id
                 LEFT JOIN core.file_objects file_object ON file_object.id=entity_file.file_object_id
                WHERE ref.organization_id=trosa.compat_org_id()
                  AND ref.legacy_user_id=? AND ref.legacy_customer_id=?
                GROUP BY a.display_name, c.canonical_name, d.notes, a.priority_level''',
            ('hamid', customer_id),
        ).fetchone()
        self.assertEqual(row['display_name'], 'API Acceptance Customer Updated')
        self.assertEqual(row['canonical_name'], 'API Acceptance Co')
        self.assertEqual(row['priority_level'], 'B+')
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
            'action': 'record_communication', 'customer_id': 1,
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
