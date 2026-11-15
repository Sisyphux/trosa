"""Organization-wide ownership projection tests without any database connection."""

import json
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("trosa_company_ownership_unit_app", ROOT / "app.py")
app = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(app)


class Result:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        statement = " ".join(str(sql).split())
        self.statements.append((statement, tuple(params or ())))
        if "SELECT trosa.compat_org_id() AS organization_id" in statement:
            return Result([{"organization_id": "org-test"}])
        if "AS unresolved_accounts" in statement:
            return Result([{"total_owners": 2, "unresolved_accounts": 0}])
        if "FROM identity.users" in statement:
            return Result([
                {"legacy_user_id": "amy", "display_name": "Amy"},
                {"legacy_user_id": "hamid", "display_name": "Hamid"},
            ])
        if "SELECT count(*) AS count FROM core.companies" in statement:
            return Result([{"count": 11}])
        if "FROM core.companies company" in statement:
            return Result([{
                "company_id": "company-amy", "canonical_name": "CRISTACRYL SAS",
                "normalized_name": "cristacryl sas", "website": "https://cristacryl.com",
                "identity_status": "confirmed", "normalized_domain": "cristacryl.com",
                "normalized_alias": "cristacryl", "account_id": "account-amy",
                "owner_username": "amy", "owner_display_name": "Amy",
                "account_status": "未建联", "last_contact_on": "2026-10-01",
            }])
        raise AssertionError(f"unexpected SQL in fake connection: {statement}")


