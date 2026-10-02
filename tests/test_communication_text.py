import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from communication_text import looks_like_quoted_email, strip_quoted_email_text


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


if __name__ == '__main__':
    unittest.main()
