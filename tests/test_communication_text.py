import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from communication_text import (
    clean_legacy_sela_content,
    fallback_message_fact,
    looks_like_raw_message,
    looks_like_legacy_sela_content,
    looks_like_quoted_email,
    strip_quoted_email_text,
)


PROTON_SAMPLE = """Fri, 11 Sep 2026 14:34:06 +0000 · acryplex &lt;acryplex@proton.me&gt;
Name below Thanks MOE AFTAB SALES, Acrylic / Plexiglas 647 774 7623 | ACRYLICPLEX@PROTON.ME [622 MAGNETIC DR,TORONTO,ONTARIO,M3J 3J2](https://maps.google.com/?q=622%20MAGNETIC%20DR,TORONTO,ONTARIO,M3J%203J2)
Sent from [Proton Mail](https://proton.me/mail/home) for Android.
-------- Original Message --------
On Friday, 09/11/26 at 10:28 Hamid Luo &lt;hamid.luo.pronamax@gmail.com&gt; wrote:
&gt; Good morning Boss Moe,
&gt; Great, we will arrange the samples.
&gt; &gt; Acrylic / Plexiglas &gt; 622 Magnetic Dr, Toronto, Ontario M3J 3J2
acryplex &lt;acryplex@proton.me&gt;于2026年9月9日 周三23:02写道：
&gt;&gt; good morming we need samples"""

# Gmail/Proton plain-text parts often collapse the whole thread onto one line;
# this mirrors a real stored record.
INLINE_PROTON_SAMPLE = (
    'Fri, 11 Sep 2026 14:34:06 +0000 · acryplex &lt;acryplex@proton.me&gt;\n'
    'Name below Thanks MOE AFTAB SALES, Acrylic / Plexiglas 647 774 7623 | '
    'ACRYLICPLEX@PROTON.ME [622 MAGNETIC DR,TORONTO,ONTARIO,M3J 3J2]'
    '(https://maps.google.com/?q=622%20MAGNETIC%20DR,TORONTO,ONTARIO,M3J%203J2) '
    'Sent from [Proton Mail](https://proton.me/mail/home) for Android. '
    '-------- Original Message -------- On Friday, 09/11/26 at 10:28 Hamid Luo '
    '&lt;hamid.luo.pronamax@gmail.com&gt; wrote: &gt; Good morning Boss Moe, '
    '&gt; &gt; Great, we will arrange the samples. '
    'acryplex &lt;acryplex@proton.me&gt;于2026年9月9日 周三23:02写道： '
    '&gt;&gt; good morming we need samples'
)


class LooksLikeQuotedTest(unittest.TestCase):
    def test_detects_quoted_mail(self):
        self.assertTrue(looks_like_quoted_email(PROTON_SAMPLE))

    def test_clean_body_is_not_flagged(self):
        self.assertFalse(looks_like_quoted_email('客户确认了报价，下周安排样品。'))
        self.assertFalse(looks_like_quoted_email(''))
        self.assertFalse(looks_like_quoted_email(None))


