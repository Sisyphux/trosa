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

    def _env(self, session, _no_pid=False, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("TRADE_OS_", "CLAUDE_"))}
        env.update(TRADE_OS_AGENT_ROLE="dev", TRADE_OS_WORKTREE_ROOT=str(self.worktrees))
        if not _no_pid:
            env["CLAUDE_PID"] = str(os.getpid())
        if session:
            env["TRADE_OS_AGENT_SESSION"] = session
        env.update(extra)
        return env

    def _run(self, session, *args, cwd=None, _no_pid=False, **extra):
        return subprocess.run(["bash", self.script, *args], capture_output=True, text=True,
                              env=self._env(session, _no_pid=_no_pid, **extra),
                              cwd=str(cwd or self.wt))

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

    def test_lease_survives_without_claude_pid(self):
        # Without CLAUDE_PID the lease must not record a short-lived helper pid
        # (hook/git): that pid exits immediately and the lease would look expired.
        # It falls back to the TTL heartbeat, so a second session is still refused.
        self.assertEqual(self._run("A", "guard", _no_pid=True).returncode, 0)
        lease = self.repo / ".git" / "trosa-tasks" / "t1.lease"
        self.assertNotIn("pid=", lease.read_text(encoding="utf-8"))
        blocked = self._run("B", "guard", _no_pid=True)
        self.assertEqual(blocked.returncode, HELD, blocked.stdout + blocked.stderr)
        self.assertIn("create --task t1-b", blocked.stderr)

    def test_lease_check_is_read_only_for_shared_hooks(self):
        # lease check runs before every Edit/Write; it must not (re)install the
        # shared git hooks each time.  Remove them, run lease check, and require
        # they stay removed.
        hooks_dir = self.repo / ".git" / "hooks"
        self.assertTrue((hooks_dir / "pre-commit").exists())
        for name in ("pre-commit", "commit-msg"):
            (hooks_dir / name).unlink()
        self.assertEqual(self._run("A", "lease", "check", "--quiet").returncode, 0)
        self.assertFalse((hooks_dir / "pre-commit").exists(),
                         "只读 lease check 不应写共享 pre-commit")
        self.assertFalse((hooks_dir / "commit-msg").exists(),
                         "只读 lease check 不应写共享 commit-msg")

    def _settings_hook_command(self):
        """Parse the hook command exactly as Claude Code would from settings.json."""
        import json
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        for entry in settings["hooks"]["PreToolUse"]:
            for hook in entry.get("hooks", []):
                if hook.get("type") == "command" and hook.get("command"):
                    return hook["command"]
        raise AssertionError("settings.json 里没有 PreToolUse command 钩子")

    def _pretool(self, session, path, _no_pid=False, payload=None):
        """Run the shipped hook through the real settings.json command form.

        Deliberately does not call ``bash <hook>`` directly: the defect was that the
        settings.json command silently failed (permission denied) because the hook
        was committed non-executable.  Executing the parsed command as a shell command
        covers the real invocation path.
        """
        import json
        env = self._env(session, _no_pid=_no_pid)
        env["CLAUDE_PROJECT_DIR"] = str(ROOT)
        body = payload if payload is not None else json.dumps(
            {"session_id": session or "", "cwd": str(self.wt),
             "tool_input": {"file_path": str(path)}})
        return subprocess.run(
            self._settings_hook_command(), shell=True, capture_output=True, text=True,
            env=env, input=body, cwd=str(self.wt))

    def test_pretool_hook_via_settings_command_blocks_second_session(self):
        # The shipped command must run and must not depend on an executable bit.
        hook = ROOT / "deploy" / "cloud" / "agent-lease-pretool.sh"
        self.assertIn("bash", self._settings_hook_command())
        self.assertTrue(os.access(str(hook), os.X_OK), "钩子应带可执行位")
        self.assertEqual(self._pretool("A", self.wt / "x.txt").returncode, 0)  # claims
        blocked = self._pretool("B", self.wt / "x.txt")
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        self.assertIn("create --task t1-b", blocked.stderr)
        # Outside an agent/<id> worktree the hook never interferes.
        self.assertEqual(self._pretool("B", self.repo / "README.md").returncode, 0)

    def test_pretool_hook_uses_stdin_session_id_when_env_absent(self):
        # No TRADE_OS_AGENT_SESSION / CLAUDE_CODE_SESSION_ID in the environment:
        # the hook must fall back to the session_id in the event JSON, so two
        # distinct stdin sessions still contend for the same lease.
        import json

        def body(sid):
            return json.dumps({"session_id": sid, "cwd": str(self.wt),
                               "tool_input": {"file_path": str(self.wt / "x.txt")}})

        self.assertEqual(
            self._pretool(None, self.wt / "x.txt", payload=body("sess-A")).returncode, 0)
        blocked = self._pretool(None, self.wt / "x.txt", payload=body("sess-B"))
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        self.assertIn("create --task t1-b", blocked.stderr)

    def test_pretool_hook_fails_open_on_bad_input(self):
        proc = self._pretool("B", self.wt / "x.txt", payload="not json")
        self.assertEqual(proc.returncode, 0)

    def test_release_and_remove_clear_lease(self):
        self._run("A", "guard")
        lease = self.repo / ".git" / "trosa-tasks" / "t1.lease"
        self.assertTrue(lease.exists())
        self.assertEqual(self._run("A", "lease", "release").returncode, 0)
        self.assertFalse(lease.exists())
        self.assertEqual(self._run("B", "guard").returncode, 0)


if __name__ == "__main__":
    unittest.main()
