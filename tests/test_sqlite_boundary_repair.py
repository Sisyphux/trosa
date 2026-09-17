"""Unit tests for the SQLite-derived deterministic repair planner."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.sqlite_boundary_repair import plan_event_followups


class PlanEventFollowupsTest(unittest.TestCase):
    def test_event_follows_its_corrected_task(self):
        fixed_tasks = {('hamid', 'reminders', 2060): {'old': 731, 'new': 19}}
        events = [{
            'owner': 'hamid', 'legacy_id': 34648, 'row_id': 'e1',
            'account_id': 'acct', 'related_task_id': 2060,
            'bound': 731, 'payload': {'related_task_id': 2060},
        }]
        updates = plan_event_followups(events, fixed_tasks)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]['customer_id'], 19)
        self.assertEqual(updates[0]['old_customer_id'], 731)

    def test_event_bound_to_another_customer_is_left_alone(self):
        fixed_tasks = {('hamid', 'reminders', 2060): {'old': 731, 'new': 19}}
        events = [{
            'owner': 'hamid', 'legacy_id': 1, 'row_id': 'e1',
            'account_id': 'acct', 'related_task_id': 2060,
            'bound': 999, 'payload': {'related_task_id': 2060},
        }]
        self.assertEqual(plan_event_followups(events, fixed_tasks), [])

    def test_unrelated_task_reference_is_ignored(self):
        fixed_tasks = {('hamid', 'reminders', 2060): {'old': 731, 'new': 19}}
        events = [{
            'owner': 'amy', 'legacy_id': 2, 'row_id': 'e2',
            'account_id': 'acct', 'related_task_id': 2060,
            'bound': 731, 'payload': {'related_task_id': 2060},
        }]
        self.assertEqual(plan_event_followups(events, fixed_tasks), [])

    def test_unbound_event_is_never_guessed(self):
        fixed_tasks = {('hamid', 'reminders', 2060): {'old': 731, 'new': 19}}
        events = [{
            'owner': 'hamid', 'legacy_id': 3, 'row_id': 'e3',
            'account_id': 'acct', 'related_task_id': 2060,
            'bound': None, 'payload': {'related_task_id': 2060},
        }]
        self.assertEqual(plan_event_followups(events, fixed_tasks), [])


if __name__ == '__main__':
    unittest.main()
