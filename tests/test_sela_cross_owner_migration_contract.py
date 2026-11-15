"""Static contracts for the external PostgreSQL cross-owner rehearsal."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SelaCrossOwnerMigrationContractTests(unittest.TestCase):
    def test_guard_matches_and_serializes_every_company_name_alias_and_domain(self):
        sql = (ROOT / "migrations" / "0136_sela_cross_owner_guard.sql").read_text(encoding="utf-8").lower()

        # Account creation and identity edits must share the same ordered lock
        # set, including identities that are not in the primary website/name.
        self.assertIn("sela_lock_identity_keys", sql)
        self.assertIn("create trigger", sql)
        self.assertIn("on core.company_aliases", sql)
        self.assertIn("on core.company_domains", sql)
        self.assertIn("on core.companies", sql)
        self.assertIn("normalized_alias", sql)
        self.assertIn("normalized_domain", sql)
        self.assertIn("sela_company_website_domain_keys(company.website)", sql)
        self.assertIn("sela_company_website_domain_keys(other_company.website)", sql)

        # Candidate aliases must be compared in both directions: an incoming
        # alias can equal an existing canonical name, or another alias.
        self.assertIn("candidate_name_keys", sql)
        self.assertIn("candidate_key.name_key = trosa.sela_company_name_key(other_company.canonical_name)", sql)
        self.assertIn("trosa.sela_company_name_key(other_alias.normalized_alias)", sql)
        self.assertIn("is distinct from new.owner_user_id", sql)

    def test_website_hosts_share_api_delimiters_and_lock_keys(self):
        sql = (ROOT / "migrations" / "0136_sela_cross_owner_guard.sql").read_text(encoding="utf-8").lower()
        helper = sql.split("create or replace function trosa.sela_company_website_domain_keys", 1)[1].split("$$;", 1)[0]
        self.assertIn("immutable", helper)
        self.assertIn("regexp_split_to_table", helper)
        self.assertIn("'[[:space:],;，；]+'", helper)
        self.assertIn("distinct", helper)
        self.assertIn("sela_company_domain_key", helper)
        self.assertIn("sela_company_website_domain_keys(old.website)", sql)
        self.assertIn("sela_company_website_domain_keys(new.website)", sql)

    def test_identity_locks_have_deterministic_order(self):
        sql = (ROOT / "migrations" / "0136_sela_cross_owner_guard.sql").read_text(encoding="utf-8").lower()
        self.assertLess(sql.index("select distinct lock_key"), sql.index("order by lock_key"))
        self.assertIn("pg_advisory_xact_lock", sql)


if __name__ == "__main__":
    unittest.main()
