"""Session lease: one task worktree belongs to one agent session at a time.

Two sessions writing the same worktree end up either sweeping each other's
uncommitted files into a commit or stopping to ask a human.  The lease makes
the second session fail at ``guard`` (before it writes) and at ``pre-commit``
(last line of defence), with a ready-made fork command.

Runs offline in a temporary repository; no test touches ECS.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "deploy" / "cloud"
HELD = 42


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc


class WorktreeLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.repo = base / "repo"
        self.worktrees = base / "worktrees"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "Tester")
        (self.repo / "app" / "static").mkdir(parents=True)
        (self.repo / "app" / "static" / "app.js").write_text("\n", encoding="utf-8")
        cloud = self.repo / "deploy" / "cloud"
        (cloud / "git-hooks").mkdir(parents=True)
        for path in CLOUD.glob("*.sh"):
            target = cloud / path.name
            target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            target.chmod(0o755)
        for name in ("pre-commit", "commit-msg"):
            (cloud / "git-hooks" / name).write_text(
                (CLOUD / "git-hooks" / name).read_text(encoding="utf-8"), encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")
        self.script = str(cloud / "agent-worktree.sh")
        self.create = self._run("A", "create", "--task", "t1", "--owner", "x",
                                "--goal", "g", "--scope", "s", "--no-reserve-migration",
                                cwd=self.repo)
        self.assertEqual(self.create.returncode, 0, self.create.stderr)
        self.wt = self.worktrees / "t1"

    def _env(self, session, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("TRADE_OS_", "CLAUDE_"))}
        env.update(TRADE_OS_AGENT_ROLE="dev", TRADE_OS_WORKTREE_ROOT=str(self.worktrees),
                   CLAUDE_PID=str(os.getpid()))
        if session:
            env["TRADE_OS_AGENT_SESSION"] = session
        env.update(extra)
        return env

    def _run(self, session, *args, cwd=None, **extra):
        return subprocess.run(["bash", self.script, *args], capture_output=True, text=True,
                              env=self._env(session, **extra), cwd=str(cwd or self.wt))

    def _commit(self, session, name):
        (self.wt / name).write_text(name, encoding="utf-8")
        git(self.wt, "add", name)
        return subprocess.run(["git", "-C", str(self.wt), "commit", "-qm", f"[t1] {name}"],
                              capture_output=True, text=True, env=self._env(session))

    def test_second_session_is_refused_with_fork_command(self):
        self.assertEqual(self._run("A", "guard").returncode, 0)
        proc = self._run("B", "guard")
        self.assertEqual(proc.returncode, HELD, proc.stdout + proc.stderr)
        self.assertIn("create --task t1-b --base agent/t1", proc.stderr)

    def test_owner_reentry_and_other_session_commit_is_blocked(self):
        self.assertEqual(self._run("A", "guard").returncode, 0)
        self.assertEqual(self._run("A", "guard").returncode, 0)
        blocked = self._commit("B", "b.txt")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("另一个 Agent 会话", blocked.stderr)
        self.assertEqual(self._commit("A", "a.txt").returncode, 0)

    def test_takeover_transfers_ownership(self):
        self._run("A", "guard")
        self.assertEqual(self._run("B", "guard", "--takeover").returncode, 0)
        self.assertEqual(self._run("A", "lease", "check", "--quiet").returncode, HELD)

    def test_dead_holder_does_not_block(self):
        self._run("A", "guard")
        lease = self.repo / ".git" / "trosa-tasks" / "t1.lease"
        text = "".join("pid=999999\n" if line.startswith("pid=") else line + "\n"
                       for line in lease.read_text(encoding="utf-8").splitlines())
        lease.write_text(text, encoding="utf-8")
        self.assertEqual(self._run("B", "guard").returncode, 0)

    def test_no_session_id_or_override_disables_lease(self):
        self._run("A", "guard")
        self.assertEqual(self._run(None, "guard").returncode, 0)
        self.assertEqual(
            self._run("B", "guard", TRADE_OS_ALLOW_SHARED_WORKTREE="1").returncode, 0)

    def test_release_and_remove_clear_lease(self):
        self._run("A", "guard")
        lease = self.repo / ".git" / "trosa-tasks" / "t1.lease"
        self.assertTrue(lease.exists())
        self.assertEqual(self._run("A", "lease", "release").returncode, 0)
        self.assertFalse(lease.exists())
        self.assertEqual(self._run("B", "guard").returncode, 0)


if __name__ == "__main__":
    unittest.main()
