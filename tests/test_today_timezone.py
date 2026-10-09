"""Today tide read APIs expose customer timezone metadata without guessing.

The customer profile is the only place that decides a timezone.  These tests
pin the read contract: ``/api/reminders/today`` and ``/api/reminders/upcoming``
must return ``timezone`` / ``timezone_source`` for each row, keep the fields
empty when the profile has no timezone, flag an inference as ``inferred``, and
keep cross-user isolation intact.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db


class TodayTimezoneApiTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        self.original_demo = os.environ.get("CRM_SEED_DEMO_DATA")
        db.DB_DIR = self.tempdir.name
        os.environ.pop("CRM_SEED_DEMO_DATA", None)
        db.init_all_dbs()

    def tearDown(self):
        db.cancel_safety_backup()
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop("CRM_SEED_DEMO_DATA", None)
        else:
            os.environ["CRM_SEED_DEMO_DATA"] = self.original_demo
        self.tempdir.cleanup()

    def _app(self):
        spec = importlib.util.spec_from_file_location(
            "crm_app_today_timezone", ROOT / "app.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def _client(self, user="hamid"):
        client = self._app().app.test_client()
        self.assertEqual(
            client.post("/api/auth/login", json={"user": user}).status_code, 200
        )
        return client

    def _engaged_customer(self, client, **payload):
        """Create a customer with a real inbound interaction so it enters Today."""
        created = client.post("/api/customers", json=payload)
        self.assertEqual(created.status_code, 201, created.get_json())
        customer_id = created.get_json()["id"]
        recorded = client.post(
            f"/api/customers/{customer_id}/follow_history",
            json={
                "activity_content": "客户回复询价",
                "direction": "inbound",
                "follow_date": date.today().isoformat(),
            },
        )
        self.assertEqual(recorded.status_code, 200, recorded.get_json())
        return customer_id

    def _due_today(self, client, customer_id, title="跟进物流"):
        created = client.post(
            f"/api/customers/{customer_id}/tasks",
            json={"title": title, "due_date": date.today().isoformat()},
        )
        self.assertEqual(created.status_code, 201, created.get_json())

    def _upcoming(self, client, customer_id, title="稍后跟进"):
        created = client.post(
            f"/api/customers/{customer_id}/tasks",
            json={
                "title": title,
                "due_date": (date.today() + timedelta(days=3)).isoformat(),
            },
        )
        self.assertEqual(created.status_code, 201, created.get_json())

    def test_today_and_upcoming_expose_manual_timezone(self):
        client = self._client()
        customer_id = self._engaged_customer(
            client, name="Zed", company="Zed Co.", country="美国"
        )
        updated = client.put(
            f"/api/customers/{customer_id}", json={"timezone": "America/Los_Angeles"}
        )
        self.assertEqual(updated.status_code, 200, updated.get_json())
        self._due_today(client, customer_id)
        self._upcoming(client, customer_id)

        today = client.get("/api/reminders/today").get_json()
        mine = [row for row in today if row["customer_id"] == customer_id]
        self.assertEqual(len(mine), 1, mine)
        self.assertEqual(mine[0]["timezone"], "America/Los_Angeles")
        self.assertEqual(mine[0]["timezone_source"], "manual")

        upcoming = client.get("/api/reminders/upcoming").get_json()
        mine_upcoming = [row for row in upcoming if row["customer_id"] == customer_id]
        self.assertEqual(len(mine_upcoming), 1, mine_upcoming)
        self.assertEqual(mine_upcoming[0]["timezone"], "America/Los_Angeles")
        self.assertEqual(mine_upcoming[0]["timezone_source"], "manual")

    def test_country_inference_is_flagged_not_hidden(self):
        client = self._client()
        # 美国 maps to a default IANA zone; the row must say it is inferred.
        customer_id = self._engaged_customer(
            client, name="Multi", company="Multi Co.", country="美国"
        )
        self._due_today(client, customer_id)

        mine = [
            row
            for row in client.get("/api/reminders/today").get_json()
            if row["customer_id"] == customer_id
        ]
        self.assertEqual(len(mine), 1, mine)
        self.assertTrue(mine[0]["timezone"], mine[0])
        self.assertEqual(mine[0]["timezone_source"], "inferred")

    def test_ambiguous_country_stays_unknown(self):
        client = self._client()
        # A worldwide marker has no single place; the row stays empty.
        customer_id = self._engaged_customer(
            client, name="NoZone", company="NoZone Co.", country="全球"
        )
        self._due_today(client, customer_id)

        mine = [
            row
            for row in client.get("/api/reminders/today").get_json()
            if row["customer_id"] == customer_id
        ]
        self.assertEqual(len(mine), 1, mine)
        self.assertEqual(mine[0]["timezone"], "")
        self.assertEqual(mine[0]["timezone_source"], "")

    def test_cross_user_isolation_is_preserved(self):
        hamid = self._client("hamid")
        hamid_customer = self._engaged_customer(
            hamid, name="HamidClient", company="Hamid Co.", country="美国"
        )
        self._due_today(hamid, hamid_customer)

        amy = self._client("amy")
        amy_customer = self._engaged_customer(
            amy, name="AmyClient", company="Amy Co.", country="日本"
        )
        self._due_today(amy, amy_customer)

        amy_rows = amy.get("/api/reminders/today").get_json()
        companies = {row.get("customer_company") or row.get("customer_name") for row in amy_rows}
        self.assertIn("Amy Co.", companies)
        self.assertNotIn("Hamid Co.", companies)
        for row in amy_rows:
            self.assertIn("timezone", row)
            self.assertIn("timezone_source", row)

    def test_manual_order_endpoint_is_retained(self):
        """The retired reorder UI must not remove the backend endpoint."""
        client = self._client()
        response = client.post("/api/reminders/today/order", json={"order": []})
        self.assertNotEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
