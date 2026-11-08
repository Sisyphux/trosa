"""Data-quality guards of the follow-up candidate engine.

One group per known data pitfall (see ``docs/research/2026-10-改造落地路线.md``,
"数据里的七个坑").  Every fixture is synthetic.  ``TODAY`` is a Tuesday;
``D(n)`` is the day ``n`` days before it.
"""

from __future__ import annotations

import copy
import re
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import followup_candidates as fc

TODAY = date(2026, 11, 10)
_next_event_id = [5000]


def D(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


def ev(account_id, days_ago, content, direction='unknown', event_type='follow_up',
       source='manual', payload=None, **extra):
    _next_event_id[0] += 1
    return {'event_id': _next_event_id[0], 'account_id': account_id, 'occurred_at': D(days_ago),
            'event_type': event_type, 'direction': direction, 'content': content,
            'source_module': source, 'payload': payload or {}, **extra}


def customer_mail(account_id, days_ago, content, source='sela_reply_engine'):
    return ev(account_id, days_ago, content, 'inbound', 'customer_reply', source)


def our_quote(account_id, days_ago, **extra):
    return ev(account_id, days_ago, '已发报价 单价 3.2 USD/kg', 'outbound', **extra)


def accounts_for(*ids, **extra):
    return [{'account_id': i, **extra} for i in ids]


def run(accounts, events, open_tasks=(), **kwargs):
    kwargs.setdefault('human_owned_ids', {a['account_id'] for a in accounts})
    kwargs['params'] = {**{'r3': {'min_days': 30, 'interval_multiplier': 2, 'max_days': 365, 'min_contact_days': 3},
                         'guards': {**fc.DEFAULT_PARAMS['guards'], 'complete_reason': False}},
                        **(kwargs.get('params') or {})}
    return fc.scan(accounts, events, open_tasks, TODAY, owner='amy', **kwargs)


def filler(day_ago, count, creator='amy', first_account=900):
    """``count`` unrelated one-line records on one day, spread over throw-away accounts."""
    return [ev(first_account + i, day_ago, '整理资料', created_by=creator) for i in range(count)]


def rules(result):
    return {c.account_id: c.rule for c in result.released}


# ---------------------------------------------------------------------------
# Pitfall: bulk back-fill days
# ---------------------------------------------------------------------------

class BatchDayTest(unittest.TestCase):
    def test_quote_dated_on_a_backfill_day_waits_a_week_longer(self):
        events = [our_quote(1, 18, created_by='amy')] + filler(18, 24)       # 25 records that day
        self.assertEqual(run(accounts_for(1), events).released, [])
        events = [our_quote(1, 22, created_by='amy')] + filler(22, 24)
        c = run(accounts_for(1), events).released[0]
        self.assertEqual(c.rule, 'R2')
        self.assertTrue(c.facts['batch_dated'])
        self.assertNotIn('批量补录', c.reason)

    def test_same_quote_on_a_normal_day_fires_at_14_days(self):
        events = [our_quote(1, 18, created_by='amy')] + filler(18, 23)       # only 24 that day
        c = run(accounts_for(1), events).released[0]
        self.assertEqual(c.rule, 'R2')
        self.assertFalse(c.facts['batch_dated'])
        self.assertNotIn('批量补录', c.reason)

    def test_count_is_per_creator(self):
        events = [our_quote(1, 18, created_by='amy')] + filler(18, 12, 'amy') + filler(18, 20, 'kelley', 950)
        self.assertEqual(rules(run(accounts_for(1), events)), {1: 'R2'})

    def test_threshold_and_extra_days_are_parameters(self):
        events = [our_quote(1, 18, created_by='amy')] + filler(18, 9)
        loose = run(accounts_for(1), events, params={'batch_day': {'threshold': 10, 'extra_days': 3}})
        self.assertEqual(loose.released[0].rule, 'R2')                       # 18 >= 14 + 3
        strict = run(accounts_for(1), events, params={'batch_day': {'threshold': 10, 'extra_days': 7}})
        self.assertEqual(strict.released, [])

    def test_guard_can_be_switched_off(self):
        events = [our_quote(1, 18, created_by='amy')] + filler(18, 24)
        result = run(accounts_for(1), events, params={'guards': {'batch_day': False}})
        self.assertEqual(rules(result), {1: 'R2'})
        self.assertFalse(result.released[0].facts['batch_dated'])

    def test_dormant_deal_client_last_contact_on_a_backfill_day(self):
        # usual gap 10 days -> silence threshold is the 30-day floor, +7 when the last date is a bulk day
        base = [ev(1, 120, '客户下单了，合同已签', 'two_way'), ev(1, 110, '跟进交期'), ev(1, 100, '电话沟通', 'two_way')]
        accounts = accounts_for(1)
        self.assertEqual(rules(run(accounts, base + [ev(1, 35, '发资料', created_by='amy')])), {1: 'R3'})
        batched = base + [ev(1, 35, '发资料', created_by='amy')] + filler(35, 24)
        self.assertEqual(run(accounts, batched).released, [])
        longer = base + [ev(1, 38, '发资料', created_by='amy')] + filler(38, 24)
        c = run(accounts, longer).released[0]
        self.assertEqual(c.rule, 'R3')
        self.assertTrue(c.facts['batch_dated'])
        self.assertNotIn('批量补录', c.reason)

    def test_customer_waiting_is_not_delayed_by_a_backfill_day(self):
        events = [customer_mail(1, 5, 'Please send the updated price list')] + filler(5, 30)
        self.assertEqual(rules(run(accounts_for(1), events)), {1: 'R1'})


# ---------------------------------------------------------------------------
# Pitfall: duplicate customers
# ---------------------------------------------------------------------------

class DuplicateNameTest(unittest.TestCase):
    def test_newer_record_on_a_twin_holds_the_stale_one_back_and_flags_it(self):
        accounts = [{'account_id': 1, 'name': 'Acme Co., Ltd.'}, {'account_id': 2, 'name': 'ACME CO LTD'}]
        events = [our_quote(1, 20), ev(2, 3, '电话沟通', 'two_way')]
        result = run(accounts, events)
        self.assertEqual(result.released, [])
        self.assertEqual(result.excluded['possible_duplicate'], 1)
        held = result.suppressed[0]
        self.assertEqual((held.account_id, held.rule), (1, 'R2'))
        self.assertEqual(held.facts['suppressed'], 'possible_duplicate')
        self.assertEqual(held.facts['duplicate_account_ids'], [2])
        self.assertIn('同名客户', held.reason)

    def test_the_twin_with_the_newest_record_is_not_held_back(self):
        accounts = [{'account_id': 1, 'name': 'Acme'}, {'account_id': 2, 'name': 'acme'}]
        events = [our_quote(1, 30), our_quote(2, 20)]
        result = run(accounts, events)
        self.assertEqual(rules(result), {2: 'R2'})
        self.assertEqual([c.account_id for c in result.suppressed], [1])

    def test_same_day_twins_are_both_kept(self):
        accounts = [{'account_id': 1, 'name': 'Acme'}, {'account_id': 2, 'name': 'Acme'}]
        events = [our_quote(1, 20), our_quote(2, 20)]
        self.assertEqual(rules(run(accounts, events)), {1: 'R2', 2: 'R2'})

    def test_different_names_and_twin_without_records_do_not_interfere(self):
        accounts = [{'account_id': 1, 'name': 'Acme'}, {'account_id': 2, 'name': 'Acme Plastics'},
                    {'account_id': 3, 'name': 'Acme'}]
        events = [our_quote(1, 20), ev(2, 2, '电话沟通', 'two_way')]
        self.assertEqual(rules(run(accounts, events)), {1: 'R2'})

    def test_chinese_names_and_full_width_forms_are_normalised(self):
        self.assertEqual(fc.normalise_name('深圳 华信（亚克力）有限公司'), fc.normalise_name('深圳华信(亚克力)有限公司'))
        self.assertEqual(fc.normalise_name('ＡＢＣ　Trading'), fc.normalise_name('abc trading'))
        self.assertEqual(fc.normalise_name(None), '')

    def test_accounts_without_a_name_skip_the_guard(self):
        events = [our_quote(1, 20), ev(2, 3, '电话沟通', 'two_way')]
        self.assertEqual(rules(run(accounts_for(1, 2), events)), {1: 'R2'})

    def test_very_short_names_are_too_generic_to_compare(self):
        accounts = [{'account_id': 1, 'name': 'AB'}, {'account_id': 2, 'name': 'ab'}]
        events = [our_quote(1, 20), ev(2, 3, '电话沟通', 'two_way')]
        self.assertEqual(rules(run(accounts, events)), {1: 'R2'})
        strict = run(accounts, events, params={'duplicates': {'min_name_length': 2}})
        self.assertEqual(strict.released, [])

    def test_guard_can_be_switched_off(self):
        accounts = [{'account_id': 1, 'name': 'Acme'}, {'account_id': 2, 'name': 'Acme'}]
        events = [our_quote(1, 20), ev(2, 3, '电话沟通', 'two_way')]
        result = run(accounts, events, params={'guards': {'duplicate_names': False}})
        self.assertEqual(rules(result), {1: 'R2'})
        self.assertEqual(result.suppressed, [])

    def test_twin_held_by_another_member_counts_through_peer_last_contact(self):
        accounts = [{'account_id': 1, 'name': 'Acme Plastics'}]
        events = [our_quote(1, 20)]
        self.assertEqual(rules(run(accounts, events)), {1: 'R2'})
        result = run(accounts, events, peer_last_contact={'ACME-plastics': D(2)})
        self.assertEqual(result.released, [])
        self.assertEqual(result.suppressed[0].facts['duplicate_account_ids'], ['other-member'])
        older = run(accounts, events, peer_last_contact={'Acme Plastics': D(40)})
        self.assertEqual(rules(older), {1: 'R2'})

    def test_twin_does_not_suppress_when_the_stale_one_would_not_have_fired(self):
        accounts = [{'account_id': 1, 'name': 'Acme'}, {'account_id': 2, 'name': 'Acme'}]
        events = [ev(1, 20, '随便聊聊'), ev(2, 3, '电话沟通', 'two_way')]
        result = run(accounts, events)
        self.assertEqual((result.released, result.suppressed), ([], []))


# ---------------------------------------------------------------------------
# Pitfall: auto-replies and delivery failures inside the timeline
# ---------------------------------------------------------------------------

class AutoReplyTest(unittest.TestCase):
    AUTO_REPLIES = [
        'Out of office until Monday, I will reply when back.',
        'This is an auto-reply: the recipient is travelling.',
        'Automatic reply: Re: your quotation',
        'Hello, I am currently out and will answer on my return.',
        '自动回复：我正在休假中,回国后联系您。',
        '您好，我目前休假中，12月1日后回复。',
        'Respuesta automática: estoy fuera de la oficina.',
        'Your request has been received and a ticket was created.',
        'This is an automated message, please do not reply to this email.',
    ]
    REAL_MAILS = [
        'Our factory is closed for the holiday from Dec 25 to Jan 2, please send the quotation before then.',
        'We opened a ticket with the courier about the damaged sample, can you resend two sheets?',
        'The vacation season is slow here but we still want 2 containers in January.',
        'Please confirm the holiday shipping schedule and the closing date for the PI.',
        'Our boss is out of town next week, but I can approve the sample today.',
        'Can you reply to the ticket I sent yesterday about the colour card?',
    ]

    def test_typical_auto_reply_sentences_are_not_the_customer_speaking(self):
        for text in self.AUTO_REPLIES:
            with self.subTest(text=text):
                self.assertEqual(run(accounts_for(1), [customer_mail(1, 5, text)]).released, [])

    def test_real_customer_mails_with_holiday_ticket_vacation_words_still_count(self):
        for text in self.REAL_MAILS:
            with self.subTest(text=text):
                c = run(accounts_for(1), [customer_mail(1, 5, text)]).released
                self.assertEqual([x.rule for x in c], ['R1'], text)

    def test_an_auto_reply_does_not_hide_the_real_mail_before_it(self):
        events = [customer_mail(1, 8, 'Please quote 3mm clear sheets'),
                  customer_mail(1, 5, 'Out of office until Monday')]
        c = run(accounts_for(1), events).released[0]
        self.assertEqual((c.rule, c.facts['customer_spoke_on']), ('R1', D(8)))

    def test_only_customer_side_rows_are_filtered(self):
        events = [ev(1, 20, '已发报价，对方自动回复了 out of office', 'outbound')]
        self.assertEqual(rules(run(accounts_for(1), events)), {1: 'R2'})

    def test_guard_can_be_switched_off(self):
        result = run(accounts_for(1), [customer_mail(1, 5, 'Out of office until Monday')],
                     params={'guards': {'auto_reply': False}})
        self.assertEqual(rules(result), {1: 'R1'})


# ---------------------------------------------------------------------------
# Pitfall: archived customers that are still being contacted
# ---------------------------------------------------------------------------

class ArchivedContactTest(unittest.TestCase):
    def test_contact_after_archiving_is_reported_but_never_reminded(self):
        accounts = [{'account_id': 1, 'deleted_at': D(30)}]
        result = run(accounts, [customer_mail(1, 5, 'Are you still there?')])
        self.assertEqual(result.released, [])
        self.assertEqual(result.excluded, {'archived': 1})
        self.assertEqual(result.archived_with_contact,
                         [{'account_id': 1, 'last_record_on': D(5), 'archived_on': D(30)}])

    def test_records_only_from_before_archiving_are_not_reported(self):
        accounts = [{'account_id': 1, 'deleted_at': D(5)}]
        result = run(accounts, [customer_mail(1, 30, 'Old mail')])
        self.assertEqual((result.released, result.archived_with_contact), ([], []))

    def test_archive_timestamp_with_a_time_is_understood(self):
        accounts = [{'account_id': 1, 'deleted_at': D(10) + 'T09:30:00+08:00'}]
        result = run(accounts, [customer_mail(1, 4, 'Hello again')])
        self.assertEqual(result.archived_with_contact[0]['archived_on'], D(10))

    def test_guard_off_stops_the_report_but_not_the_exclusion(self):
        accounts = [{'account_id': 1, 'deleted_at': D(30)}]
        result = run(accounts, [customer_mail(1, 5, 'Are you still there?')],
                     params={'guards': {'archived_contact': False}})
        self.assertEqual((result.released, result.archived_with_contact), ([], []))
        self.assertEqual(result.excluded, {'archived': 1})


# ---------------------------------------------------------------------------
# Weeks, not days, in the wording
# ---------------------------------------------------------------------------

class WeeksWordingTest(unittest.TestCase):
    def test_reasons_never_show_a_day_count(self):
        cases = [
            run(accounts_for(1), [ev(1, 3, 'Customer asks for lead time', 'inbound')]),
            run(accounts_for(1), [ev(1, 12, 'Customer asks for lead time', 'inbound')]),
            run(accounts_for(1), [our_quote(1, 20)]),
        ]
        for result in cases:
            reason = result.released[0].reason
            with self.subTest(reason=reason):
                self.assertNotRegex(reason, r'(?<![\d-])\d+ 天')

    def test_hint_uses_less_than_a_week_or_about_n_weeks(self):
        short = run(accounts_for(1), [ev(1, 3, 'Customer asks for lead time', 'inbound')]).released[0]
        self.assertIn('不到一周前', short.reason)
        longer = run(accounts_for(1), [ev(1, 15, 'Customer asks for lead time', 'inbound')]).released[0]
        self.assertIn('约 2 周前', longer.reason)

    def test_day_counts_stay_in_the_facts(self):
        c = run(accounts_for(1), [ev(1, 15, 'Customer asks for lead time', 'inbound')]).released[0]
        self.assertEqual(c.facts['days_ago'], 15)

    def test_guard_can_be_switched_off(self):
        c = run(accounts_for(1), [ev(1, 3, 'Customer asks for lead time', 'inbound')],
                params={'guards': {'weeks_display': False}}).released[0]
        self.assertIn('3 天前', c.reason)


# ---------------------------------------------------------------------------
# Pitfall: legacy 15/30/60 outreach reminders must never matter
# ---------------------------------------------------------------------------

class OutreachNoiseTest(unittest.TestCase):
    def setUp(self):
        self.accounts = accounts_for(1, 2, 3, 4, 5)
        self.base = [customer_mail(1, 5, 'Please send the price list'), our_quote(2, 20),
                     ev(3, 3, '客户下周回来再定', 'two_way')]

    def noise(self):
        rows = []
        for i in range(300):
            account = 1 + i % 5
            rows.append(ev(account, 1 + i % 40, '开发信第一次', 'outbound', event_type=f'outreach_{15 * (1 + i % 3)}d'))
            rows.append(ev(account, 2 + i % 30, '外联发信', 'outbound', event_type='外联发信'))
            rows.append(ev(account, 1 + i % 20, 'Hello from the sequence', 'outbound', source='outreach_auto'))
            rows.append(ev(account, 1 + i % 10, '已发送', 'outbound', event_type='follow_up',
                           payload={'source': 'outreach_sequence'}))
            rows.append(ev(account, 4, '开发节点已完成', event_type='task_completed'))
        return rows

    def test_a_flood_of_outreach_rows_changes_nothing(self):
        clean = run(self.accounts, self.base)
        noisy = run(self.accounts, self.base + self.noise())
        self.assertEqual([c.as_dict() for c in noisy.released], [c.as_dict() for c in clean.released])
        self.assertEqual(noisy.excluded, clean.excluded)
        self.assertEqual(noisy.fired, clean.fired)
        self.assertTrue(clean.released)

    def test_accounts_with_only_outreach_rows_have_no_signal(self):
        result = run(accounts_for(7), [{**r, 'account_id': 7} for r in self.noise()])
        self.assertEqual((result.released, result.excluded), ([], {'no_real_events': 1}))

    def test_inputs_are_not_modified(self):
        events = self.base + self.noise()
        before = copy.deepcopy(events)
        run(self.accounts, events)
        self.assertEqual(events, before)

    def test_open_task_helper_ignores_outreach_reminders(self):
        reminders = [
            {'customer_id': 1, 'reminder_type': 'outreach_15', 'is_done': 0},
            {'customer_id': 1, 'reminder_type': 'outreach_60', 'is_done': 0},
            {'customer_id': 2, 'reminder_type': 'follow_up', 'is_done': 0},
            {'customer_id': 3, 'reminder_type': None, 'is_done': 0},
            {'customer_id': 4, 'reminder_type': 'follow_up', 'is_done': 1},
            {'account_id': 5, 'reminder_type': 'follow_up', 'status': 'done'},
        ]
        self.assertEqual(fc.open_task_account_ids(reminders), {2, 3})

    def test_hundreds_of_outreach_reminders_do_not_hide_a_customer(self):
        reminders = [{'customer_id': 1, 'reminder_type': 'outreach_30', 'is_done': 0} for _ in range(300)]
        result = run(self.accounts, self.base, open_tasks=fc.open_task_account_ids(reminders))
        self.assertEqual(rules(result)[1], 'R1')


if __name__ == '__main__':
    unittest.main()
