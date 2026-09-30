import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "check_daylight_contrast.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("check_daylight_contrast", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DaylightContrastTest(unittest.TestCase):
    def test_daylight_tokens_and_usage_meet_contrast(self):
        result = subprocess.run(
            [sys.executable, str(TOOL), "--failures"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_paper_scan_flags_a_faint_informational_label(self):
        tool = _load_tool()
        css = "#trosa .modal .form-label { color: var(--dl-faint); }\n"
        problems = tool.scan_paper_faint(css)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("--dl-faint", problems[0])

    def test_paper_scan_exempts_disabled_placeholder_and_decoration(self):
        tool = _load_tool()
        css = (
            "#trosa .modal .form-control:disabled { color: var(--dl-faint); }\n"
            "#trosa .modal .form-control::placeholder { color: var(--dl-faint); }\n"
            "#trosa #customerEditModal .cw-prow.is-done b { color: var(--dl-faint); }\n"
            "#trosa .room-page { color: var(--dl-faint); }\n"
        )
        self.assertEqual(tool.scan_paper_faint(css), [])

    def test_gate_reads_v5_not_only_v3(self):
        """A paper faint rule in v5 must fail the gate (no more false green)."""
        tool = _load_tool()
        original_v5 = tool.CSS_V5
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "visual-v5.css"
            bad.write_text("#trosa .modal .form-label { color: var(--dl-faint); }\n", encoding="utf-8")
            tool.CSS_V5 = bad
            argv = sys.argv
            sys.argv = [str(TOOL), "--failures"]
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    rc = tool.main()
            finally:
                sys.argv = argv
                tool.CSS_V5 = original_v5
        self.assertEqual(rc, 1, out.getvalue())
        self.assertIn("usage FAIL", out.getvalue())
        self.assertIn("--dl-faint", out.getvalue())


if __name__ == "__main__":
    unittest.main()