class StripQuotedEmailTest(unittest.TestCase):
    def test_strips_proton_quote_history_and_signature(self):
        cleaned = strip_quoted_email_text(PROTON_SAMPLE)
        self.assertIn('Name below Thanks MOE', cleaned)
        self.assertNotIn('Sent from [Proton Mail]', cleaned)
        self.assertNotIn('Original Message', cleaned)
        self.assertNotIn('wrote:', cleaned)
        self.assertNotIn('写道', cleaned)
        self.assertFalse(any(line.lstrip().startswith(('>', '&gt;'))
                             for line in cleaned.splitlines()))

    def test_idempotent(self):
        cleaned = strip_quoted_email_text(PROTON_SAMPLE)
        self.assertEqual(strip_quoted_email_text(cleaned), cleaned)

    def test_clean_text_is_unchanged(self):
        body = 'Good morning,\n\nWe will arrange the samples next week.\n\nBest regards'
        self.assertEqual(strip_quoted_email_text(body), body)

    def test_escaped_quote_marker_is_a_boundary(self):
        body = 'Current reply\n&gt; quoted older message'
        self.assertEqual(strip_quoted_email_text(body), 'Current reply')

    def test_outlook_divider_and_attribution(self):
        body = ('Thanks for the update.\n'
                '________________________________\n'
                'From: Buyer <buyer@example.com>\n'
                'Sent: Monday\n'
                'To: Owner\n'
                'Subject: Re: quote')
        self.assertEqual(strip_quoted_email_text(body), 'Thanks for the update.')

    def test_signature_delimiter(self):
        body = 'Done.\n--\nHamid\nAcme'
        self.assertEqual(strip_quoted_email_text(body), 'Done.')

    def test_never_returns_empty_for_non_empty_input(self):
        body = '> only a quote'
        self.assertEqual(strip_quoted_email_text(body), '> only a quote')
        self.assertEqual(strip_quoted_email_text('single line'), 'single line')

    def test_drops_duplicate_paragraphs(self):
        body = 'Please confirm.\n\nPlease confirm.'
        self.assertEqual(strip_quoted_email_text(body), 'Please confirm.')

    def test_consecutive_duplicate_messages_are_deduped(self):
        message = 'Good morning Boss Moe, we will arrange the samples.'
        body = message + '\n\n' + message + '\n\nOn Monday Buyer wrote:\n> old'
        self.assertEqual(strip_quoted_email_text(body), message)

    def test_inline_single_line_proton_body_is_cleaned(self):
        self.assertTrue(looks_like_quoted_email(INLINE_PROTON_SAMPLE))
        cleaned = strip_quoted_email_text(INLINE_PROTON_SAMPLE)
        self.assertIn('Name below Thanks MOE', cleaned)
        self.assertIn('ACRYLICPLEX@PROTON.ME', cleaned)
        for marker in ('Sent from [Proton Mail]', 'Original Message', 'wrote:',
                       '写道', ' &gt; ', '&gt;&gt;',
                       '&lt;hamid.luo.pronamax@gmail.com&gt;'):
            self.assertNotIn(marker, cleaned)
        self.assertEqual(strip_quoted_email_text(cleaned), cleaned)

    def test_inline_attribution_without_newline_is_a_boundary(self):
        body = 'Current reply On Monday Buyer wrote: old quoted text'
        self.assertEqual(strip_quoted_email_text(body), 'Current reply')

    def test_bare_greater_than_in_prose_is_not_a_boundary(self):
        body = 'Please review > see below for the revised numbers.'
        self.assertFalse(looks_like_quoted_email(body))
        self.assertEqual(strip_quoted_email_text(body), body)

    def test_lowercase_sent_from_prose_is_not_a_signature(self):
        body = 'The parcel was sent from Guangzhou last week.'
        self.assertFalse(looks_like_quoted_email(body))
        self.assertEqual(strip_quoted_email_text(body), body)


SELA_ENVELOPE_CURATED = (
    '[Sela Feedback ID: feedback:c0c50b3549f051b68e5b971ae2dee4ad611d1721]\n'
    '历史客户回复\n'
    '事件：INTERESTED\n'
    '时间：Tue, 25 Aug 2026 09:11:31 -0400\n'
    '2026-09-03：客户表示当前可从中国供应商获得更好的价格，但对我们产品厚度感兴趣。'
)

SELA_ENVELOPE_RULES = (
    '[Sela Feedback ID: feedback:abc]\n'
    '历史客户回复\n'
    '事件：REPLIED\n'
    '时间：2026-09-01 10:00:00 +0800\n'
    'Gmail 同步（RULES_V1）： 主题：Re: Acrylic sheet supply for Hideout Signs '
    '发件人：Hideout Signs &lt;hideoutsignsltd@gmail.com&gt; '
    'Gmail message_id：1a064e9c9410c8d8 规则意图：QUOTE_REQUEST 路由：HUMAN_REVIEW '
    '正文： We get a better price from China. Please send colour chart.'
)


