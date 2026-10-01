"""开发期工效回归：start 兼容任意 worktree、旧警告分级、门禁按改动分档。

覆盖四件事，全部离线、在临时 Git 仓库里跑，不接触 ECS 与任何业务数据：

* ``start`` 可在桌面端会话的 ``claude/*`` worktree 里直接调用（自动定位主工作区
  建区），而 ``create``/``adopt`` 的“只在主工作区”约束保持不变；
* ``guard`` 在 ``claude/*`` worktree 里给出“直接 start”的提示，而不是让人手动切目录；
* ``preflight`` 把迁移编号冲突分成“与本任务相关 / 无关”，无关的只提示不阻断；
* ``test`` 按改动范围分档：docs/CLI/测试走 fast（不写完整证据、不登记可复用验收树），
  触及运行时代码走 full；发布前门禁（release-commit.sh）从不使用 fast。
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

import release_tier as tier_mod  # noqa: E402

AGENT_WORKTREE = ROOT / "deploy" / "cloud" / "agent-worktree.sh"
RELEASE_TEST = ROOT / "deploy" / "cloud" / "release-test.sh"
RELEASE_COMMIT = ROOT / "deploy" / "cloud" / "release-commit.sh"
TIER_TOOL = ROOT / "tools" / "release_tier.py"


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


def copy_workflow(repo: Path, with_gate_stub: str | None = None) -> None:
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
    if with_gate_stub is not None:
        (cloud / "release-test.sh").write_text(with_gate_stub, encoding="utf-8")


def copy_tier_tool(repo: Path) -> None:
    tools = repo / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "release_tier.py").write_text(
        (ROOT / "tools" / "release_tier.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# 1. 分档判据：dev_gate_tier（docs/CLI/测试 → fast，运行时代码 → full）
# --------------------------------------------------------------------------- #


class DevGateTierTests(unittest.TestCase):
    def test_docs_cli_tests_and_markdown_are_fast(self):
        self.assertEqual(tier_mod.dev_gate_tier(["docs/a.md", "tests/x.py"]), "fast")
        self.assertEqual(tier_mod.dev_gate_tier(["tools/trosa_cli.py"]), "fast")
        self.assertEqual(tier_mod.dev_gate_tier(["design/x.html"]), "fast")
        self.assertEqual(tier_mod.dev_gate_tier(["README.md", "CHANGELOG.md"]), "fast")

    def test_runtime_and_protected_paths_are_full(self):
        for paths in (
            ["app.py"],
            ["app/static/app.js"],
            ["migrations/0113_x.sql"],
            ["deploy/cloud/release-test.sh"],
            ["serve.py"],
            ["config.py"],
            ["AGENTS.md"],
            ["tools/release_tier.py"],  # T2 受保护工具，不因在 tools/ 就降级
            ["tools/reconcile_migrations.py"],
        ):
            with self.subTest(paths=paths):
                self.assertEqual(tier_mod.dev_gate_tier(paths), "full", paths)

    def test_mixed_change_stays_full(self):
        self.assertEqual(tier_mod.dev_gate_tier(["docs/a.md", "app/static/app.js"]), "full")

    def test_empty_change_is_full(self):
        # 无法判定范围时 fail closed，走完整档（也保证既有的“空改动登记验收树”行为）。
        self.assertEqual(tier_mod.dev_gate_tier([]), "full")

    def test_dev_gate_cli_prints_level_only(self):
        proc = subprocess.run(
            [sys.executable, str(TIER_TOOL), "--dev-gate", "--paths", "app.py"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "full")

    def test_regular_classification_is_unchanged(self):
        proc = subprocess.run(
            [sys.executable, str(TIER_TOOL), "--paths", "app.py"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.stdout.strip(), "T1")


# --------------------------------------------------------------------------- #
# 2. 入口兼容：claude/* worktree 里直接 start
# --------------------------------------------------------------------------- #


class StartFromClaudeWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # 解析符号链接，避免 macOS 上 /var 与 /private/var 造成路径比较不一致。
        base = Path(self.tmp.name).resolve()
        self.repo = base / "repo"
        self.worktrees = base / "worktrees"
        self.worktrees.mkdir()
        init_repo(self.repo)
        (self.repo / "README.md").write_text("repo\n", encoding="utf-8")
        (self.repo / "app" / "static").mkdir(parents=True)
        (self.repo / "app" / "static" / "app.js").write_text("console.log('x');\n", encoding="utf-8")
        copy_workflow(self.repo)
        copy_tier_tool(self.repo)
        commit(self.repo, "init")
        # 真实仓库用 .git/info/exclude 忽略 .claude/worktrees/；这里照做，保证主工作区
        # 在存在会话 worktree 时仍是干净的（start 要求主工作区干净）。
        (self.repo / ".git" / "info" / "exclude").write_text(
            ".claude/worktrees/\n", encoding="utf-8"
        )
        # 桌面端会话 worktree：分支 claude/*，且允许带未提交改动。
        (self.repo / ".claude" / "worktrees").mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "add", "-q", "-b", "claude/session",
            str(self.repo / ".claude" / "worktrees" / "session"), "main")
        self.claude_wt = self.repo / ".claude" / "worktrees" / "session"
        (self.claude_wt / "notes.md").write_text("scratch\n", encoding="utf-8")

    def _start(self, task, cwd=None):
        return subprocess.run(
            ["bash", str(self.repo / "deploy" / "cloud" / "agent-worktree.sh"),
             "start", "--task", task, "--owner", "dev", "--goal", "g", "--scope", "s"],
            capture_output=True, text=True, cwd=str(cwd or self.claude_wt),
            env=clean_env(TRADE_OS_AGENT_ROLE="dev",
                          TRADE_OS_WORKTREE_ROOT=str(self.worktrees)),
            timeout=60,
        )

    def test_start_from_claude_worktree_builds_task_area(self):
        proc = self._start("from-claude")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        wt = self.worktrees / "from-claude"
        self.assertTrue(wt.is_dir(), proc.stdout + proc.stderr)
        branch = git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout
        self.assertTrue(
            git(self.repo, "rev-parse", "--verify", "--quiet", "refs/heads/agent/from-claude").returncode == 0
        )
        self.assertTrue((self.repo / ".git" / "trosa-tasks" / "from-claude.json").is_file())
        # 建区不应污染 claude worktree 里的未提交改动。
        self.assertTrue((self.claude_wt / "notes.md").is_file())

    def test_create_still_requires_main_workspace(self):
        proc = subprocess.run(
            ["bash", str(self.repo / "deploy" / "cloud" / "agent-worktree.sh"),
             "create", "--task", "direct", "--owner", "dev", "--goal", "g", "--scope", "s"],
            capture_output=True, text=True, cwd=str(self.claude_wt),
            env=clean_env(TRADE_OS_AGENT_ROLE="dev",
                          TRADE_OS_WORKTREE_ROOT=str(self.worktrees)),
            timeout=60,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("主工作区", proc.stderr)

    def test_start_from_main_workspace_is_unchanged(self):
        proc = subprocess.run(
            ["bash", str(self.repo / "deploy" / "cloud" / "agent-worktree.sh"),
             "start", "--task", "from-main", "--owner", "dev", "--goal", "g", "--scope", "s"],
            capture_output=True, text=True, cwd=str(self.repo),
            env=clean_env(TRADE_OS_AGENT_ROLE="dev",
                          TRADE_OS_WORKTREE_ROOT=str(self.worktrees)),
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((self.worktrees / "from-main").is_dir())


class GuardClaudeWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve() / "repo"
        init_repo(self.repo)
        (self.repo / "README.md").write_text("repo\n", encoding="utf-8")
        commit(self.repo, "init")
        copy_workflow(self.repo)
        (self.repo / ".claude" / "worktrees").mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "add", "-q", "-b", "claude/session",
            str(self.repo / ".claude" / "worktrees" / "session"), "main")
        self.claude_wt = self.repo / ".claude" / "worktrees" / "session"

    def _guard(self, role):
        return subprocess.run(
            ["bash", str(self.repo / "deploy" / "cloud" / "agent-worktree.sh"), "guard"],
            capture_output=True, text=True, cwd=str(self.claude_wt),
            env=clean_env(TRADE_OS_AGENT_ROLE=role), timeout=30,
        )

    def test_dev_in_claude_worktree_is_pointed_at_start(self):
        proc = self._guard("dev")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("start --task", proc.stderr)
        self.assertIn("claude/session", proc.stderr)

    def test_release_in_claude_worktree_is_allowed(self):
        proc = self._guard("release")
        self.assertEqual(proc.returncode, 0, proc.stderr)


# --------------------------------------------------------------------------- #
# 3. 旧警告分级：与本任务无关的迁移编号冲突只提示、不阻断
# --------------------------------------------------------------------------- #


class PreflightGradingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.repo = base / "repo"
        self.worktrees = base / "worktrees"
        self.worktrees.mkdir()
        init_repo(self.repo)
        (self.repo / "migrations").mkdir()
        (self.repo / "migrations" / "0001_base.sql").write_text("select 1;\n", encoding="utf-8")
        (self.repo / "migrations" / "0041_a.sql").write_text("select 41;\n", encoding="utf-8")
        copy_workflow(self.repo)
        commit(self.repo, "init")

        # 另一个任务抢到了 main 已用的 0041。
        git(self.repo, "checkout", "-q", "-b", "agent/other")
        (self.repo / "migrations" / "0041_b.sql").write_text("select 42;\n", encoding="utf-8")
        commit(self.repo, "other")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "worktree", "add", "-q", str(self.worktrees / "other"), "agent/other")
        git(self.repo, "branch", "agent/cur", "main")
        git(self.repo, "worktree", "add", "-q", str(self.worktrees / "cur"), "agent/cur")
        self.cur = self.worktrees / "cur"

    def _preflight(self):
        return subprocess.run(
            ["bash", str(self.repo / "deploy" / "cloud" / "agent-worktree.sh"), "preflight"],
            capture_output=True, text=True, cwd=str(self.cur),
            env=clean_env(TRADE_OS_AGENT_ROLE="dev",
                          TRADE_OS_WORKTREE_ROOT=str(self.worktrees)),
            timeout=60,
        )

    def test_unrelated_conflict_is_a_hint_not_a_blocker(self):
        proc = self._preflight()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("与本任务无关", proc.stdout)
        self.assertIn("0041", proc.stdout)
        self.assertNotIn("与本任务相关", proc.stdout)

    def test_conflict_touching_this_task_is_a_blocker(self):
        (self.cur / "migrations" / "0041_cur.sql").write_text("select 43;\n", encoding="utf-8")
        commit(self.cur, "cur adds 0041")
        proc = self._preflight()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("与本任务相关", proc.stdout)
        self.assertIn("0041_cur.sql", proc.stdout)


# --------------------------------------------------------------------------- #
# 4. 门禁分档：test 走 fast/full，但发布前门禁不受影响
# --------------------------------------------------------------------------- #


class TestTierIntegrationTests(unittest.TestCase):
    """``test --task`` 在 docs 改动上走 fast，在运行时改动上走 full。"""

    GATE_STUB = "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$TROSA_TEST_CAPTURE\"\nexit 0\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.repo = base / "repo"
        self.worktrees = base / "worktrees"
        self.worktrees.mkdir()
        init_repo(self.repo)
        (self.repo / "README.md").write_text("repo\n", encoding="utf-8")
        (self.repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        copy_workflow(self.repo, with_gate_stub=self.GATE_STUB)
        copy_tier_tool(self.repo)
        commit(self.repo, "init")
        git(self.repo, "worktree", "add", "-q", "-b", "agent/task",
            str(self.worktrees / "task"), "main")
        self.wt = self.worktrees / "task"
        self.meta_dir = self.repo / ".git" / "trosa-tasks"
        self.meta_dir.mkdir()
        self.capture = Path(self.tmp.name) / "gate-args.txt"
        self._write_meta()

    def _write_meta(self):
        head = git(self.wt, "rev-parse", "HEAD").stdout.strip()
        (self.meta_dir / "task.json").write_text(json.dumps({
            "task": "task", "branch": "agent/task", "path": str(self.wt),
            "status": "active", "verify_result": "", "verified_commit": "",
            "_probe_head": head,
        }), encoding="utf-8")

    def _run_test(self):
        return subprocess.run(
            ["bash", str(self.repo / "deploy" / "cloud" / "agent-worktree.sh"),
             "test", "--task", "task"],
            capture_output=True, text=True, cwd=str(self.repo),
            env=clean_env(TRADE_OS_AGENT_ROLE="release",
                          TRADE_OS_WORKTREE_ROOT=str(self.worktrees),
                          TROSA_TEST_CAPTURE=str(self.capture)),
            timeout=60,
        )

    def _meta(self):
        return json.loads((self.meta_dir / "task.json").read_text(encoding="utf-8"))

    def _captured(self):
        return self.capture.read_text(encoding="utf-8").splitlines()

    def test_docs_only_change_runs_fast_and_does_not_mark_verified(self):
        (self.wt / "docs").mkdir()
        (self.wt / "docs" / "note.md").write_text("hi\n", encoding="utf-8")
        commit(self.wt, "docs change")
        proc = self._run_test()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("--fast", self._captured())
        self.assertIn("分档：fast", proc.stdout)
        meta = self._meta()
        self.assertEqual(meta.get("fast_result"), "ok")
        self.assertEqual(meta.get("verify_result", ""), "")
        self.assertEqual(meta.get("reusable_tree"), "0")
        self.assertFalse((self.meta_dir / ".verified-trees").exists())

    def test_runtime_change_runs_full_and_marks_verified(self):
        (self.wt / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        commit(self.wt, "runtime change")
        proc = self._run_test()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        captured = self._captured()
        self.assertNotIn("--fast", captured)
        self.assertNotIn("--quick", captured)
        self.assertIn("分档：full", proc.stdout)
        meta = self._meta()
        self.assertEqual(meta.get("verify_result"), "ok")
        self.assertEqual(meta.get("gate_tier"), "full")

    def test_full_flag_forces_full_on_docs_change(self):
        (self.wt / "docs").mkdir()
        (self.wt / "docs" / "note.md").write_text("hi\n", encoding="utf-8")
        commit(self.wt, "docs change")
        proc = subprocess.run(
            ["bash", str(self.repo / "deploy" / "cloud" / "agent-worktree.sh"),
             "test", "--task", "task", "--full"],
            capture_output=True, text=True, cwd=str(self.repo),
            env=clean_env(TRADE_OS_AGENT_ROLE="release",
                          TRADE_OS_WORKTREE_ROOT=str(self.worktrees),
                          TROSA_TEST_CAPTURE=str(self.capture)),
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("--fast", self._captured())
        self.assertEqual(self._meta().get("verify_result"), "ok")


class PublishGateUnaffectedTests(unittest.TestCase):
    """fast 档只属于开发期 test；发布前门禁（release-commit.sh）从不使用它。"""

    def test_release_test_wires_dev_fast_mode(self):
        text = RELEASE_TEST.read_text(encoding="utf-8")
        self.assertIn("--fast) FAST=1", text)
        self.assertIn("FAST_TEST_FILES", text)
        self.assertIn("fast=1", text)

    def test_release_commit_never_uses_fast(self):
        text = RELEASE_COMMIT.read_text(encoding="utf-8")
        self.assertNotIn("--fast", text)

    def test_agent_worktree_passes_fast_only_in_dev_branch(self):
        text = AGENT_WORKTREE.read_text(encoding="utf-8")
        self.assertIn("args+=(--fast)", text)
        self.assertIn("task_fast_log_path", text)
        # cmd_test 里 fast 分支先出现，且整段 cmd_test 只在 full 分支写完整证据。
        body = text[text.index("cmd_test() {"):text.index("cmd_ship() {")]
        self.assertEqual(body.count('"verify_result=ok"'), 1)
        self.assertLess(
            body.index("fast_result=ok"),
            body.index('"verify_result=ok"'),
        )


if __name__ == "__main__":
    unittest.main()
