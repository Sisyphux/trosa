"""Rules and limits of the deterministic follow-up candidate engine.

Every fixture here is synthetic: invented account numbers and made-up message
text.  ``TODAY`` is a Tuesday; ``D(n)`` is the day ``n`` days before it.
"""

from __future__ import annotations

import copy
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import followup_candidates as fc

TODAY = date(2026, 11, 10)
_next_event_id = [1000]


def D(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


def ev(account_id, days_ago, content, direction='unknown', event_type='follow_up',
       source='manual', payload=None, **extra):
    _next_event_id[0] += 1
    return {'event_id': _next_event_id[0], 'account_id': account_id, 'occurred_at': D(days_ago),
            'event_type': event_type, 'direction': direction, 'content': content,
            'source_module': source, 'payload': payload or {}, **extra}


def sela_reply(account_id, days_ago, content='Thanks, could you update the plan for Q1?'):
    return ev(account_id, days_ago, content, 'inbound', 'customer_reply', 'sela_reply_engine')


def our_quote(account_id, days_ago, content='已发报价 单价 3.2 USD/kg'):
    return ev(account_id, days_ago, content, 'outbound')


def accounts_for(*ids, **extra):
    return [{'account_id': i, **extra} for i in ids]


# The wide thresholds the rule mechanics were first written against.  The shipped defaults are
# much tighter (see PreciseDefaultsTest); the mechanics tests keep this set so they stay about mechanics.
WIDE = {
    'r2': {'max_days': 90},
    'r3': {'min_days': 30, 'interval_multiplier': 2, 'max_days': 365, 'min_contact_days': 3, 'weekly_cap': 2},
    'limits': {'daily_cap': 5, 'launch_cap': 3, 'r1_total_cap': 8},
    'priority': ['R1:certain', 'R2', 'R1:hint', 'R4', 'R3'],
    'guards': {**fc.DEFAULT_PARAMS['guards'], 'complete_reason': False},
}


def run(accounts, events, open_tasks=(), classify=False, **kwargs):
    """Scan with the Sela-only classifier bypassed unless ``classify`` asks for it."""
    if not classify:
        kwargs.setdefault('human_owned_ids', {a['account_id'] for a in accounts})
    kwargs['params'] = {**WIDE, **(kwargs.get('params') or {})}
    return fc.scan(accounts, events, open_tasks, TODAY, owner='amy', **kwargs)


def only(result):
    assert len(result.released) == 1, [c.as_dict() for c in result.released]
    return result.released[0]


class CustomerWaitingTest(unittest.TestCase):
    def test_sela_reply_without_answer_is_certain(self):
        c = only(run(accounts_for(1), [sela_reply(1, 5)]))
        self.assertEqual((c.rule, c.certainty), ('R1', 'certain'))
        self.assertEqual(c.facts['customer_spoke_on'], D(5))
        self.assertEqual(c.facts['source'], 'Sela 交接')
        spoke = date.fromisoformat(D(5))
        self.assertIn(f'{spoke.month}月{spoke.day}日', c.reason)

    def test_inbox_capture_is_certain_too(self):
        events = [ev(1, 4, 'Customer asks for lead time', 'inbound', 'follow_up', 'inbox_capture')]
        self.assertEqual(only(run(accounts_for(1), events)).certainty, 'certain')

    def test_manual_inbound_is_only_a_hint(self):
        events = [ev(1, 5, 'Customer asks for lead time', 'inbound')]
        c = only(run(accounts_for(1), events))
        self.assertEqual((c.rule, c.certainty), ('R1', 'hint'))
        self.assertNotIn('其实已经聊过', c.reason)

    def test_we_answered_the_same_day_does_not_trigger(self):
        events = [sela_reply(1, 5), ev(1, 5, '已回复交期', 'outbound')]
        self.assertEqual(run(accounts_for(1), events).released, [])

    def test_two_way_record_counts_as_our_side(self):
        events = [sela_reply(1, 5), ev(1, 5, '电话沟通了交期', 'two_way')]
        self.assertEqual(run(accounts_for(1), events).released, [])

    def test_later_record_of_ours_clears_it(self):
        events = [sela_reply(1, 6), ev(1, 3, '已回复交期', 'outbound')]
        self.assertEqual(run(accounts_for(1), events).released, [])

    def test_age_window(self):
        for days, expected in ((1, 0), (2, 1), (30, 1), (31, 0)):
            with self.subTest(days=days):
                self.assertEqual(len(run(accounts_for(1), [sela_reply(1, days)]).released), expected)

    def test_unknown_direction_with_customer_voice_is_a_hint(self):
        events = [ev(1, 6, '客户回复说价格还在内部审批')]
        c = only(run(accounts_for(1), events))
        self.assertEqual((c.rule, c.certainty), ('R1', 'hint'))

    def test_unknown_direction_without_customer_voice_is_ignored(self):
        events = [ev(1, 6, '整理了一下资料')]
        self.assertEqual(run(accounts_for(1), events).released, [])

    def test_unknown_record_on_the_same_day_demotes_certain_to_hint(self):
        events = [sela_reply(1, 5), ev(1, 5, '整理了一下资料')]
        self.assertEqual(only(run(accounts_for(1), events)).certainty, 'hint')

    def test_last_speaker_in_payload_marks_a_two_way_record(self):
        events = [ev(1, 5, 'WhatsApp 往来', 'two_way', payload={'last_speaker': 'customer'})]
        c = only(run(accounts_for(1), events))
        self.assertEqual((c.rule, c.certainty), ('R1', 'hint'))

    def test_auto_reply_is_not_customer_speech(self):
        events = [ev(1, 5, 'Out of office until Monday, automatic reply', 'inbound', 'follow_up', 'sela_reply_engine')]
        self.assertEqual(run(accounts_for(1), events).released, [])

    def test_auto_reply_guard_can_be_switched_off(self):
        events = [ev(1, 5, 'Out of office until Monday', 'inbound', 'follow_up', 'sela_reply_engine')]
        c = only(run(accounts_for(1), events, params={'guards': {'auto_reply': False}}))
        self.assertEqual(c.rule, 'R1')

    def test_bounce_notice_is_not_customer_speech_but_a_price_with_550_is(self):
        bounce = ev(1, 5, 'Delivery Status Notification: undeliverable', 'inbound', 'follow_up', 'sela_reply_engine')
        price = ev(2, 5, 'Our budget is 550 per ton, please quote', 'inbound', 'follow_up', 'sela_reply_engine')
        result = run(accounts_for(1, 2), [bounce, price])
        self.assertEqual([c.account_id for c in result.released], [2])

    def test_noise_events_do_not_move_the_last_contact_day(self):
        events = [
            sela_reply(1, 6),
            ev(1, 4, 'x', 'outbound', 'outreach_bounced'),
            ev(1, 3, '开发信', 'outbound', 'outreach'),
            ev(1, 3, '完成待办', 'outbound', 'task_completed'),
            ev(1, 2, '本周汇总', 'outbound', '周内汇总'),
            ev(1, 2, '已回复', 'outbound', 'follow_up', payload={'is_deleted': True}),
            ev(1, 2, '已回复', 'outbound', 'follow_up', is_deleted=1),
        ]
        c = only(run(accounts_for(1), events))
        self.assertEqual((c.rule, c.certainty, c.facts['customer_spoke_on']), ('R1', 'certain', D(6)))


class QuoteSampleSilentTest(unittest.TestCase):
    def test_quote_then_silence(self):
        c = only(run(accounts_for(1), [our_quote(1, 20)]))
        self.assertEqual((c.rule, c.certainty), ('R2', 'certain'))
        self.assertEqual((c.facts['deal_kind'], c.facts['sent_on'], c.facts['weeks_silent']), ('quote', D(20), 3))
        self.assertIn('报价', c.reason)
        self.assertIn('3 周', c.reason)

    def test_sample_and_pi(self):
        sample = only(run(accounts_for(1), [ev(1, 16, '已寄样 DHL', 'outbound')]))
        pi = only(run(accounts_for(1), [ev(1, 16, '已发 proforma invoice 给客户', 'outbound')]))
        self.assertEqual((sample.facts['deal_kind'], pi.facts['deal_kind']), ('sample', 'pi'))

    def test_payload_deal_events_and_type_label(self):
        by_payload = ev(1, 20, '发了文件', 'outbound', payload={'deal_events': ['pi']})
        by_type = ev(2, 20, '发了文件', 'outbound', event_type='报价')
        result = run(accounts_for(1, 2), [by_payload, by_type])
        kinds = {c.account_id: c.facts['deal_kind'] for c in result.released}
        self.assertEqual(kinds, {1: 'pi', 2: 'quote'})

    def test_window_is_14_to_90_days(self):
        for days, expected in ((13, 0), (14, 1), (90, 1), (91, 0)):
            with self.subTest(days=days):
                self.assertEqual(len(run(accounts_for(1), [our_quote(1, days)]).released), expected)

    def test_any_later_record_ends_it(self):
        events = [our_quote(1, 30), ev(1, 5, '电话没接通', 'outbound')]
        self.assertEqual(run(accounts_for(1), events).released, [])

    def test_customer_answering_on_the_same_day_is_not_silence(self):
        events = [our_quote(1, 20), ev(1, 20, 'Customer says thanks', 'inbound')]
        self.assertEqual(run(accounts_for(1), events).released, [])

    def test_rejection_does_not_trigger(self):
        events = [our_quote(1, 20, '已发报价，客户表示暂时没有需求')]
        self.assertEqual(run(accounts_for(1), events).released, [])

    def test_asking_whether_a_quote_is_wanted_is_not_a_quote(self):
        events = [ev(1, 20, '询问客户是否需要报价', 'outbound')]
        self.assertEqual(run(accounts_for(1), events).released, [])

    def test_threshold_is_a_parameter(self):
        self.assertEqual(len(run(accounts_for(1), [our_quote(1, 8)], params={'r2': {'min_days': 7}}).released), 1)


class DormantDealClientTest(unittest.TestCase):
    @staticmethod
    def deal_history(account_id, last_contact_days_ago, gaps=(20, 30, 50)):
        """Four contact days, the first one a deal, ending ``last_contact_days_ago`` ago."""
        days = [last_contact_days_ago]
        for gap in reversed(gaps):
            days.append(days[-1] + gap)
        days.reverse()
        events = [ev(account_id, days[0], '客户确认订单并付款，定金已到', 'two_way')]
        events += [ev(account_id, d, '例行联系，聊了市场情况', 'two_way') for d in days[1:]]
        return events

    def test_quiet_deal_client_is_a_hint_with_facts(self):
        c = only(run(accounts_for(1), self.deal_history(1, 130)))
        self.assertEqual((c.rule, c.certainty), ('R3', 'hint'))
        self.assertEqual(c.facts['usual_gap_days'], 30)
        self.assertEqual(c.facts['weeks_silent'], _weeks(130))
        self.assertEqual(c.facts['last_deal_on'], D(130 + 50 + 30 + 20))

    def test_silence_must_exceed_twice_the_usual_gap(self):
        # usual gap 30 days -> needs 60 days of silence
        self.assertEqual(run(accounts_for(1), self.deal_history(1, 59)).released, [])
        self.assertEqual(len(run(accounts_for(1), self.deal_history(1, 61)).released), 1)

    def test_floor_of_30_days_for_chatty_clients(self):
        chatty = self.deal_history(1, 25, gaps=(3, 4, 5))
        self.assertEqual(run(accounts_for(1), chatty).released, [])
        self.assertEqual(len(run(accounts_for(1), self.deal_history(1, 31, gaps=(3, 4, 5))).released), 1)

    def test_too_old_is_dropped(self):
        self.assertEqual(run(accounts_for(1), self.deal_history(1, 366)).released, [])

    def test_needs_three_contact_days(self):
        events = self.deal_history(1, 130)[:2]
        self.assertEqual(run(accounts_for(1), events).released, [])

    def test_negated_order_is_not_a_deal(self):
        events = [ev(1, d, '客户说目前没有订单', 'two_way') for d in (300, 250, 200, 150)]
        self.assertEqual(run(accounts_for(1), events).released, [])

    def test_no_deal_wording_no_signal(self):
        events = [ev(1, d, '聊了聊天', 'two_way') for d in (300, 250, 200, 150)]
        self.assertEqual(run(accounts_for(1), events).released, [])

    def test_weekly_cap_keeps_the_newest_deals(self):
        ids = (1, 2, 3)
        events = [e for i, last in zip(ids, (130, 140, 150)) for e in self.deal_history(i, last)]
        result = run(accounts_for(*ids), events)
        self.assertEqual({c.account_id for c in result.released}, {1, 2})
        self.assertEqual([c.account_id for c in result.held_back], [3])

    def test_cap_counts_what_was_already_shown_this_week(self):
        events = self.deal_history(1, 130)
        self.assertEqual(run(accounts_for(1), events, r3_released_this_week=2).released, [])
        self.assertEqual(len(run(accounts_for(1), events, r3_released_this_week=1).released), 1)


def _weeks(days):
    return max(1, round(days / 7))


class CustomerSaidDateTest(unittest.TestCase):
    def test_next_week_has_passed_without_follow_up(self):
        events = [ev(1, 20, '客户说下周确认数量', 'two_way')]
        c = only(run(accounts_for(1), events))
        self.assertEqual((c.rule, c.certainty), ('R4', 'hint'))
        self.assertIn('下周', c.facts['quote'])
        self.assertEqual(c.facts['stated_date'], '2026-11-01')

    def test_not_yet_due(self):
        for days in (2, 5):
            with self.subTest(days=days):
                self.assertEqual(run(accounts_for(1), [ev(1, days, '客户说下周确认数量', 'two_way')]).released, [])

    def test_grace_period_after_the_date(self):
        # TODAY is 11-10; the named date must be at least 3 days behind us
        for named, expected in ('11月8日', 0), ('11月7日', 1):
            with self.subTest(named=named):
                events = [ev(1, 20, f'客户说{named}给答复', 'two_way')]
                self.assertEqual(len(run(accounts_for(1), events).released), expected)

    def test_explicit_dates(self):
        cn = only(run(accounts_for(1), [ev(1, 20, '客户说11月1日前给答复', 'two_way')]))
        iso = only(run(accounts_for(1), [ev(1, 20, 'customer will confirm by 2026-11-02', 'two_way')]))
        en = only(run(accounts_for(1), [ev(1, 20, 'customer said Nov 2 for the decision', 'two_way')]))
        self.assertEqual((cn.facts['stated_date'], iso.facts['stated_date'], en.facts['stated_date']),
                         ('2026-11-01', '2026-11-02', '2026-11-02'))

    def test_dates_in_the_past_are_ignored(self):
        events = [ev(1, 20, '10月5日客户来过电话', 'two_way')]
        self.assertEqual(run(accounts_for(1), events).released, [])

    def test_month_end_and_after_holiday(self):
        month_end = only(run(accounts_for(1), [ev(1, 25, '客户说月底给答复', 'two_way')]))
        after = only(run(accounts_for(1), [ev(1, 30, '客户说节后再看', 'two_way')]))
        self.assertEqual(month_end.facts['stated_date'], '2026-10-31')
        self.assertEqual(after.facts['stated_date'], '2026-10-25')

    def test_any_later_record_cancels(self):
        events = [ev(1, 20, '客户说下周确认数量', 'two_way'), ev(1, 6, '提醒了客户', 'outbound')]
        self.assertEqual(run(accounts_for(1), events).released, [])

    def test_our_own_plan_is_not_the_customers_word(self):
        events = [ev(1, 20, '电话聊了产品', 'two_way', next_plan='下周发样品')]
        self.assertEqual(run(accounts_for(1), events).released, [])

    def test_can_be_limited_to_a_supporting_signal(self):
        events = [ev(1, 20, '客户说下周确认数量', 'two_way')]
        self.assertEqual(run(accounts_for(1), events, params={'r4': {'standalone': False}}).released, [])

    def test_supporting_only_mode_rides_along_with_another_rule(self):
        events = [sela_reply(1, 20, '下周会给你答复')]
        c = only(run(accounts_for(1), events, params={'r4': {'standalone': False}}))
        self.assertEqual(c.rule, 'R1')
        self.assertIn('R4', c.supporting)


class FilterTest(unittest.TestCase):
    def test_archived_customer_is_excluded(self):
        result = run([{'account_id': 1, 'deleted_at': '2026-10-01'}], [sela_reply(1, 5)])
        self.assertEqual((result.released, result.excluded), ([], {'archived': 1}))

    def test_open_task_means_already_on_today(self):
        result = run(accounts_for(1), [sela_reply(1, 5)], open_tasks={1})
        self.assertEqual((result.released, result.excluded), ([], {'has_open_task': 1}))

    def test_sela_only_prospect_is_excluded_by_the_classifier(self):
        quote_mail = ev(1, 20, '开发信 报价 单价 每公斤 3.2', 'outbound')
        human = our_quote(2, 20)
        result = run(accounts_for(1, 2), [quote_mail, human], classify=True)
        self.assertEqual([c.account_id for c in result.released], [2])
        self.assertEqual(result.excluded, {'sela_only': 1})

    def test_human_customer_passes_the_classifier(self):
        c = only(run(accounts_for(1), [sela_reply(1, 5)], classify=True))
        self.assertEqual(c.rule, 'R1')

    def test_no_real_events_means_no_signal(self):
        # Summary fields are never read; an account without timeline rows is not "long quiet".
        accounts = [{'account_id': 1, 'last_contact_at': D(40), 'contact_state': 'contacted',
                     'next_task_date': D(5)}]
        result = run(accounts, [])
        self.assertEqual((result.released, result.excluded), ([], {'no_real_events': 1}))

    def test_muted_evidence_never_comes_back(self):
        event = our_quote(1, 20)
        self.assertEqual(run(accounts_for(1), [event], muted={(1, event['event_id'])}).released, [])
        self.assertEqual(run(accounts_for(1), [event], muted={(1, 'R2', event['event_id'])}).released, [])
        self.assertEqual(len(run(accounts_for(1), [event], muted={(1, 'R1', event['event_id'])}).released), 1)

    def test_new_evidence_is_not_muted_by_old_mute(self):
        old, new = our_quote(1, 40), our_quote(1, 20)
        self.assertEqual(len(run(accounts_for(1), [old, new], muted={(1, old['event_id'])}).released), 1)

    def test_ignored_twice_in_two_weeks_cools_the_customer(self):
        events = [sela_reply(1, 5)]
        twice = {1: [D(3), D(10)]}
        self.assertEqual(run(accounts_for(1), events, ignored=twice).released, [])
        self.assertEqual(len(run(accounts_for(1), events, ignored={1: [D(3)]}).released), 1)
        self.assertEqual(len(run(accounts_for(1), events, ignored={1: [D(3), D(20)]}).released), 1)

    def test_one_candidate_per_customer_best_rule_wins(self):
        events = DormantDealClientTest.deal_history(1, 130)[:-1] + [sela_reply(1, 5)]
        c = only(run(accounts_for(1), events))
        self.assertEqual((c.rule, c.certainty), ('R1', 'certain'))
        self.assertEqual(c.dedupe_key, f'amy:1:R1:{c.evidence_event_id}')

    def test_inputs_are_not_modified(self):
        accounts, events = accounts_for(1), [sela_reply(1, 5), our_quote(2, 20)]
        before = copy.deepcopy((accounts, events))
        run(accounts_for(1, 2), events)
        run(accounts, events)
        self.assertEqual((accounts, events), before)

    def test_future_dated_rows_are_ignored(self):
        events = [sela_reply(1, 5), ev(1, -2, '已回复', 'outbound')]
        self.assertEqual(len(run(accounts_for(1), events).released), 1)

    def test_timezone_is_applied_to_aware_timestamps(self):
        # 17:30 UTC on 11-03 is already 11-04 in Beijing -> six days ago.
        event = ev(1, 0, 'Customer asks', 'inbound', 'follow_up', 'sela_reply_engine')
        event['occurred_at'] = '2026-11-03T17:30:00+00:00'
        c = only(run(accounts_for(1), [event]))
        self.assertEqual((c.facts['customer_spoke_on'], c.facts['days_ago']), ('2026-11-04', 6))


class LimitsTest(unittest.TestCase):
    def quotes(self, count, start=1):
        ids = range(start, start + count)
        return accounts_for(*ids), [our_quote(i, 20 + i) for i in ids]

    def test_daily_cap_is_five(self):
        accounts, events = self.quotes(7)
        result = run(accounts, events)
        self.assertEqual((len(result.released), len(result.held_back)), (5, 2))

    def test_launch_days_use_the_smaller_cap(self):
        accounts, events = self.quotes(7)
        for offset, expected in ((0, 3), (2, 3), (3, 5)):
            with self.subTest(offset=offset):
                result = run(accounts, events, launch_date=TODAY - timedelta(days=offset))
                self.assertEqual(len(result.released), expected)

    def test_priority_order(self):
        events = [
            sela_reply(1, 5),                                                   # R1 certain
            our_quote(2, 20),                                                   # R2
            ev(3, 6, 'Customer asks for lead time', 'inbound'),                 # R1 hint
            ev(4, 20, '客户说下周确认数量', 'two_way'),                          # R4
            *DormantDealClientTest.deal_history(5, 130),                        # R3
        ]
        result = run(accounts_for(5, 4, 3, 2, 1), events)
        self.assertEqual([(c.account_id, c.rule, c.certainty) for c in result.released],
                         [(1, 'R1', 'certain'), (2, 'R2', 'certain'), (3, 'R1', 'hint'),
                          (4, 'R4', 'hint'), (5, 'R3', 'hint')])

    def test_longest_wait_first_inside_a_tier(self):
        accounts, events = self.quotes(3)
        events[0]['occurred_at'] = D(60)
        self.assertEqual([c.account_id for c in run(accounts, events).released], [1, 3, 2])

    def test_waiting_customers_may_overflow_the_cap_up_to_eight(self):
        ids = range(1, 11)
        events = [sela_reply(i, 3 + i) for i in ids]
        accounts = accounts_for(*ids)
        released = run(accounts, events).released
        self.assertEqual(len(released), 8)
        self.assertTrue(all(c.rule == 'R1' for c in released))
        self.assertEqual(len(run(accounts, events, launch_date=TODAY).released), 3)

    def test_overflow_never_lets_other_rules_past_the_cap(self):
        accounts_q, quotes = self.quotes(5, start=1)
        waiting = [sela_reply(10, 5), sela_reply(11, 6)]
        released = run(accounts_q + accounts_for(10, 11), quotes + waiting).released
        self.assertEqual(len(released), 5)
        self.assertEqual([c.rule for c in released[:2]], ['R1', 'R1'])

    def test_stats_report_what_fired_and_what_was_left_out(self):
        accounts = accounts_for(1, 2, 3, 4) + [{'account_id': 5, 'deleted_at': D(1)}]
        events = [sela_reply(1, 5), our_quote(2, 20), ev(3, 2, '刚聊过', 'two_way'), sela_reply(5, 5)]
        result = run(accounts, events, open_tasks={2})
        self.assertEqual(result.excluded, {'has_open_task': 1, 'no_rule_matched': 1,
                                           'no_real_events': 1, 'archived': 1})
        self.assertEqual(result.fired, {'R1': 1})

    def test_compute_candidates_is_the_released_list(self):
        events = [sela_reply(1, 5)]
        listed = fc.compute_candidates(accounts_for(1), events, (), TODAY, None, 'kelley', human_owned_ids={1})
        self.assertEqual([c.owner for c in listed], ['kelley'])
        self.assertEqual(listed[0].as_dict()['rule_name'], 'customer-waiting')

    def test_overrides_merge_without_touching_the_defaults(self):
        merged = fc.merge_params({'r2': {'min_days': 7}, 'limits': {'daily_cap': 2}})
        self.assertEqual((merged['r2']['min_days'], merged['r2']['max_days'], merged['limits']['daily_cap']), (7, 56, 2))
        self.assertEqual(fc.DEFAULT_PARAMS['r2']['min_days'], 14)


def fresh(accounts, events, **kwargs):
    """Scan with the shipped (tight) defaults."""
    kwargs.setdefault('human_owned_ids', {a['account_id'] for a in accounts})
    return fc.scan(accounts, events, (), TODAY, owner='amy', **kwargs)


class PreciseDefaultsTest(unittest.TestCase):
    def test_stale_quote_is_not_suggested(self):
        # 9 weeks of silence: re-prospecting, not a follow-up
        self.assertEqual(fresh(accounts_for(1), [our_quote(1, 63)]).released, [])
        self.assertEqual(len(fresh(accounts_for(1), [our_quote(1, 40)]).released), 1)

    def test_at_most_two_a_day_even_with_many_waiting(self):
        events = [sela_reply(i, 5) for i in range(1, 9)]
        self.assertEqual(len(fresh(accounts_for(*range(1, 9)), events).released), 2)

    def test_customer_waiting_and_own_date_come_before_quotes(self):
        events = [our_quote(1, 30), our_quote(2, 35), sela_reply(3, 4)]
        first = fresh(accounts_for(1, 2, 3), events).released[0]
        self.assertEqual(first.rule, 'R1')

    def test_dormant_client_needs_recent_contact_and_four_contact_days(self):
        deal = [ev(1, 200, '客户下单了，合同已签', 'two_way'), ev(1, 190, '跟进', 'two_way'),
                ev(1, 180, '电话沟通', 'two_way'), ev(1, 170, '发资料', 'two_way')]
        self.assertEqual(fresh(accounts_for(1), deal).released, [])           # last contact 170 days ago
        recent = [ev(1, 140, '客户下单了，合同已签', 'two_way'), ev(1, 130, '跟进', 'two_way'),
                  ev(1, 120, '电话沟通', 'two_way'), ev(1, 110, '发资料', 'two_way')]
        self.assertEqual([c.rule for c in fresh(accounts_for(1), recent).released], ['R3'])
        self.assertEqual(fresh(accounts_for(1), recent[1:]).released, [])     # only three contact days

    def test_reason_card_carries_last_topic_price_and_speaker(self):
        c = only(fresh(accounts_for(1), [our_quote(1, 30, '已发报价 单价 3.2 USD/kg，含运费')]))
        ctx = c.facts['context']
        self.assertEqual(ctx['last_speaker'], '我们')
        self.assertIn('报价', ctx['last_topic'])
        self.assertIn('3.2 USD/kg', ctx['deal_detail'])

    def test_customer_last_words_are_quoted_for_r1(self):
        c = only(fresh(accounts_for(1), [sela_reply(1, 5, 'Please send the lead time for 3mm sheets.')]))
        self.assertEqual(c.facts['context']['last_speaker'], '客户')
        self.assertIn('lead time', c.facts['context']['last_words'])

    def test_customer_who_declined_is_not_chased(self):
        events = [sela_reply(1, 5, 'Thanks, but we are not interested in overseas suppliers.'),
                  sela_reply(2, 5, '客户回复拒绝了亚克力供应的提议，表示不感兴趣')]
        self.assertEqual(fresh(accounts_for(1, 2), events).released, [])

    def test_no_price_means_no_price_line_never_invented(self):
        c = only(fresh(accounts_for(1), [our_quote(1, 30, '报价已发给客户')]))
        self.assertNotIn('deal_detail', c.facts['context'])

    def test_incomplete_reason_is_not_shown(self):
        # no words on the record at all: nothing to say about where the talk stood
        events = [ev(1, 30, '', 'outbound', event_type='报价')]
        result = fresh(accounts_for(1), events)
        self.assertEqual(result.released, [])

    def test_complete_reason_guard_can_be_switched_off(self):
        events = [ev(1, 30, '', 'outbound', event_type='报价')]
        result = fresh(accounts_for(1), events, params={'guards': {'complete_reason': False}})
        self.assertEqual(len(result.released), 1)


if __name__ == '__main__':
    unittest.main()