class SelaCompanyOwnershipSnapshotTests(unittest.TestCase):
    def test_all_organization_identities_include_inactive_retained_owner(self):
        connection = FakeConnection()
        original = connection.execute

        def execute(sql, params=None):
            statement = " ".join(str(sql).split())
            if "AS unresolved_accounts" in statement:
                return Result([{"total_owners": 2, "unresolved_accounts": 0}])
            if "FROM identity.users" in statement and "status='active'" in statement:
                return Result([{"legacy_user_id": "hamid", "display_name": "Hamid"}])
            if "FROM identity.users" in statement:
                return Result([{"legacy_user_id": "amy", "display_name": "Amy"},
                               {"legacy_user_id": "hamid", "display_name": "Hamid"}])
            return original(sql, params)

        connection.execute = execute
        with mock.patch.object(app, "postgres_mode", return_value=True):
            body = app._sela_company_ownership_snapshot(connection, domain="cristacryl.com")
        self.assertEqual(body["checked_owners"], ["amy", "hamid"])
        self.assertEqual(body["total_owners"], 2)
        self.assertEqual(body["coverage"], {
            "basis": "all_org_identities_and_accounts_v1",
            "organization_identity_count": 2,
            "unresolved_account_count": 0,
        })

    def test_unresolvable_account_anywhere_in_organization_fails_closed(self):
        class Unresolvable(FakeConnection):
            def execute(self, sql, params=None):
                if "AS unresolved_accounts" in " ".join(str(sql).split()):
                    return Result([{"total_owners": 2, "unresolved_accounts": 1}])
                return super().execute(sql, params)

        with mock.patch.object(app, "postgres_mode", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "负责人"):
                app._sela_company_ownership_snapshot(Unresolvable(), domain="unrelated.test")

    def test_independent_owner_count_mismatch_fails_closed(self):
        class Mismatch(FakeConnection):
            def execute(self, sql, params=None):
                if "AS unresolved_accounts" in " ".join(str(sql).split()):
                    return Result([{"total_owners": 3, "unresolved_accounts": 0}])
                return super().execute(sql, params)

        with mock.patch.object(app, "postgres_mode", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "负责人"):
                app._sela_company_ownership_snapshot(Mismatch(), domain="cristacryl.com")

    def test_projection_checks_every_owner_and_returns_only_identity_facts(self):
        connection = FakeConnection()
        with mock.patch.object(app, "postgres_mode", return_value=True):
            body = app._sela_company_ownership_snapshot(
                connection, domain="https://www.cristacryl.com", name="Cristacryl")

        self.assertEqual(body["scope"], "org")
        self.assertEqual(body["organization_id"], "org-test")
        self.assertEqual(body["checked_owners"], ["amy", "hamid"])
        self.assertEqual(body["total_owners"], 2)
        self.assertEqual(body["total_companies"], 11)
        self.assertTrue(body["matched"])
        match = body["matches"][0]
        self.assertEqual(match["matched_by"], ["domain", "name"])
        self.assertEqual(match["accounts"][0]["owner_username"], "amy")
        rendered = json.dumps(body, ensure_ascii=False).lower()
        for forbidden in ("email", "notes", "amount", "activities", "interactions"):
            self.assertNotIn(forbidden, rendered)

        sql = "\n".join(statement for statement, _params in connection.statements).lower()
        self.assertIn("identity.users", sql)
        self.assertIn("core.companies", sql)
        self.assertIn("core.company_domains", sql)
        self.assertIn("account.organization_id=company.organization_id", sql)
        self.assertNotIn("customer_contacts", sql)
        self.assertNotIn("customer_interactions", sql)
        self.assertNotIn("email", sql)

    def test_snapshot_reads_multiple_recipient_domains_in_one_complete_projection(self):
        connection = FakeConnection()
        with mock.patch.object(app, "postgres_mode", return_value=True):
            body = app._sela_company_ownership_snapshot(
                connection, domain="cristacryl.com", domains=["secondary.cristacryl.test"],
                name="Cristacryl")

        self.assertEqual(body["scope"], "org")
        lookup = next((params for statement, params in connection.statements
                       if "FROM core.companies company" in statement), None)
        self.assertIsNotNone(lookup)
        self.assertIn("cristacryl.com", lookup)
        self.assertIn("secondary.cristacryl.test", lookup)

    def test_website_only_match_checks_every_domain_and_reports_domain_match(self):
        class WebsiteOnly(FakeConnection):
            def execute(self, sql, params=None):
                statement = " ".join(str(sql).split())
                if "FROM core.companies company" in statement:
                    self.statements.append((statement, tuple(params or ())))
                    return Result([{
                        "company_id": "website-only", "canonical_name": "Other Name",
                        "normalized_name": "other name", "website": "https://www.foreign.test/path",
                        "identity_status": "confirmed", "normalized_domain": None,
                        "normalized_alias": None, "account_id": "amy-account",
                        "owner_username": "amy", "owner_display_name": "Amy",
                        "account_status": "active", "last_contact_on": "",
                    }])
                return super().execute(sql, params)

        connection = WebsiteOnly()
        with mock.patch.object(app, "postgres_mode", return_value=True):
            body = app._sela_company_ownership_snapshot(
                connection, domain="primary.test", domains=["foreign.test"])
        sql, params = next((sql, params) for sql, params in connection.statements
                           if "FROM core.companies company" in sql)
        self.assertIn("trosa.sela_company_domain_key", sql)
        self.assertEqual(params.count("primary.test"), 2)
        self.assertEqual(params.count("foreign.test"), 2)
        self.assertEqual(body["matches"][0]["matched_by"], ["domain"])
        self.assertEqual(body["matches"][0]["accounts"][0]["owner_username"], "amy")

    def test_alias_name_still_matches(self):
        connection = FakeConnection()
        with mock.patch.object(app, "postgres_mode", return_value=True):
            body = app._sela_company_ownership_snapshot(connection, name="Cristacryl")
        sql = next(sql for sql, _ in connection.statements if "FROM core.companies company" in sql)
        self.assertIn("company_alias.normalized_alias IN", sql)
        self.assertIn("name", body["matches"][0]["matched_by"])

    def test_guard_name_key_finds_foreign_owner_without_domain(self):
        class GuardMatch(FakeConnection):
            def execute(self, sql, params=None):
                statement = " ".join(str(sql).split())
                if "FROM core.companies company" in statement:
                    self.statements.append((statement, tuple(params or ())))
                    # The database guard reduces both names to "acme"; Python
                    # instead produces "acme and" for the requested name.
                    required = ("nullif(trosa.sela_company_name_key(?), '')",
                                "input_name.name_key IS NOT NULL", "AS guard_name_match") + tuple(
                        "input_name.name_key=trosa.sela_company_name_key(" + field + ")"
                        for field in ("company.canonical_name", "company.normalized_name",
                                      "company_alias.alias", "company_alias.normalized_alias"))
                    if not all(part in statement for part in required) or "Acme & Co" not in params:
                        return Result([])
                    return Result([{
                        "company_id": "foreign", "canonical_name": "Acme Co",
                        "normalized_name": "acme co", "website": "",
                        "identity_status": "confirmed", "normalized_domain": None,
                        "normalized_alias": None, "guard_name_match": True,
                        "account_id": "account-amy", "owner_username": "amy",
                        "owner_display_name": "Amy", "account_status": "active",
                        "last_contact_on": "",
                    }])
                return super().execute(sql, params)

        with mock.patch.object(app, "postgres_mode", return_value=True):
            body = app._sela_company_ownership_snapshot(GuardMatch(), name="Acme & Co")
        self.assertTrue(body["matched"])
        self.assertEqual(body["matches"][0]["matched_by"], ["name"])
        self.assertEqual(body["matches"][0]["accounts"][0]["owner_username"], "amy")

    def test_guard_name_check_is_not_skipped_when_python_key_is_empty(self):
        connection = FakeConnection()
        with mock.patch.object(app, "postgres_mode", return_value=True):
            app._sela_company_ownership_snapshot(connection, name="Company", domain="unrelated.test")
        sql = next(statement for statement, _ in connection.statements
                   if "FROM core.companies company" in statement)
        where = sql.split("WHERE company.organization_id", 1)[1]
        self.assertIn("input_name.name_key=trosa.sela_company_name_key(company.canonical_name)", where)

    def test_duplicate_domains_and_shared_company_accounts_are_all_returned(self):
        class DuplicateDomains(FakeConnection):
            def execute(self, sql, params=None):
                statement = " ".join(str(sql).split())
                if "SELECT count(*) AS count FROM core.companies" in statement:
                    return Result([{"count": 2}])
                if "FROM core.companies company" in statement:
                    shared = {
                        "company_id": "company-shared", "canonical_name": "CRISTACRYL SAS",
                        "normalized_name": "cristacryl sas", "website": "https://cristacryl.com",
                        "identity_status": "confirmed", "normalized_domain": "cristacryl.com",
                        "normalized_alias": "cristacryl", "account_status": "未建联",
                        "last_contact_on": "2026-10-01",
                    }
                    duplicate = {
                        "company_id": "company-duplicate", "canonical_name": "Cristacryl Group",
                        "normalized_name": "cristacryl group", "website": "https://cristacryl.com",
                        "identity_status": "confirmed", "normalized_domain": "cristacryl.com",
                        "normalized_alias": "cristacryl group", "account_status": "未建联",
                        "last_contact_on": "",
                    }
                    return Result([
                        {**shared, "account_id": "account-amy", "owner_username": "amy",
                         "owner_display_name": "Amy"},
                        {**shared, "account_id": "account-hamid", "owner_username": "hamid",
                         "owner_display_name": "Hamid"},
                        {**duplicate, "account_id": "account-duplicate", "owner_username": "amy",
                         "owner_display_name": "Amy"},
                    ])
                return super().execute(sql, params)

        with mock.patch.object(app, "postgres_mode", return_value=True):
            body = app._sela_company_ownership_snapshot(
                DuplicateDomains(), domain="cristacryl.com")

        self.assertEqual(body["total_companies"], 2)
        self.assertEqual({row["company_id"] for row in body["matches"]},
                         {"company-shared", "company-duplicate"})
        shared = next(row for row in body["matches"] if row["company_id"] == "company-shared")
        self.assertEqual({account["owner_username"] for account in shared["accounts"]}, {"amy", "hamid"})
        self.assertTrue(all("legacy_customer_id" not in row for row in body["matches"]))

    def test_same_legacy_customer_number_is_scoped_by_organization_and_owner(self):
        class OwnerScopedProfiles:
            profiles = {
                "amy": {"customer_id": 157, "company": "CRISTACRYL SAS"},
                "hamid": {"customer_id": 157, "company": "Granada Plastics"},
            }

            def __init__(self):
                self.calls = []

            def execute(self, sql, params=None):
                statement = " ".join(str(sql).split())
                self.calls.append((statement, tuple(params or ())))
                if "FROM trosa.agent_prospect_profiles" not in statement:
                    raise AssertionError(f"unexpected SQL: {statement}")
                if "organization_id=trosa.compat_org_id()" not in statement:
                    raise AssertionError("profile lookup must stay inside the authenticated organization")
                if "legacy_user_id=? AND source=? AND source_id=?" not in statement:
                    raise AssertionError("profile lookup must include its owner and stable source id")
                owner, source, source_id = params
                return Result([self.profiles[owner]])

        connection = OwnerScopedProfiles()
        results = {}
        for owner in ("amy", "hamid"):
            with mock.patch.object(app, "postgres_mode", return_value=True), \
                 mock.patch.object(app, "_sela_prospect_user", return_value=owner):
                results[owner] = app._sela_profile_by_source(connection, "trosa-customer-157")

        self.assertEqual(results["amy"]["company"], "CRISTACRYL SAS")
        self.assertEqual(results["hamid"]["company"], "Granada Plastics")
        self.assertEqual([call[1] for call in connection.calls], [
            ("amy", "sela", "trosa-customer-157"),
            ("hamid", "sela", "trosa-customer-157"),
        ])

    def test_incomplete_owner_roster_fails_closed(self):
        class Incomplete(FakeConnection):
            def execute(self, sql, params=None):
                if "FROM identity.users" in " ".join(str(sql).split()):
                    return Result([])
                return super().execute(sql, params)

        with mock.patch.object(app, "postgres_mode", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "负责人清单不完整"):
                app._sela_company_ownership_snapshot(Incomplete(), domain="cristacryl.com")

    def test_existing_sela_prospect_update_is_blocked_if_ownership_moved_to_another_owner(self):
        class Connection:
            def __init__(self):
                self.audit_writes = []

            def execute(self, sql, params=None):
                if "INSERT INTO audit.sela_company_ownership_lookups" in str(sql):
                    self.audit_writes.append((str(sql), tuple(params or ())))
                return Result([])

        connection = Connection()
        ownership = {
            "matches": [{
                "company_id": "company-amy", "canonical_name": "CRISTACRYL SAS",
                "identity_status": "confirmed", "matched_by": ["domain"],
                "accounts": [{"account_id": "account-amy", "owner_username": "amy",
                              "owner_display_name": "Amy", "account_status": "未建联"}],
            }],
            "total_owners": 2,
        }
        with mock.patch.object(app, "postgres_mode", return_value=True), \
             mock.patch.object(app, "_sela_profile_by_source", return_value={"customer_id": 7}), \
             mock.patch.object(app, "_sela_profile_customer", return_value=None), \
             mock.patch.object(app, "_sela_company_ownership_snapshot", return_value=ownership), \
             mock.patch.object(app, "_sela_prospect_user", return_value="hamid"):
            result = app._sela_upsert_prospect(connection, {
                "source_id": "sela-existing-cross-owner", "company": "Cristacryl",
                "website": "https://cristacryl.com",
            })

        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "CROSS_OWNER_COMPANY")
        self.assertEqual(len(connection.audit_writes), 1)

    def test_service_route_uses_one_snapshot_and_writes_only_redacted_lookup_audit(self):
        class EndpointConnection(FakeConnection):
            def __init__(self):
                super().__init__()
                self.audit_writes = []
                self.committed = False
                self.closed = False

            def execute(self, sql, params=None):
                statement = " ".join(str(sql).split())
                if statement.startswith("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"):
                    self.statements.append((statement, tuple(params or ())))
                    return Result([])
                if "FROM audit.sela_company_ownership_lookups" in statement:
                    self.statements.append((statement, tuple(params or ())))
                    return Result([{"count": 0}])
                if "INSERT INTO audit.sela_company_ownership_lookups" in statement:
                    self.statements.append((statement, tuple(params or ())))
                    self.audit_writes.append(tuple(params or ()))
                    return Result([])
                return super().execute(sql, params)

            def commit(self):
                self.committed = True

            def rollback(self):
                raise AssertionError("successful lookup should commit only its audit record")

            def close(self):
                self.closed = True

        connection = EndpointConnection()
        app.app.secret_key = "test-only"
        client = app.app.test_client()
        with mock.patch.object(app, "_sela_service_integration_record", return_value={}), \
             mock.patch.object(app, "_integration_token_matches", return_value=True), \
             mock.patch.object(app, "_production_mode", False), \
             mock.patch.object(app, "postgres_mode", return_value=True), \
             mock.patch.object(app, "get_db", return_value=connection):
            response = client.get("/api/integrations/sela/company-ownership?domain=cristacryl.com&name=Cristacryl",
                                  headers={"Authorization": "Bearer rehearsal-only"})
            malformed = client.get("/api/integrations/sela/company-ownership?domain=cristacryl.com&email=private@test",
                                   headers={"Authorization": "Bearer rehearsal-only"})

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.get_json()["scope"], "org")
        self.assertEqual(len(connection.audit_writes), 1)
        audit = connection.audit_writes[0]
        self.assertEqual((audit[1], audit[2]), ("org-test", "hamid"))
        self.assertNotIn("cristacryl.com", audit[3])
        self.assertTrue(connection.committed)
        self.assertTrue(connection.closed)
        self.assertEqual(malformed.status_code, 400)


if __name__ == "__main__":
    unittest.main()
