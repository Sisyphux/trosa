"""Task graduation, entry isolation and migration-collision regressions.

These close the three parallel-development loops that reservation alone did not:

* a dev/review session cannot commit on the integration branch (shared
  ``pre-commit`` guard installed by ``agent-worktree.sh``);
* a task's new migrations are renumbered off any number the latest main,
  another worktree, or another task's reservation already owns;
* a task is only publishable when its gate evidence matches the current HEAD
  and that HEAD already contains the latest main (``sync`` invalidates stale
  evidence).

Everything runs offline in temporary Git repositories; no test touches ECS.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import reconcile_migrations as rec  # noqa: E402

AGENT_WORKTREE = ROOT / "deploy" / "cloud" / "agent-worktree.sh"
HOOK = ROOT / "deploy" / "cloud" / "git-hooks" / "pre-commit"
HOOK_MSG = ROOT / "deploy" / "cloud" / "git-hooks" / "commit-msg"


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc


def init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Tester")


def commit(repo: Path, message: str = "c") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


# --------------------------------------------------------------------------- #
# 1. Entry isolation: dev/review cannot commit on the integration branch.
# --------------------------------------------------------------------------- #


class PreCommitGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        init_repo(self.repo)
        (self.repo / "file.txt").write_text("hello\n", encoding="utf-8")
        commit(self.repo, "init")
        hooks = self.repo / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / "pre-commit"
        hook.write_text(HOOK.read_text(encoding="utf-8"), encoding="utf-8")
        hook.chmod(0o755)

    def _commit(self, role):
        self._counter = getattr(self, "_counter", 0) + 1
        (self.repo / "file.txt").write_text(f"changed-{self._counter}\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        env = {k: v for k, v in os.environ.items() if not k.startswith("TRADE_OS_")}
        if role is not None:
            env["TRADE_OS_AGENT_ROLE"] = role
        return subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "attempt"],
            capture_output=True, text=True, env=env,
        )

    def test_dev_cannot_commit_on_main(self):
        proc = self._commit("dev")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("不能", proc.stderr)
        self.assertIn("create", proc.stderr)

    def test_review_cannot_commit_on_main(self):
        self.assertNotEqual(self._commit("review").returncode, 0)

    def test_release_and_unset_are_allowed(self):
        self.assertEqual(self._commit("release").returncode, 0)
        (self.repo / "file.txt").write_text("again\n", encoding="utf-8")
        self.assertEqual(self._commit(None).returncode, 0)

    def test_dev_can_commit_on_task_branch(self):
        git(self.repo, "checkout", "-q", "-b", "agent/feature")
        self.assertEqual(self._commit("dev").returncode, 0)

    def test_target_branch_is_configurable(self):
        git(self.repo, "checkout", "-q", "-b", "trunk")
        (self.repo / "file.txt").write_text("x\n", encoding="utf-8")
        env = {
            k: v for k, v in os.environ.items()
            if not k.startswith("TRADE_OS_")
        }
        env["TRADE_OS_AGENT_ROLE"] = "dev"
        env["TRADE_OS_AUTO_PUBLISH_BRANCH"] = "trunk"
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-am", "attempt"],
            capture_output=True, text=True, env=env,
        )
        self.assertNotEqual(proc.returncode, 0)


class CommitMsgGuardTests(unittest.TestCase):
    """Task-style commits cannot land on the integration branch, role or not.

    A session that forgets ``TRADE_OS_AGENT_ROLE=dev`` must still not be able to
    drop a ``[<id>]`` commit straight onto ``main``: the commit message is the
    unambiguous signal that this is task work.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        init_repo(self.repo)
        (self.repo / "f.txt").write_text("x\n", encoding="utf-8")
        commit(self.repo, "init")
        hook = self.repo / ".git" / "hooks" / "commit-msg"
        hook.write_text(HOOK_MSG.read_text(encoding="utf-8"), encoding="utf-8")
        hook.chmod(0o755)

    def _commit(self, message, **env_extra):
        (self.repo / "f.txt").write_text(f"{message}\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        env = {k: v for k, v in os.environ.items() if not k.startswith("TRADE_OS_")}
        env.update(env_extra)
        return subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", message],
            capture_output=True, text=True, env=env,
        )

    def test_task_commit_on_main_refused_without_role(self):
        proc = self._commit("[retire-sela-follow-up] do work")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("不能直接接收任务 commit", proc.stderr)

    def test_human_message_on_main_allowed(self):
        self.assertEqual(self._commit("hotfix: typo").returncode, 0)

    def test_task_commit_on_agent_branch_allowed(self):
        git(self.repo, "checkout", "-q", "-b", "agent/x")
        self.assertEqual(self._commit("[x] do work").returncode, 0)

    def test_explicit_override_allows_task_commit(self):
        self.assertEqual(
            self._commit("[x] forced", TRADE_OS_ALLOW_MAIN_COMMIT="1").returncode, 0
        )


