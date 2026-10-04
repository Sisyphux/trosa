"""回收隔离区（``remove``）的安全护栏回归。

覆盖四类在真实回收盘点里暴露的缺陷，全部离线、在临时 Git 仓库里跑，不接触真实
``trosa-worktrees``、真实共享 stash 或真实共享任务目录：

* A 进程占用：cwd 或打开文件落在 worktree 内的进程会让 ``remove`` 默认拒绝，且
  缺少 ``lsof`` 时 fail closed；只有显式 ``--ignore-processes`` 才越过。
* B 删除前预览与确认：列出将被删除的 gitignore 内容与任务清单；非交互环境必须
  显式 ``--yes``；删除任务清单前备份到 ``trosa-tasks/removed/``，``evidence`` 仍
  能读出“已回收 + 发布结论”。
* C 分支删除语义：``--delete-branch`` 只对已发布（status=landed）或在 main 上
  能找到等价补丁（``git cherry`` 无独有提交）的分支安全删除，否则拒绝并列出独有
  提交（仅 ``--force`` 显式丢弃）。
* D 只读命令不写 hooks：``status``/``guard``/``preflight``/``list``/``evidence``/
  ``gate``/``flakes`` 不得改动共享 ``.git/hooks``；只有写命令才安装护栏。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_WORKTREE = ROOT / "deploy" / "cloud" / "agent-worktree.sh"


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


def clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TRADE_OS_")}
    env.setdefault("LC_ALL", "C")
    env.update(extra)
    return env


def copy_workflow(repo: Path) -> None:
    cloud = repo / "deploy" / "cloud"
    cloud.mkdir(parents=True, exist_ok=True)
    for name in ("agent-worktree.sh", "release-env.sh", "lib-release-lock.sh",
                 "lib-release-gate.sh"):
        (cloud / name).write_text(
            (ROOT / "deploy" / "cloud" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    hooks = cloud / "git-hooks"
    hooks.mkdir()
    for name in ("pre-commit", "commit-msg"):
        (hooks / name).write_text(
            (ROOT / "deploy" / "cloud" / "git-hooks" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )


def hooks_snapshot(hooks_dir: Path) -> dict:
    snap = {}
    if not hooks_dir.is_dir():
        return snap
    for path in sorted(hooks_dir.iterdir()):
        if path.is_file():
            snap[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snap


class RemoveFixture(unittest.TestCase):
    """临时仓库 + 一个 agent/t 隔离区 + 任务清单。"""

    task = "t"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.repo = base / "repo"
        self.worktrees = base / "worktrees"
        self.worktrees.mkdir()
        init_repo(self.repo)
        (self.repo / ".gitignore").write_text(
            ".local/\ndata/\n.venv\nnode_modules\n", encoding="utf-8"
        )
        (self.repo / "README.md").write_text("repo\n", encoding="utf-8")
        copy_workflow(self.repo)
        commit(self.repo, "init")
        git(self.repo, "worktree", "add", "-q", "-b", f"agent/{self.task}",
            str(self.worktrees / self.task), "main")
        self.wt = self.worktrees / self.task
        self.meta_dir = self.repo / ".git" / "trosa-tasks"
        self.meta_dir.mkdir()
        self._write_meta()
        (self.meta_dir / f"{self.task}.verify.log").write_text(
            "# trosa task evidence\n", encoding="utf-8"
        )

    def _write_meta(self, **fields):
        doc = {
            "task": self.task,
            "branch": f"agent/{self.task}",
            "path": str(self.wt),
            "status": "active",
        }
        doc.update(fields)
        (self.meta_dir / f"{self.task}.json").write_text(
            json.dumps(doc), encoding="utf-8"
        )

    def _script(self) -> str:
        return str(self.repo / "deploy" / "cloud" / "agent-worktree.sh")

    def _remove(self, *args, env=None, cwd=None, stdin=subprocess.DEVNULL):
        base_env = clean_env(
            TRADE_OS_AGENT_ROLE="dev",
            TRADE_OS_WORKTREE_ROOT=str(self.worktrees),
        )
        if env:
            base_env.update(env)
        return subprocess.run(
            ["bash", self._script(), "remove", "--task", self.task, *args],
            capture_output=True, text=True, env=base_env,
            cwd=str(cwd or self.repo), stdin=stdin, timeout=180,
        )

    def _evidence(self):
        return subprocess.run(
            ["bash", self._script(), "evidence", "--task", self.task],
            capture_output=True, text=True,
            env=clean_env(TRADE_OS_AGENT_ROLE="dev",
                          TRADE_OS_WORKTREE_ROOT=str(self.worktrees)),
            cwd=str(self.repo), timeout=60,
        )

    def _add_ignored_content(self):
        for rel in (".local/pgdata", "data", "node_modules"):
            (self.wt / rel).mkdir(parents=True, exist_ok=True)
        (self.wt / ".local" / "pgdata" / "base.bin").write_bytes(b"x" * 32)
        (self.wt / "data" / "app.sqlite").write_bytes(b"db")
        (self.wt / "node_modules" / "pkg.js").write_text("x\n", encoding="utf-8")


class ProcessOccupancyTests(RemoveFixture):
    def _start_holder(self):
        holder = subprocess.Popen(
            ["sleep", "300"], cwd=str(self.wt), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        def _cleanup(proc=holder):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

        self.addCleanup(_cleanup)
        time.sleep(0.5)
        return holder

    @unittest.skipUnless(shutil.which("lsof"), "lsof is required for occupancy checks")
    def test_process_holding_worktree_blocks_removal(self):
        holder = self._start_holder()
        proc = self._remove("--yes")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("仍被", proc.stdout)
        self.assertIn("sleep", proc.stdout)
        self.assertIn("pid=", proc.stdout)
        # 拒绝时不得删除任何内容，也不得自动杀进程。
        self.assertTrue(self.wt.is_dir())
        self.assertTrue((self.meta_dir / f"{self.task}.json").is_file())
        self.assertIsNone(holder.poll(), "remove must never kill the process")

    @unittest.skipUnless(shutil.which("lsof"), "lsof is required for occupancy checks")
    def test_ignore_processes_overrides_check(self):
        self._start_holder()
        proc = self._remove("--yes", "--ignore-processes")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("--ignore-processes", proc.stdout + proc.stderr)
        self.assertFalse(self.wt.exists())

    def test_missing_lsof_fails_closed(self):
        bindir = Path(self.tmp.name).resolve() / "bin-no-lsof"
        bindir.mkdir()
        for directory in ("/bin", "/usr/bin", "/sbin", "/usr/sbin"):
            src = Path(directory)
            if not src.is_dir():
                continue
            for entry in src.iterdir():
                if entry.name == "lsof":
                    continue
                dest = bindir / entry.name
                if not dest.exists():
                    try:
                        dest.symlink_to(entry)
                    except OSError:
                        pass
        proc = self._remove("--yes", env={"PATH": str(bindir)})
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("缺少 lsof", proc.stderr)
        self.assertTrue(self.wt.is_dir())
        self.assertTrue((self.meta_dir / f"{self.task}.json").is_file())


class DeletePreviewAndConfirmationTests(RemoveFixture):
    def test_non_interactive_without_yes_is_refused_with_preview(self):
        self._add_ignored_content()
        proc = self._remove()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(".local", proc.stdout)
        self.assertIn("data", proc.stdout)
        self.assertIn("任务清单", proc.stdout)
        self.assertIn("--yes", proc.stderr)
        self.assertTrue(self.wt.is_dir())
        self.assertTrue((self.meta_dir / f"{self.task}.json").is_file())

    def test_yes_deletes_ignored_content(self):
        self._add_ignored_content()
        proc = self._remove("--yes")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(".local", proc.stdout)
        self.assertFalse(self.wt.exists())


class RemovedBackupAndEvidenceTests(RemoveFixture):
    def test_backup_created_and_evidence_reads_landed(self):
        self._write_meta(status="landed", landed_commit="deadbeef",
                         landed_release="rel-20260101000000-deadbeef")
        proc = self._remove("--yes")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        removed = self.meta_dir / "removed"
        backups = sorted(removed.glob(f"{self.task}.*.json"))
        logs = sorted(removed.glob(f"{self.task}.*.verify.log"))
        self.assertEqual(len(backups), 1, list(removed.iterdir()))
        self.assertEqual(len(logs), 1)
        self.assertFalse((self.meta_dir / f"{self.task}.json").exists())
        # 备份保留了发布结论。
        backed = json.loads(backups[0].read_text(encoding="utf-8"))
        self.assertEqual(backed["status"], "landed")
        self.assertEqual(backed["landed_commit"], "deadbeef")
        self.assertEqual(backed["landed_release"], "rel-20260101000000-deadbeef")

        ev = self._evidence()
        self.assertEqual(ev.returncode, 0, ev.stdout + ev.stderr)
        self.assertIn("已回收", ev.stdout)
        self.assertIn("备份", ev.stdout)
        self.assertIn("deadbeef", ev.stdout)
        self.assertIn("已发布", ev.stdout)
        self.assertIn("rel-20260101000000-deadbeef", ev.stdout)

    def test_evidence_output_unchanged_for_active_task(self):
        ev = self._evidence()
        self.assertEqual(ev.returncode, 0, ev.stdout + ev.stderr)
        self.assertNotIn("已回收", ev.stdout)
        self.assertIn("最近一次完成证据", ev.stdout)


class BranchDeletionTests(RemoveFixture):
    def _add_unique_commit(self):
        (self.wt / "task.txt").write_text("work\n", encoding="utf-8")
        return commit(self.wt, "unique work")

    def test_delete_branch_refuses_unmerged_and_lists_unique(self):
        tip = self._add_unique_commit()
        proc = self._remove("--yes", "--delete-branch")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(tip, proc.stdout)
        self.assertIn("独有提交", proc.stdout)
        # 拒绝时什么都不删除。
        self.assertTrue(self.wt.is_dir())
        self.assertTrue((self.meta_dir / f"{self.task}.json").is_file())
        self.assertEqual(
            git(self.repo, "branch", "--list", f"agent/{self.task}").stdout.strip() != "",
            True,
        )

    def test_delete_branch_allows_landed_status(self):
        self._add_unique_commit()
        self._write_meta(status="landed", landed_commit="x", landed_release="rel-x")
        proc = self._remove("--yes", "--delete-branch")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("status=landed", proc.stdout)
        self.assertFalse(self.wt.exists())
        self.assertEqual(
            git(self.repo, "branch", "--list", f"agent/{self.task}").stdout.strip(), ""
        )

    def test_delete_branch_allows_cherry_equivalent(self):
        tip = self._add_unique_commit()
        git(self.repo, "cherry-pick", "-x", tip)
        proc = self._remove("--yes", "--delete-branch")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("等价补丁", proc.stdout)
        self.assertEqual(
            git(self.repo, "branch", "--list", f"agent/{self.task}").stdout.strip(), ""
        )

    def test_delete_branch_force_overrides_unmerged(self):
        self._add_unique_commit()
        proc = self._remove("--yes", "--force", "--delete-branch")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("--force", proc.stdout + proc.stderr)
        self.assertEqual(
            git(self.repo, "branch", "--list", f"agent/{self.task}").stdout.strip(), ""
        )


class ReadOnlyCommandsDoNotWriteHooksTests(RemoveFixture):
    READ_ONLY = [
        ("status", []),
        ("guard", []),
        ("preflight", []),
        ("list", []),
        ("evidence", ["--task", "t"]),
        ("gate", ["--task", "t"]),
        ("flakes", []),
    ]

    def _run(self, command, args):
        return subprocess.run(
            ["bash", self._script(), command, *args],
            capture_output=True, text=True,
            env=clean_env(TRADE_OS_AGENT_ROLE="dev",
                          TRADE_OS_WORKTREE_ROOT=str(self.worktrees)),
            cwd=str(self.wt), stdin=subprocess.DEVNULL, timeout=60,
        )

    def test_read_only_commands_never_write_hooks(self):
        hooks_dir = self.repo / ".git" / "hooks"
        before = hooks_snapshot(hooks_dir)
        self.assertNotIn("pre-commit", before)
        self.assertNotIn("commit-msg", before)
        for command, args in self.READ_ONLY:
            with self.subTest(command=command):
                self._run(command, args)
                self.assertEqual(hooks_snapshot(hooks_dir), before, command)
        self.assertFalse((hooks_dir / "pre-commit").exists())
        self.assertFalse((hooks_dir / "commit-msg").exists())

        # 写命令（hooks）才会安装护栏；之后只读命令不得再改动它们。
        installed = self._run("hooks", [])
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertTrue((hooks_dir / "pre-commit").exists())
        self.assertTrue((hooks_dir / "commit-msg").exists())
        after = hooks_snapshot(hooks_dir)
        for command, args in self.READ_ONLY:
            with self.subTest(command=command, after_install=True):
                self._run(command, args)
                self.assertEqual(hooks_snapshot(hooks_dir), after, command)


class RemoveContractTests(unittest.TestCase):
    def setUp(self):
        self.script = AGENT_WORKTREE.read_text(encoding="utf-8")

    def test_new_switches_and_helpers_are_wired(self):
        for token in (
            "worktree_using_pids", "report_worktree_processes",
            "print_removal_preview", "backup_task_assets",
            "branch_equivalently_merged", "TASK_REMOVED_DIR",
            "latest_removed_meta", "--ignore-processes", "--yes",
        ):
            self.assertIn(token, self.script)

    def test_hooks_installed_only_for_write_commands(self):
        self.assertIn(
            "create|adopt|start|reserve-migration|reconcile|hooks|sync|test|ship|publish|remove",
            self.script,
        )
        # install_git_hooks 只应出现一次定义 + 一次调用。
        self.assertEqual(self.script.count("install_git_hooks"), 2)


if __name__ == "__main__":
    unittest.main()
