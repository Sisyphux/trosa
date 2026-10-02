import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

from clean_communication_content import find_candidates, iter_events


class _FakeGateway:
    """Return fixed pages for /messages/search and record the offsets used."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.offsets = []

    def agent_get(self, path, **params):
        self.offsets.append(params.get('offset', 0))
        index = len(self.offsets) - 1
        return self.pages[index] if index < len(self.pages) else {'items': [], 'total': 0}


QUOTED = ('We need samples.\n'
          'Sent from my iPhone\n'
          'On Monday Buyer wrote:\n'
          '> old message')


class FindCandidatesTest(unittest.TestCase):
    def test_flags_quoted_communication_and_computes_clean_body(self):
        items = [
            {'event_type': 'communication', 'event_id': 1, 'customer_id': 7,
             'customer_name': 'Acme', 'event_date': '2026-09-11',
             'activity_type': 'email', 'direction': 'inbound', 'content': QUOTED},
            {'event_type': 'communication', 'event_id': 2, 'customer_id': 7,
             'content': '客户确认报价，下周安排样品。'},
            {'event_type': 'email', 'event_id': 3, 'content': QUOTED},
            {'event_type': 'communication', 'event_id': 4, 'content': 'short > quote'},
        ]
        candidates = find_candidates(items, min_length=20)
        self.assertEqual([item['log_id'] for item in candidates], [1])
        self.assertEqual(candidates[0]['after'], 'We need samples.')
        self.assertEqual(candidates[0]['before_length'], len(QUOTED))

    def test_min_length_filters_short_noise(self):
        items = [{'event_type': 'communication', 'event_id': 9, 'content': '> x'}]
        self.assertEqual(find_candidates(items, min_length=400), [])

    def test_skips_non_communication_and_non_int_ids(self):
        items = [
            {'event_type': 'outreach_email', 'event_id': 1, 'content': QUOTED},
            {'event_type': 'communication', 'event_id': '1', 'content': QUOTED},
        ]
        self.assertEqual(find_candidates(items), [])


class IterEventsTest(unittest.TestCase):
    def test_pages_until_short_page(self):
        page_one = {'items': [{'event_id': 1}, {'event_id': 2}], 'total': 3}
        page_two = {'items': [{'event_id': 3}], 'total': 3}
        gateway = _FakeGateway([page_one, page_two])
        items = list(iter_events(gateway, page_size=2))
        self.assertEqual([item['event_id'] for item in items], [1, 2, 3])
        self.assertEqual(gateway.offsets, [0, 2])

    def test_stops_at_total(self):
        gateway = _FakeGateway([{'items': [{'event_id': 1}, {'event_id': 2}], 'total': 2}])
        items = list(iter_events(gateway, page_size=2))
        self.assertEqual(len(items), 2)
        self.assertEqual(gateway.offsets, [0])

    def test_max_pages_caps_requests(self):
        gateway = _FakeGateway([
            {'items': [{'event_id': 1}, {'event_id': 2}], 'total': 10},
            {'items': [{'event_id': 3}, {'event_id': 4}], 'total': 10},
        ])
        items = list(iter_events(gateway, page_size=2, max_pages=1))
        self.assertEqual([item['event_id'] for item in items], [1, 2])
        self.assertEqual(gateway.offsets, [0])


if __name__ == '__main__':
    unittest.main()