# --------------------------------------------------------------------------- #
# 2. Migration collisions: renumber only colliding task migrations.
# --------------------------------------------------------------------------- #

class MigrationPlanTests(unittest.TestCase):
    def test_no_collision_leaves_files_alone(self):
        plan = rec.plan_renumbers(
            ["0038_alpha.sql"], ["0037_main.sql"], ["0037_main.sql", "0038_alpha.sql"], {}
        )
        self.assertEqual(plan, [])

    def test_collision_with_main_is_renumbered_to_next_free(self):
        plan = rec.plan_renumbers(
            ["0038_alpha.sql"],
            ["0037_x.sql", "0038_beta.sql"],
            ["0038_alpha.sql", "0038_beta.sql"],
            {},
        )
        self.assertEqual([(p["old"], p["new"]) for p in plan], [("0038_alpha.sql", "0039_alpha.sql")])

    def test_collision_with_another_reservation_is_renumbered(self):
        plan = rec.plan_renumbers(
            ["0038_alpha.sql"], ["0037_x.sql"], ["0038_alpha.sql"], {38: "other-task"}
        )
        self.assertEqual([(p["old"], p["new"]) for p in plan], [("0038_alpha.sql", "0039_alpha.sql")])

    def test_duplicate_within_task_keeps_first_and_renames_second(self):
        plan = rec.plan_renumbers(
            ["0038_a.sql", "0038_b.sql"], ["0037_x.sql"],
            ["0037_x.sql", "0038_a.sql", "0038_b.sql"], {},
        )
        self.assertEqual([(p["old"], p["new"]) for p in plan], [("0038_b.sql", "0039_b.sql")])

    def test_non_colliding_file_is_untouched_when_sibling_collides(self):
        plan = rec.plan_renumbers(
            ["0038_a.sql", "0040_b.sql"],
            ["0037_x.sql", "0038_c.sql"],
            ["0037_x.sql", "0038_a.sql", "0038_c.sql", "0040_b.sql"],
            {},
        )
        self.assertEqual([(p["old"], p["new"]) for p in plan], [("0038_a.sql", "0041_a.sql")])

    def test_cross_worktree_collision_detected_regardless_of_order(self):
        # The task's own file may be listed before the other worktree's file;
        # the collision must still be found.
        plan = rec.plan_renumbers(
            ["0038_task.sql"], ["0037_main.sql"],
            ["0037_main.sql", "0038_task.sql", "0038_other.sql"],
            {},
        )
        self.assertEqual(
            [(p["old"], p["new"]) for p in plan], [("0038_task.sql", "0039_task.sql")]
        )


class MigrationReconcileIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        init_repo(self.repo)
        (self.repo / "migrations").mkdir()
        (self.repo / "migrations" / "0001_base.sql").write_text("select 1;\n", encoding="utf-8")
        commit(self.repo, "init")
        git(self.repo, "checkout", "-q", "-b", "agent/task")
        (self.repo / "migrations" / "0002_task.sql").write_text("select 2;\n", encoding="utf-8")
        commit(self.repo, "task 0002")

    def _advance_main(self):
        git(self.repo, "checkout", "-q", "main")
        (self.repo / "migrations" / "0002_other.sql").write_text("select 3;\n", encoding="utf-8")
        commit(self.repo, "main 0002")
        git(self.repo, "checkout", "-q", "agent/task")

    def test_reports_collision_without_applying(self):
        self._advance_main()
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "reconcile_migrations.py"),
             "--task-dir", str(self.repo), "--target-ref", "main"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("0002_task.sql -> 0003_task.sql", proc.stdout)
        self.assertTrue((self.repo / "migrations" / "0002_task.sql").exists())

    def test_apply_renames_and_stages_the_move(self):
        self._advance_main()
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "reconcile_migrations.py"),
             "--task-dir", str(self.repo), "--target-ref", "main", "--apply"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((self.repo / "migrations" / "0002_task.sql").exists())
        self.assertTrue((self.repo / "migrations" / "0003_task.sql").exists())
        status = git(self.repo, "status", "--porcelain", "--untracked-files=all").stdout
        self.assertIn("R  migrations/0002_task.sql -> migrations/0003_task.sql", status)