class LegacySelaEnvelopeTest(unittest.TestCase):
    def test_keeps_curated_summary_and_drops_envelope(self):
        self.assertTrue(looks_like_legacy_sela_content(SELA_ENVELOPE_CURATED))
        cleaned = clean_legacy_sela_content(SELA_ENVELOPE_CURATED)
        self.assertEqual(cleaned, '2026-09-03：客户表示当前可从中国供应商获得更好的价格，但对我们产品厚度感兴趣。')
        self.assertNotIn('Sela Feedback ID', cleaned)
        self.assertNotIn('历史客户回复', cleaned)
        self.assertNotIn('事件：', cleaned)

    def test_reformats_structured_gmail_block(self):
        cleaned = clean_legacy_sela_content(SELA_ENVELOPE_RULES)
        self.assertIn('客户通过 Gmail 回复', cleaned)
        self.assertIn('主题：Re: Acrylic sheet supply for Hideout Signs', cleaned)
        self.assertIn('正文：\nWe get a better price from China.', cleaned)
        for internal in ('Sela Feedback ID', 'Gmail 同步', 'Gmail message_id', '规则意图', '路由：', '发件人'):
            self.assertNotIn(internal, cleaned)

    def test_strips_a_bare_sync_label_without_envelope(self):
        body = 'Gmail 同步（SYSTEM_FALLBACK）：Re: Acrylic sheet options for Metacrilato.eu'
        self.assertTrue(looks_like_legacy_sela_content(body))
        self.assertEqual(clean_legacy_sela_content(body),
                         'Re: Acrylic sheet options for Metacrilato.eu')

    def test_multi_line_detail_keeps_its_own_time_line(self):
        body = (
            '[Sela Feedback ID: feedback:xyz]\n'
            '历史客户回复\n'
            '事件：INTERESTED\n'
            '时间：2026-09-01 10:00:00 +0800\n'
            '客户确认了报价。\n'
            '时间：下周安排样品。'
        )
        cleaned = clean_legacy_sela_content(body)
        self.assertIn('客户确认了报价。', cleaned)
        self.assertIn('时间：下周安排样品。', cleaned)
        self.assertNotIn('2026-09-01 10:00:00', cleaned)
        self.assertEqual(clean_legacy_sela_content(cleaned), cleaned)

    def test_idempotent_and_never_empty(self):
        for sample in (SELA_ENVELOPE_CURATED, SELA_ENVELOPE_RULES):
            cleaned = clean_legacy_sela_content(sample)
            self.assertEqual(clean_legacy_sela_content(cleaned), cleaned)
            self.assertTrue(cleaned.strip())
        self.assertEqual(clean_legacy_sela_content('历史 sela 外联'), '历史 sela 外联')

    def test_clean_and_plain_bodies_are_untouched(self):
        body = '客户确认了报价，下周安排样品。'
        self.assertFalse(looks_like_legacy_sela_content(body))
        self.assertEqual(clean_legacy_sela_content(body), body)

    def test_quoted_cleaner_also_drops_the_envelope(self):
        self.assertTrue(looks_like_quoted_email(SELA_ENVELOPE_CURATED))
        cleaned = strip_quoted_email_text(SELA_ENVELOPE_CURATED)
        self.assertEqual(strip_quoted_email_text(cleaned), cleaned)
        self.assertNotIn('Sela Feedback ID', cleaned)


class CompatibilityAliasesTest(unittest.TestCase):
    def test_read_aliases_clean_legacy_content_and_reply(self):
        import trosa_domain

        items = [
            {'kind': 'communication', 'occurred_on': '2026-09-09', 'content': SELA_ENVELOPE_CURATED,
             'result': 'INTERESTED', 'next_plan': '', 'delivery_status': ''},
            {'kind': 'email', 'occurred_on': '2026-08-25', 'content': '历史 sela 外联',
             'result': SELA_ENVELOPE_RULES, 'delivery_status': 'replied'},
        ]
        trosa_domain._add_compatibility_aliases(items)
        self.assertNotIn('Sela Feedback ID', items[0]['content'])
        self.assertNotIn('Gmail 同步', items[0]['content'])
        self.assertNotIn('Sela Feedback ID', items[1]['result'])
        self.assertEqual(items[1]['subject'], '历史 sela 外联')
        self.assertEqual(items[1]['reply_status'], 'replied')


class RawMessageGateTest(unittest.TestCase):
    def test_captured_mail_is_raw_but_a_human_summary_is_not(self):
        self.assertTrue(looks_like_raw_message(PROTON_SAMPLE))
        self.assertTrue(looks_like_raw_message('客户通过 Gmail 回复\n主题：Re: Quote\n正文：\nhi'))
        self.assertFalse(looks_like_raw_message('客户想先看规格书，并询问最小起订量。'))
        self.assertFalse(looks_like_raw_message('电话沟通，对方下周回复 john@x.com'))

    def test_fallback_fact_never_contains_the_body(self):
        text = '客户通过 Gmail 回复\n主题：Re: Quote\n发件人：a@b.com\n正文：\nsecret body'
        self.assertEqual(fallback_message_fact(text, 'inbound'), '客户回复了邮件：Quote')
        self.assertEqual(fallback_message_fact('外联邮件退信\n主题：Hi', 'outbound', 'outreach_bounced'), '外联邮件退信：Hi')


if __name__ == '__main__':
    unittest.main()
