import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "check_daylight_contrast.py"


class DaylightContrastTest(unittest.TestCase):
    def test_daylight_tokens_and_usage_meet_contrast(self):
        result = subprocess.run(
            [sys.executable, str(TOOL), "--failures"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
