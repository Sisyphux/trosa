"""Regression coverage for the one-open-follow-up-per-customer-day rule."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db
import trosa_domain


class TodayDuplicateRegressionTest(unittest.TestCase):
    """A customer must not occupy Today twice for the same due date."""

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
            "crm_app_today_dedupe_regression", ROOT / "app.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def _client_with_customer(self):
        client = self._app().app.test_client()
        self.assertEqual(client.post("/api/auth/login", json={"user": "hamid"}).status_code, 200)
        created = client.post(
            "/api/customers", json={"name": "Kaze", "company": "Kaze Co."}
        )
        self.assertEqual(created.status_code, 201, created.get_json())
        return client, created.get_json()["id"]

    def _open_count(self, customer_id, due_date):
        connection = sqlite3.connect(db.get_user_db_path("hamid"))
        try:
            return connection.execute(
                """SELECT COUNT(*) FROM reminders
                   WHERE customer_id=? AND is_done=0 AND remind_date=?""",
                (customer_id, due_date),
            ).fetchone()[0]
        finally:
            connection.close()

    def test_create_same_day_merges_into_existing_task(self):
        client, customer_id = self._client_with_customer()
        first = client.post(
            f"/api/customers/{customer_id}/tasks",
            json={"title": "跟进物流", "due_date": "2026-09-14"},
        )
        second = client.post(
            f"/api/customers/{customer_id}/tasks",
            json={"title": "跟进物流", "due_date": "2026-09-14", "reason": "补充联系人"},
        )
        self.assertEqual(first.status_code, 201, first.get_json())
        self.assertEqual(second.status_code, 201, second.get_json())
        self.assertEqual(second.get_json()["id"], first.get_json()["id"])
        self.assertEqual(self._open_count(customer_id, "2026-09-14"), 1)

    def test_reschedule_and_edit_onto_occupied_date_merge(self):
        client, customer_id = self._client_with_customer()
        first = client.post(
            f"/api/customers/{customer_id}/tasks",
            json={"title": "跟进 A", "due_date": "2026-09-14"},
        ).get_json()
        second = client.post(
            f"/api/customers/{customer_id}/tasks",
            json={"title": "跟进 B", "due_date": "2026-09-15"},
        ).get_json()

        moved = client.post(
            f"/api/reminders/{second['id']}/reschedule",
            json={"remind_date": "2026-09-14"},
        )
        self.assertEqual(moved.status_code, 200, moved.get_json())
        self.assertEqual(moved.get_json()["reminder"]["id"], first["id"])
        self.assertEqual(self._open_count(customer_id, "2026-09-14"), 1)

        third = client.post(
            f"/api/customers/{customer_id}/tasks",
            json={"title": "跟进 C", "due_date": "2026-09-16"},
        ).get_json()
        edited = client.patch(
            f"/api/reminders/{third['id']}", json={"remind_date": "2026-09-14"}
        )
        self.assertEqual(edited.status_code, 200, edited.get_json())
        self.assertEqual(edited.get_json()["reminder"]["id"], first["id"])
        self.assertEqual(self._open_count(customer_id, "2026-09-14"), 1)

    def test_legacy_duplicates_are_hidden_and_healed(self):
        client, customer_id = self._client_with_customer()
        connection = sqlite3.connect(db.get_user_db_path("hamid"))
        try:
            connection.execute("DROP INDEX IF EXISTS idx_reminders_one_open_follow_up_per_day")
            for index in range(3):
                connection.execute(
                    """INSERT INTO reminders
                       (customer_id, title, content, reason, remind_date, is_done,
                        reminder_type, created_at)
                       VALUES (?, '重复跟进', '重复跟进', ?, '2026-09-14', 0,
                               'follow_up', ?)""",
                    (customer_id, f"原因 {index}", f"2026-09-01 0{index}:00:00"),
                )
            connection.commit()
        finally:
            connection.close()

        today = client.get("/api/reminders/today")
        self.assertEqual(today.status_code, 200, today.get_json())
        mine = [item for item in today.get_json() if item["customer_id"] == customer_id]
        self.assertEqual(len(mine), 1)

        db.init_user_tables("hamid")
        self.assertEqual(self._open_count(customer_id, "2026-09-14"), 1)
        connection = sqlite3.connect(db.get_user_db_path("hamid"))
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO reminders
                       (customer_id, title, remind_date, is_done, reminder_type)
                       VALUES (?, '第四条', '2026-09-14', 0, 'follow_up')""",
                    (customer_id,),
                )
        finally:
            connection.close()

    def test_canonical_task_id_collapses_legacy_alias_fanout(self):
        class FakeResult:
            def fetchall(self):
                return [
                    {
                        "id": 2121, "customer_id": 18,
                        "remind_date": "2026-09-14", "title": "物流价格降低稳定时跟进",
                    },
                    {
                        "id": 2121, "customer_id": 87,
                        "remind_date": "2026-09-14", "title": "物流价格降低稳定时跟进",
                    },
                ]

        class FakeConnection:
            def execute(self, query, params):
                return FakeResult()

        with mock.patch.object(trosa_domain, "postgres_mode", return_value=True):
            rows = trosa_domain.today_tasks(
                FakeConnection(), due_on_or_before="2026-09-16"
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 2121)


if __name__ == "__main__":
    unittest.main()
