"""Release infrastructure authority regression.

A production commit must not be published by a different version of the release
mechanism depending on which checkout invoked it.  These tests pin the rule that
``release-commit.sh`` treats its own ``deploy/cloud`` infrastructure (and
``tools/release_baseline.py``) as authoritative only when it matches the latest
``origin/main``; otherwise it re-executes the real logic from a clean worktree at
``origin/main``.  Task code is not required to equal main -- only the release
mechanism is.

Everything runs offline in a temporary Git repository; no test touches ECS.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_COMMIT = ROOT / "deploy" / "cloud" / "release-commit.sh"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


class ReleaseDriverAuthorityContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = read(RELEASE_COMMIT)

    def test_guard_is_wired(self) -> None:
        for token in (
            "release_driver_is_behind",
            "TRADE_OS_RELEASE_DRIVER_MAIN",
            "trosa-release-driver.",
            "worktree add --detach",
            "deploy/cloud tools/release_baseline.py",
            "fail closed",
        ):
            self.assertIn(token, self.script)

    def test_guard_runs_before_the_release_lock(self) -> None:
        guard = self.script.index("release_driver_is_behind")
        lock = self.script.index("trosa_lock_acquire", guard)
        self.assertLess(guard, lock)

    def test_auto_publish_delegates_to_release_commit(self) -> None:
        auto = read(ROOT / "deploy" / "cloud" / "auto-publish.sh")
        self.assertIn("release-commit.sh", auto)


class ReleaseDriverBehindHarnessTests(unittest.TestCase):
    """Exercise the real ``release_driver_is_behind`` predicate offline."""

    HARNESS = r"""
set -euo pipefail
SOURCE_DIR="$1"
BASE="$2"
__FUNCTION__
if release_driver_is_behind "$BASE"; then echo BEHIND; else echo CURRENT; fi
"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "Tester")
        (self.repo / "deploy" / "cloud").mkdir(parents=True)
        (self.repo / "deploy" / "cloud" / "release-commit.sh").write_text("v1\n", encoding="utf-8")
        (self.repo / "app.py").write_text("app\n", encoding="utf-8")
        self.base = commit(self.repo, "base")
        # A task-only change (not release infrastructure).
        (self.repo / "app.py").write_text("app2\n", encoding="utf-8")
        self.non_infra = commit(self.repo, "task code")
        # A change to the release infrastructure itself.
        (self.repo / "deploy" / "cloud" / "release-commit.sh").write_text("v2\n", encoding="utf-8")
        self.infra = commit(self.repo, "release tooling")

        script = read(RELEASE_COMMIT)
        start = script.index("release_driver_is_behind() {")
        end = script.index("\n}\n", start) + len("\n}\n")
        self.program = self.HARNESS.replace("__FUNCTION__", script[start:end])

    def _checkout(self, sha: str) -> None:
        git(self.repo, "checkout", "-q", sha)

    def _run(self, base: str) -> str:
        proc = subprocess.run(
            ["bash", "-c", self.program, "_", str(self.repo), base],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_current_tree_matches_base(self) -> None:
        self._checkout(self.base)
        self.assertEqual(self._run(self.base), "CURRENT")

    def test_tree_behind_origin_main_is_behind(self) -> None:
        self._checkout(self.base)
        self.assertEqual(self._run(self.infra), "BEHIND")

    def test_task_ahead_but_infra_changed_is_behind(self) -> None:
        self._checkout(self.infra)
        self.assertEqual(self._run(self.base), "BEHIND")

    def test_task_ahead_without_infra_change_is_current(self) -> None:
        self._checkout(self.non_infra)
        self.assertEqual(self._run(self.base), "CURRENT")


if __name__ == "__main__":
    unittest.main()
