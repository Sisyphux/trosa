"""Regression checks for the commit-driven local release boundary.

These checks intentionally avoid ECS, GitHub writes, and a live database. The
full candidate gate is exercised by ``release-test.sh``; this module protects
the part that is easiest to accidentally weaken while changing the scripts:
the caller supplies commits, the release candidate is built in a clean
worktree, and legacy file-list staging is no longer an entrypoint.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class CommitReleaseEntrypointTests(unittest.TestCase):
    def test_auto_publish_delegates_only_to_commit_entrypoint(self):
        auto = read("deploy/cloud/auto-publish.sh")
        self.assertIn('release-commit.sh', auto)
        self.assertNotIn('git add', auto)
        self.assertNotIn('git commit', auto)
        self.assertNotIn('REQUESTED_FILES', auto)

    def test_commit_entrypoint_exposes_the_small_interface(self):
        script = read("deploy/cloud/release-commit.sh")
        for token in ("--commit", "--branch", "--release-id", "--dry-run",
                      "git worktree add --detach", "git cherry-pick",
                      "git push", "worktree remove"):
            self.assertIn(token, script)
        self.assertIn("RELEASE_COMMIT_ALREADY_PRESENT", script)
        self.assertIn("RELEASE_DRY_RUN_OK", script)

    def test_release_candidate_rejects_runtime_and_secret_paths(self):
        script = read("deploy/cloud/release-commit.sh")
        for token in ("data/*", "*.db", "*.sqlite", ".env.*",
                      "deploy/cloud/workbench.env", "deploy/macos/cloudflared.yml",
                      "node_modules"):
            self.assertIn(token, script)
        self.assertIn('发布 commit 包含禁止路径', script)

    def test_task_publish_requires_a_fully_clean_worktree(self):
        script = read("deploy/cloud/agent-worktree.sh")
        self.assertIn('git -C "$repo" status --porcelain --untracked-files=all', script)
        self.assertIn('require_clean "$wt"', script)
        self.assertIn('auto-publish.sh" --branch "$branch"', script)
        self.assertNotIn('auto-publish.sh" --staged', script)

    def test_shared_gate_is_isolated_and_used_by_tasks(self):
        gate = read("deploy/cloud/release-test.sh")
        for token in ("CRM_ENV=development", "TRADE_OS_DEV_SQLITE=1",
                      "TRADE_OS_DATA_BACKEND=sqlite", 'CRM_DB_PATH="$TEST_DATA_DIR"',
                      "unittest discover", "npm test"):
            self.assertIn(token, gate)
        task_script = read("deploy/cloud/agent-worktree.sh")
        self.assertIn("release-test.sh", task_script)

    def test_legacy_file_list_is_not_accepted(self):
        proc = subprocess.run(
            ["bash", "deploy/cloud/auto-publish.sh", "--", "app.py"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("commit 或分支", proc.stderr)

    def test_all_commit_entry_scripts_are_shell_valid(self):
        for name in ("auto-publish.sh", "release-commit.sh", "release-test.sh",
                     "agent-worktree.sh"):
            proc = subprocess.run(
                ["bash", "-n", f"deploy/cloud/{name}"],
                cwd=str(ROOT), capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(proc.returncode, 0, f"{name}: {proc.stderr}")


if __name__ == "__main__":
    unittest.main()
