"""发布角色边界、配置位置与完成证据的回归。

这套边界回答两个问题，且完全不触碰 ECS：

* 谁可以发布：``TRADE_OS_AGENT_ROLE=dev/review`` 只能开发与验证；
* 任务是否真的完成：``agent-worktree.sh test/publish`` 会把 commit、门禁结果和
  发布结果落盘为任务证据，而不是依赖一句自述。

发布配置（``workbench.env``）的正式位置在用户配置目录（仓库外），仓库内旧位置
仅保留兼容读取并提示迁移。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_ENV = ROOT / "deploy" / "cloud" / "release-env.sh"


def run_bash(snippet: str, **env) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith("TRADE_OS_")}
    base.update(env)
    return subprocess.run(
        ["bash", "-c", snippet], capture_output=True, text=True,
        timeout=30, cwd=str(ROOT), env=base,
    )


class WorkbenchEnvResolutionTests(unittest.TestCase):
    """配置解析必须可预测，且能同时在干净 clone 和已迁移机器上通过。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.script_dir = Path(self.tmp.name) / "scripts"
        self.script_dir.mkdir()

    def resolve(self, script_dir=None, **env):
        proc = run_bash(
            f'source "{RELEASE_ENV}"\n'
            f'trosa_resolve_workbench_env "{script_dir or self.script_dir}"',
            **env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_explicit_override_wins(self):
        explicit = Path(self.tmp.name) / "custom.env"
        explicit.write_text("TRADE_OS_ECS_REGION=r\n", encoding="utf-8")
        self.assertEqual(
            self.resolve(TRADE_OS_WORKBENCH_ENV=str(explicit)), str(explicit)
        )

    def test_canonical_user_config_is_preferred(self):
        cfg = Path(self.tmp.name) / "trosa"
        cfg.mkdir()
        canonical = cfg / "workbench.env"
        canonical.write_text("TRADE_OS_ECS_REGION=r\n", encoding="utf-8")
        self.assertEqual(
            self.resolve(XDG_CONFIG_HOME=self.tmp.name), str(canonical)
        )

    def test_legacy_in_repo_path_still_readable(self):
        legacy = self.script_dir / "workbench.env"
        legacy.write_text("TRADE_OS_ECS_REGION=r\n", encoding="utf-8")
        self.assertEqual(
            self.resolve(XDG_CONFIG_HOME=str(Path(self.tmp.name) / "empty")),
            str(legacy),
        )

    def test_missing_config_reports_canonical_target(self):
        empty = Path(self.tmp.name) / "empty"
        expected = f"{empty}/trosa/workbench.env"
        self.assertEqual(self.resolve(XDG_CONFIG_HOME=str(empty)), expected)

    def test_legacy_path_warns_migration(self):
        proc = run_bash(
            f'source "{RELEASE_ENV}"\n'
            f'trosa_warn_legacy_workbench_env "{ROOT}/deploy/cloud/workbench.env"'
        )
        self.assertIn("迁移", proc.stderr)


class ReleaseRoleGuardTests(unittest.TestCase):
    """dev/review 必须被拒绝；未设置时保持人工操作可用。"""

    def role(self, value):
        snippet = (
            f'source "{RELEASE_ENV}"\n'
            "if trosa_require_release_role; then echo ALLOW; else echo DENY; fi\n"
        )
        env = {"XDG_CONFIG_HOME": tempfile.gettempdir()}
        if value is not None:
            env["TRADE_OS_AGENT_ROLE"] = value
        return run_bash(snippet, **env)

    def test_unset_defaults_to_release(self):
        self.assertEqual(self.role(None).stdout.strip(), "ALLOW")

    def test_release_allowed(self):
        self.assertEqual(self.role("release").stdout.strip(), "ALLOW")

    def test_dev_and_review_denied(self):
        for value in ("dev", "development", "review", "readonly", "read-only"):
            proc = self.role(value)
            self.assertEqual(proc.stdout.strip(), "DENY", value)
            self.assertIn("发布被拒绝", proc.stderr)

    def test_unknown_role_denied(self):
        proc = self.role("superuser")
        self.assertEqual(proc.stdout.strip(), "DENY")
        self.assertIn("未知角色", proc.stderr)


class TaskEvidenceContractTests(unittest.TestCase):
    """完成证据与发布角色必须真正接进 agent-worktree 的入口。"""

    def setUp(self):
        self.script = (ROOT / "deploy" / "cloud" / "agent-worktree.sh").read_text(
            encoding="utf-8"
        )

    def test_worktree_script_wires_evidence_and_role(self):
        for token in (
            "task_evidence_path",
            "run_with_evidence",
            "merge_task_meta",
            "verify_result=ok",
            "status=landed",
            "landed_release",
            "trosa_require_release_role",
            "evidence) cmd_evidence",
        ):
            self.assertIn(token, self.script)

    def test_help_lists_evidence_command(self):
        proc = subprocess.run(
            ["bash", "deploy/cloud/agent-worktree.sh", "--help"],
            capture_output=True, text=True, timeout=30, cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("evidence --task", proc.stdout)

    def test_role_guard_runs_before_clean_check_on_publish(self):
        guard = self.script.index("trosa_require_release_role || fail '当前角色没有发布权限")
        clean = self.script.index('require_clean "$wt" "任务 $task（改动先 commit')
        self.assertLess(guard, clean)

    def test_task_meta_records_completion_verdict(self):
        self.assertIn("完成判定：已发布", self.script)
        self.assertIn("完成判定：开发完成并通过门禁，尚未发布", self.script)

    def test_release_entrypoints_enforce_role(self):
        for name in ("trosa-release", "release-commit.sh"):
            text = (ROOT / "deploy" / "cloud" / name).read_text(encoding="utf-8")
            self.assertIn("trosa_require_release_role", text, name)


if __name__ == "__main__":
    unittest.main()