# --------------------------------------------------------------------------- #
# 3. Graduation: gate the task on latest main before it can be published.
# --------------------------------------------------------------------------- #


class AgentWorktreeGraduationContractTests(unittest.TestCase):
    def setUp(self):
        self.script = AGENT_WORKTREE.read_text(encoding="utf-8")

    def test_help_lists_new_boundary_commands(self):
        proc = subprocess.run(
            ["bash", str(AGENT_WORKTREE), "--help"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for token in ("gate --task", "reconcile --task", "hooks", "--offline"):
            self.assertIn(token, proc.stdout)

    def test_guard_helpers_are_wired(self):
        for token in (
            "install_git_hooks",
            "require_main_workspace",
            "reconcile_task_migrations",
            "check_task_ready",
            "git-hooks",
            "commit-msg",
            'verify_result=stale',
        ):
            self.assertIn(token, self.script)

    def test_sync_invalidates_evidence_after_rebase(self):
        sync = self.script[self.script.index("cmd_sync()"):self.script.index("cmd_publish()")]
        self.assertIn("verify_result=stale", sync)
        self.assertIn("reconcile_task_migrations", sync)
        self.assertIn("rebase", sync)

    def test_publish_requires_ready_task(self):
        publish = self.script[self.script.index("cmd_publish()"):self.script.index("cmd_status()")]
        self.assertIn("check_task_ready", publish)
        self.assertIn("reconcile_task_migrations", publish)
        # Role guard stays the first gate, before any readiness check.
        self.assertLess(
            publish.index("trosa_require_release_role"),
            publish.index("check_task_ready"),
        )

    def test_full_gate_requires_latest_base_but_quick_does_not(self):
        test_fn = self.script[self.script.index("cmd_test()"):self.script.index("cmd_evidence()")]
        self.assertIn('if [[ "$quick" != 1 ]]', test_fn)
        self.assertIn("head_contains", test_fn)


class ReleaseBoundaryReadinessTests(unittest.TestCase):
    """``release-commit.sh --branch agent/<id>`` must refuse a non-ready task.

    The task gate in ``agent-worktree.sh publish`` can be bypassed by calling the
    release entrypoint directly with a task branch, so the same readiness rule is
    enforced at the release boundary.  The function is exercised in isolation,
    offline, with a real temporary Git repository.
    """

    HARNESS = r"""
set -euo pipefail
export LC_ALL=C
fail() { printf '发布被拒绝：%s\n' "$*" >&2; exit 1; }
GIT_COMMON_DIR="__COMMON__"
BASE_SHA="$1"
TARGET_BRANCH=main
DRY_RUN="$4"
__FUNCTION__
enforce_agent_branch_ready "$2" "$3"
echo ALLOW
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        init_repo(self.repo)
        (self.repo / "f.txt").write_text("base\n", encoding="utf-8")
        self.base = commit(self.repo, "init")
        git(self.repo, "checkout", "-q", "-b", "agent/task")
        (self.repo / "f.txt").write_text("task\n", encoding="utf-8")
        self.tip = commit(self.repo, "task work")
        self.meta_dir = self.repo / ".git" / "trosa-tasks"
        self.meta_dir.mkdir()
        self._write_meta(status="active", verify="ok", verified=self.tip)

    def _write_meta(self, status, verify, verified):
        (self.meta_dir / "task.json").write_text(
            json.dumps({
                "task": "task", "status": status,
                "verify_result": verify, "verified_commit": verified,
            }),
            encoding="utf-8",
        )

    def _run(self, base=None, tip=None, dry_run=0):
        script = (ROOT / "deploy" / "cloud" / "release-commit.sh").read_text(encoding="utf-8")
        start = script.index("enforce_agent_branch_ready() {")
        end = script.index("\n}\n", start) + len("\n}\n")
        program = (
            self.HARNESS
            .replace("__COMMON__", str(self.repo / ".git"))
            .replace("__FUNCTION__", script[start:end])
        )
        return subprocess.run(
            ["bash", "-c", program, "_", base or self.base, "agent/task",
             tip or self.tip, str(dry_run)],
            capture_output=True, text=True, cwd=str(self.repo),
        )

    def test_ready_task_allows(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ALLOW", proc.stdout)

    def test_missing_meta_is_refused(self):
        (self.meta_dir / "task.json").unlink()
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("缺少完成证据", proc.stderr)

    def test_failed_gate_is_refused(self):
        self._write_meta(status="active", verify="failed", verified=self.tip)
        self.assertNotEqual(self._run().returncode, 0)

    def test_tip_not_containing_base_is_refused(self):
        # main moves ahead; the task tip no longer contains it.
        git(self.repo, "checkout", "-q", "main")
        (self.repo / "new.txt").write_text("move\n", encoding="utf-8")
        new_base = commit(self.repo, "main moves")
        git(self.repo, "checkout", "-q", "agent/task")
        proc = self._run(base=new_base)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("未基于最新", proc.stderr)

    def test_dry_run_does_not_enforce(self):
        self._write_meta(status="active", verify="", verified="")
        proc = self._run(dry_run=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class GateIntegrationTests(unittest.TestCase):
    """Exercise the real gate command against a temporary two-worktree repo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = base / "repo"
        self.worktrees = base / "worktrees"
        init_repo(self.repo)
        (self.repo / "README.md").write_text("repo\n", encoding="utf-8")
        commit(self.repo, "init")
        # Ship the workflow scripts into the temp repo so the entrypoint runs.
        cloud = self.repo / "deploy" / "cloud"
        cloud.mkdir(parents=True)
        for name in ("agent-worktree.sh", "release-env.sh", "lib-release-lock.sh"):
            (cloud / name).write_text(
                (ROOT / "deploy" / "cloud" / name).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        hooks = cloud / "git-hooks"
        hooks.mkdir()
        (hooks / "pre-commit").write_text(HOOK.read_text(encoding="utf-8"), encoding="utf-8")

        self.worktrees.mkdir()
        git(self.repo, "worktree", "add", "-q", "-b", "agent/task",
            str(self.worktrees / "task"), "main")
        self.task_repo = self.worktrees / "task"
        self.head = git(self.task_repo, "rev-parse", "HEAD").stdout.strip()
        self.meta_dir = self.repo / ".git" / "trosa-tasks"
        self.meta_dir.mkdir()
        self._write_meta(status="active", verify="ok", verified=self.head)

    def _write_meta(self, status, verify, verified):
        doc = {
            "task": "task",
            "branch": "agent/task",
            "path": str(self.task_repo),
            "status": status,
            "verify_result": verify,
            "verified_commit": verified,
        }
        (self.meta_dir / "task.json").write_text(
            json.dumps(doc), encoding="utf-8"
        )

    def _gate(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("TRADE_OS_")}
        env["TRADE_OS_AGENT_ROLE"] = "release"
        env["TRADE_OS_WORKTREE_ROOT"] = str(self.worktrees)
        return subprocess.run(
            ["bash", str(self.repo / "deploy" / "cloud" / "agent-worktree.sh"),
             "gate", "--task", "task"],
            capture_output=True, text=True, env=env, cwd=str(self.repo),
        )

    def test_ready_task_passes(self):
        proc = self._gate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ready", proc.stdout)

    def test_missing_evidence_fails(self):
        self._write_meta(status="active", verify="", verified="")
        proc = self._gate()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("没有有效门禁证据", proc.stderr)

    def test_stale_commit_evidence_fails(self):
        self._write_meta(status="active", verify="ok", verified="0" * 40)
        self.assertNotEqual(self._gate().returncode, 0)

    def test_task_behind_main_fails(self):
        git(self.repo, "checkout", "-q", "main")
        (self.repo / "new.txt").write_text("move\n", encoding="utf-8")
        commit(self.repo, "main moves")
        proc = self._gate()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("未基于最新", proc.stderr)


if __name__ == "__main__":
    unittest.main()
