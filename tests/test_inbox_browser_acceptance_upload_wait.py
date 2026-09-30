"""Static guardrails for the Inbox specialist acceptance upload assertion.

The upload loop used to wait on rendered DOM nodes (``.inbox-upload-status`` /
``.inbox-analysis-result``) with an arbitrary 30s timeout.  That races the app's
asynchronous ``#inboxList`` reload: a supported/not_supported conclusion resolves
its investigation question server-side, so a reload that lands afterwards can
drop the open card and its rendered result mid-wait.  The loop now waits on the
app's own durable state instead; these assertions keep that fix (and the business
assertions it must not relax) in place.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools' / 'inbox_browser_acceptance.js'


class InboxAcceptanceUploadWaitTest(unittest.TestCase):
    def setUp(self):
        self.source = SCRIPT.read_text(encoding='utf-8')

    def test_fixed_status_timeout_is_gone(self):
        # The old fixed-timeout DOM wait must not come back.
        self.assertNotIn('timeout:30000', self.source)
        self.assertNotIn('getByText(/证据已分析|失败/)', self.source)

    def test_uses_app_state_condition_wait(self):
        # The wait resolves against the app's own state, which survives a rerender.
        self.assertIn('window.inboxState', self.source)
        self.assertIn('analysisStates', self.source)
        self.assertIn('uploadStates', self.source)
        self.assertIn('readInboxUpload(questionId)', self.source)
        self.assertIn('waitForTimeout(100)', self.source)
        # The question id is read from the card's data attribute before uploading.
        self.assertIn("getAttribute('data-inbox-id')", self.source)

    def test_timeout_is_a_bounded_constant_with_diagnostics(self):
        self.assertRegex(self.source, r'INBOX_ANALYSIS_TIMEOUT_MS\s*=\s*\d{4,5}')
        self.assertIn('did not settle within', self.source)
        self.assertIn('statusBefore', self.source)

    def test_business_assertions_preserved(self):
        for needle in (
            'data-analysis-status',
            ' expected status ',
            'citation source missing',
            'citation location missing',
        ):
            self.assertIn(needle, self.source)
        self.assertIn("'(^|[^0-9])'", self.source)
        self.assertIn('证据已分析', self.source)
        # Every fixture upload stays covered, with its expected conclusion.
        for needle in (
            'supported.csv',
            'not_supported.xlsx',
            'insufficient.pdf',
            'image_without_ocr.png',
            'broken.pdf',
        ):
            self.assertIn(needle, self.source)
        for status in ("'supported'", "'not_supported'", "'insufficient'", "'analysis_failed'"):
            self.assertIn(status, self.source)


if __name__ == '__main__':
    unittest.main()