"""发布流水线（capability-based）与风险分级的回归。

覆盖两件事，都不触碰 ECS、也不接触任何发布凭据：

* ``tools/release_tier.py`` 的 T0/T1/T2 判定：级别由流水线根据 diff 计算，
  开发方无法声明；
* ``deploy/cloud/release-pipeline.sh`` 的入口收敛：只消费交付队列、自己重算
  分级与门禁、默认 dry-run、T2 必须人工放行、T0 不部署；
* ``release-commit.sh --pipeline``：流水线调用时强制重跑门禁、不要求开发方
  已有证据（而不是弱化安全）。
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIER_TOOL = ROOT / "tools" / "release_tier.py"
PIPELINE = ROOT / "deploy" / "cloud" / "release-pipeline.sh"
RELEASE_COMMIT = ROOT / "deploy" / "cloud" / "release-commit.sh"


def run_cmd(args, **env) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith("TRADE_OS_")}
    base.update(env)
    return subprocess.run(
        args, capture_output=True, text=True, timeout=30, cwd=str(ROOT), env=base,
    )


def tier(paths) -> str:
    proc = run_cmd(["python3", str(TIER_TOOL), "--paths", *paths])
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return proc.stdout.strip()


def script_text() -> str:
    return PIPELINE.read_text(encoding="utf-8")


def function_body(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start)
    return text[start:end]


class ReleaseTierTests(unittest.TestCase):
    def test_docs_design_tests_and_markdown_are_t0(self):
        self.assertEqual(tier(["docs/a.md", "design/b.md", "tests/test_x.py"]), "T0")
        self.assertEqual(tier(["CHANGELOG.md"]), "T0")

    def test_runtime_code_is_t1(self):
        self.assertEqual(tier(["app/static/app.js", "README.md"]), "T1")
        self.assertEqual(tier(["app.py"]), "T1")
        self.assertEqual(tier(["tools/browser_acceptance.js"]), "T1")

    def test_protected_paths_are_t2(self):
        for path in (
            "migrations/0112_x.sql",
            "deploy/cloud/release-test.sh",
            "deploy/cloud/README.md",
            "serve.py",
            "config.py",
            "AGENTS.md",
            "tools/release_baseline.py",
            "tools/reconcile_migrations.py",
            "tools/backup_helper.py",
        ):
            with self.subTest(path=path):
                self.assertEqual(tier([path]), "T2", path)

    def test_t2_wins_over_t0_and_t1(self):
        self.assertEqual(tier(["docs/a.md", "serve.py"]), "T2")
        self.assertEqual(tier(["app/static/app.js", "migrations/0112_x.sql"]), "T2")

    def test_pipeline_itself_is_t2(self):
        self.assertEqual(tier(["tools/release_tier.py"]), "T2")
        self.assertEqual(tier(["deploy/cloud/release-pipeline.sh"]), "T2")

    def test_empty_diff_is_t0(self):
        self.assertEqual(tier([]), "T0")

    def test_explain_writes_reasons_to_stderr(self):
        proc = run_cmd(["python3", str(TIER_TOOL), "--paths", "serve.py", "--explain"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "T2")
        self.assertIn("serve.py", proc.stderr)


class ReleasePipelineWiringTests(unittest.TestCase):
    def setUp(self):
        self.text = script_text()

    def test_usage_and_dispatch_expose_entrypoints(self):
        for name in ("classify", "status", "run"):
            self.assertIn(name, self.text)
        self.assertIn("classify) cmd_classify", self.text)
        self.assertIn("status) cmd_status", self.text)
        self.assertIn("run) cmd_run", self.text)

    def test_defaults_to_dry_run_and_publish_is_explicit(self):
        run = function_body(self.text, "cmd_run")
        self.assertIn("dry_run=1", run)
        self.assertIn("--publish) dry_run=0", run)

    def test_consumes_ship_queue_and_writes_state(self):
        self.assertIn('.ship-queue', self.text)
        self.assertIn('.pipeline-state', self.text)
        self.assertIn("queue_lines", self.text)
        self.assertIn("record_state", self.text)

    def test_recomputes_tier_and_dispatches_by_level(self):
        run = function_body(self.text, "cmd_run")
        self.assertIn("tools/release_tier.py", self.text)
        self.assertIn("awaiting-approval", run)
        self.assertIn("merge-only", run)
        self.assertIn("handle_t1", run)
        self.assertIn("status=awaiting-approval", run)

    def test_t1_goes_through_release_commit_with_pipeline_flag(self):
        body = function_body(self.text, "handle_t1")
        self.assertIn("--pipeline", body)
        self.assertIn("--dry-run", body)
        self.assertIn("--branch", body)

    def test_real_publish_requires_release_role(self):
        body = function_body(self.text, "handle_t1")
        self.assertIn("trosa_require_release_role", body)
        self.assertIn("refused", body)

    def test_never_handles_credentials(self):
        # 只允许在注释里提到凭据位置，绝不读取或复制任何机密。
        for token in ("access_key", "access_key_secret", "TRADE_OS_WORKBENCH_CONFIG"):
            self.assertNotIn(token, self.text)
        self.assertNotIn("cp ", self.text)
        self.assertNotIn("scp ", self.text)

    def test_validates_queue_entry_against_task_manifest(self):
        body = function_body(self.text, "validate_entry")
        self.assertIn("shipped_commit", body)
        self.assertIn("refs/heads/", body)

    def test_publish_entry_comes_from_own_checkout(self):
        self.assertIn('PUBLISH_ENTRY="$SCRIPT_DIR/release-commit.sh"', self.text)


class PipelineReleaseCommitFlagTests(unittest.TestCase):
    def setUp(self):
        self.text = RELEASE_COMMIT.read_text(encoding="utf-8")

    def test_pipeline_flag_is_parsed(self):
        self.assertIn("--pipeline) PIPELINE=1", self.text)
        self.assertIn("--pipeline", self.text)

    def test_pipeline_skips_dev_evidence_preconditions(self):
        self.assertEqual(self.text.count('[[ "${PIPELINE:-0}" == 1 ]] && return 0'), 2)

    def test_pipeline_forces_full_gate_no_reuse(self):
        self.assertIn('if [[ "$PIPELINE" == 1 ]]; then', self.text)
        self.assertIn("流水线模式强制重跑全量门禁", self.text)

    def test_pipeline_flag_is_accepted_without_publishing(self):
        # 无 commit/branch 时应停在“必须提供 --commit 或 --branch”，而不是未知参数。
        proc = run_cmd(["bash", str(RELEASE_COMMIT), "--pipeline"])
        self.assertNotIn("未知参数：--pipeline", proc.stderr)
        self.assertIn("--commit", proc.stderr + proc.stdout)


if __name__ == "__main__":
    unittest.main()
