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
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_ENV = ROOT / "deploy" / "cloud" / "release-env.sh"


GATE_LIB = ROOT / "deploy" / "cloud" / "lib-release-gate.sh"
AGENT_WORKTREE = ROOT / "deploy" / "cloud" / "agent-worktree.sh"
RELEASE_TEST = ROOT / "deploy" / "cloud" / "release-test.sh"
CORE_ACCEPTANCE_JS = ROOT / "tools" / "browser_acceptance.js"
BROWSER_DRIVER = ROOT / "tools" / "run_browser_acceptance.cjs"


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
        clean = self.script.index('require_clean "$wt" "任务 ${task}（改动先 commit')
        self.assertLess(guard, clean)

    def test_task_meta_records_completion_verdict(self):
        self.assertIn("完成判定：已发布", self.script)
        self.assertIn("完成判定：开发完成并通过门禁，尚未发布", self.script)

    def test_quick_gate_cannot_mark_task_verified(self):
        # 快速门禁只做语法检查：必须写独立 quick 日志，且不得写 verify_result。
        self.assertIn("task_quick_log_path", self.script)
        self.assertIn(
            'log="$(task_quick_log_path "$task")"; kind="test-quick"', self.script
        )
        # verify_result=ok 只应出现在完整门禁与发布路径（各一处）。
        self.assertEqual(self.script.count('"verify_result=ok"'), 2)
        quick = self.script.index('task_quick_log_path "$task"')
        first_full = self.script.index('"verify_result=ok"')
        self.assertLess(quick, first_full)

    def test_release_entrypoints_enforce_role(self):
        for name in ("trosa-release", "release-commit.sh"):
            text = (ROOT / "deploy" / "cloud" / name).read_text(encoding="utf-8")
            self.assertIn("trosa_require_release_role", text, name)


class BrowserFlakeGateTests(unittest.TestCase):
    """浏览器验收的瞬时失败只重跑一次，且必须落进可汇总的 flake 台账。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "flake.log"

    def retry(self, first_rc: int, retry_rc: int = 0, with_retry: bool = True):
        body = "first() { return %d; }\n" % first_rc
        if with_retry:
            body += "retry() { return %d; }\n" % retry_rc
        retry_fn = "retry" if with_retry else "first"
        snippet = (
            f'source "{GATE_LIB}"\n'
            f'export TROSA_FLAKE_LEDGER="{self.ledger}"\n'
            + body
            + "if release_gate_run_browser_step 'synthetic 步骤' "
            f'"{ROOT}" first {retry_fn}; then echo RESULT=0; else echo RESULT=$?; fi\n'
        )
        proc = run_bash(snippet)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc, proc.stdout.strip().splitlines()[-1]

    def ledger_rows(self):
        if not self.ledger.exists():
            return []
        text = self.ledger.read_text(encoding="utf-8")
        return [line.split("\t") for line in text.splitlines() if line]

    def test_success_first_try_records_nothing(self):
        _, result = self.retry(0)
        self.assertEqual(result, "RESULT=0")
        self.assertEqual(self.ledger_rows(), [])

    def test_retry_succeeds_records_flake_but_passes(self):
        proc, result = self.retry(21, 0)
        self.assertEqual(result, "RESULT=0")
        rows = self.ledger_rows()
        self.assertEqual([row[4] for row in rows], ["retrying", "ok"])
        self.assertEqual(rows[0][1], "synthetic 步骤")
        self.assertEqual(rows[0][3], "21")
        self.assertIn("flake", proc.stderr)

    def test_both_attempts_fail_fails_the_gate(self):
        _, result = self.retry(21, 21)
        self.assertEqual(result, "RESULT=1")
        self.assertEqual(
            [row[4] for row in self.ledger_rows()], ["retrying", "failed"]
        )

    def test_retry_defaults_to_the_same_step(self):
        _, result = self.retry(21, 21, with_retry=False)
        self.assertEqual(result, "RESULT=1")
        self.assertEqual(
            [row[4] for row in self.ledger_rows()], ["retrying", "failed"]
        )

    def test_assertion_failure_is_not_retried(self):
        # 断言失败（退出码 20）绝不重跑：既不写 flake，也绝不调用重跑函数，
        # 否则真实缺陷会被重跑洗成绿。用哨兵文件证明重跑函数从未被调用。
        sentinel = Path(self.tmp.name) / "retried"
        snippet = (
            f'source "{GATE_LIB}"\n'
            f'export TROSA_FLAKE_LEDGER="{self.ledger}"\n'
            "first() { return 20; }\n"
            f'retry() {{ touch "{sentinel}"; return 0; }}\n'
            "if release_gate_run_browser_step 'synthetic 步骤' "
            f'"{ROOT}" first retry; then echo RESULT=0; else echo RESULT=$?; fi\n'
        )
        proc = run_bash(snippet)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().splitlines()[-1], "RESULT=1")
        self.assertEqual(self.ledger_rows(), [])
        self.assertFalse(sentinel.exists(), "断言失败不得重跑")

    def test_infra_exit_code_is_configurable_and_wired(self):
        text = GATE_LIB.read_text(encoding="utf-8")
        self.assertIn("RELEASE_GATE_BROWSER_INFRA_EXIT", text)
        self.assertIn("RELEASE_GATE_BROWSER_INFRA_EXIT:-21", text)

    def test_ledger_path_defaults_below_shared_git_dir(self):
        proc = run_bash(
            f'source "{GATE_LIB}"\nrelease_gate_flake_ledger_path "{ROOT}"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(
            proc.stdout.strip().endswith("/trosa-tasks/.flake-events.log"),
            proc.stdout,
        )

    def test_flakes_command_aggregates_ledger(self):
        self.ledger.write_text(
            "2026-09-30T00:00:00Z\tstep-a\t/tree\t1\tok\n"
            "2026-09-30T00:01:00Z\tstep-a\t/tree\t1\tfailed\n"
            "2026-09-30T00:02:00Z\tstep-a\t/tree\t1\tok\n",
            encoding="utf-8",
        )
        proc = run_bash(
            f'TROSA_FLAKE_LEDGER="{self.ledger}" bash "{AGENT_WORKTREE}" flakes'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(proc.stdout, r"2\s+step-a \| ok")
        self.assertRegex(proc.stdout, r"1\s+step-a \| failed")

    def test_gate_wires_retry_for_browser_steps_only(self):
        text = RELEASE_TEST.read_text(encoding="utf-8")
        self.assertEqual(text.count("release_gate_run_browser_step '"), 2)
        self.assertIn("release_gate_run_browser_step '真实 Chromium 页面验收'", text)
        self.assertIn("release_gate_run_browser_step 'Inbox 专项 Chromium 验收'", text)
        # PostgreSQL rehearsal 是确定性的，绝不能被重跑包裹。
        self.assertIn('postgres_rehearsal.py" test', text)
        self.assertNotIn("run_browser_step 'PostgreSQL", text)

    def test_core_acceptance_polls_focus_and_waits_for_search_hit(self):
        js = CORE_ACCEPTANCE_JS.read_text(encoding="utf-8")
        # 弹窗焦点由 requestAnimationFrame 调度，断言必须轮询而不是瞬时采样。
        self.assertIn("waitForFunction", js)
        self.assertNotIn("completeFocusId", js)
        # 搜索断言必须等到「命中」高亮的刷新快照，而不是某一条更早的具体沟通正文。
        self.assertIn(".filter({hasText: '命中'})", js)
        self.assertNotIn("hasText: 'Browser acceptance customer reply'", js)
        # 每条记录带本次运行唯一标记，搜索断言据此要求“恰好命中一条”。
        self.assertIn("TROSA_BROWSER_ACCEPTANCE_RUN_TAG", js)
        self.assertIn("searchHitCount === 1", js)

    def test_browser_driver_classifies_infra_vs_assertion(self):
        self.assertTrue(BROWSER_DRIVER.is_file())
        if shutil.which("node") is None:
            self.skipTest("node 不可用")
        cases = (
            ("net::ERR_CONNECTION_REFUSED", None, "infra"),
            ("Timeout 15000ms exceeded", None, "assertion"),
            ("anything at all", "assertion", "assertion"),
        )
        for message, override, expected in cases:
            script = (
                "const m=require(process.argv[1]);"
                "const e=Object.assign(new Error(process.argv[2]),"
                "process.argv[3]?{acceptanceClass:process.argv[3]}:{});"
                "console.log(m.classify(e));"
            )
            proc = subprocess.run(
                ["node", "-e", script, str(BROWSER_DRIVER), message, override or ""],
                capture_output=True, text=True, timeout=30, cwd=str(ROOT),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), expected, message)

    def test_browser_launchers_use_locked_headless_chromium(self):
        for name in ("browser_acceptance.sh", "inbox_browser_acceptance.sh"):
            text = (ROOT / "tools" / name).read_text(encoding="utf-8")
            self.assertNotIn("tabbit", text.lower(), name)
            self.assertIn("run_browser_acceptance.cjs", text, name)
            self.assertIn("lib-release-lock.sh", text, name)
            self.assertIn("trosa_lock_acquire", text, name)
            # 每次运行生成唯一 RUN_ID；缺失浏览器依赖是基础设施硬失败，不是 SKIP。
            self.assertIn("RUN_ID", text, name)
            self.assertIn("不会把浏览器验收标记为 SKIP", text, name)

    def test_evidence_is_append_only_and_landed_is_immutable(self):
        text = AGENT_WORKTREE.read_text(encoding="utf-8")
        # 证据只追加：允许 >>"$log"，绝不允许单 > 覆盖同一证据文件。
        self.assertIn('>>"$log"', text)
        self.assertNotRegex(text, r'(?<!>)>"\$log"')
        self.assertIn("# --- run", text)
        # 已 landed 的任务重跑只能追加证据，不得改写已发布的验证结论。
        self.assertIn("task_status", text)
        self.assertIn("已是 landed；本次只追加证据，不改写已发布的验证结论", text)


class EntryConvergenceTests(unittest.TestCase):
    """开发方入口收敛：start / ship / 懒预留迁移号，且不得越权发布。"""

    def _script_text(self) -> str:
        return AGENT_WORKTREE.read_text(encoding="utf-8")

    def _function_body(self, name: str) -> str:
        text = self._script_text()
        start = text.index(f"{name}() {{")
        end = text.index("\n}\n", start)
        return text[start:end]

    def test_usage_and_dispatch_expose_new_entrypoints(self):
        text = self._script_text()
        self.assertIn("agent-worktree.sh start --task", text)
        self.assertIn("agent-worktree.sh ship --task", text)
        self.assertIn("agent-worktree.sh reserve-migration --task", text)
        self.assertIn("start) cmd_start ", text)
        self.assertIn("ship) cmd_ship ", text)
        self.assertIn("reserve-migration) cmd_reserve_migration ", text)

    def test_start_wires_guard_status_preflight_and_create(self):
        body = self._function_body("cmd_start")
        # start 合并了环境判定、并发体检与建区三步，并要求主工作区干净。
        self.assertIn("require_main_workspace", body)
        self.assertIn("trosa_agent_role", body)
        self.assertIn("cmd_preflight", body)
        self.assertIn("cmd_create", body)
        # 有在途改动时拒绝并指向 adopt，而不是把脏改动留在主工作区。
        self.assertIn("status --porcelain --untracked-files=all", body)
        self.assertIn("adopt --task", body)
        self.assertIn("fail", body)

    def test_create_no_longer_reserves_migration_by_default(self):
        body = self._function_body("cmd_create")
        self.assertIn("reserve=0", body)
        # 兼容旧参数：仍是合法输入，且提供 opt-in。
        self.assertIn("--no-reserve-migration) reserve=0", body)
        self.assertIn("--reserve-migration) reserve=1", body)
        self.assertNotIn("reserve=1\n  while", body)

    def test_reserve_migration_is_lazy_and_idempotent(self):
        body = self._function_body("cmd_reserve_migration")
        # 只有真正要写迁移时才取号，且已有编号时不重复占用。
        self.assertIn("isdigit()", body)
        self.assertIn("begin_migration_lock", body)
        self.assertIn("next_migration_number", body)
        self.assertIn("merge_task_meta", body)
        # 幂等分支必须早于取号返回。
        self.assertIn("已预留迁移编号", body)

    def test_ship_composes_sync_quick_gate_and_queue_without_publishing(self):
        body = self._function_body("cmd_ship")
        self.assertIn("cmd_sync", body)
        self.assertIn("cmd_test", body)
        self.assertIn("--quick", body)
        self.assertIn("status=shipped", body)
        self.assertIn(".ship-queue", body)
        self.assertIn("shipped_commit", body)
        # 开发方能力边界：ship 绝不能自己发布或推送。
        for forbidden in ("cmd_publish", "auto-publish.sh", "release-commit.sh", "git push"):
            self.assertNotIn(forbidden, body, forbidden)

    def test_ship_queue_fields_are_tsv_and_ship_status_has_verdict(self):
        text = self._script_text()
        # 队列行：时间 任务 分支 commit（制表符分隔）。
        self.assertIn("'%s\\t%s\\t%s\\t%s\\n'", text)
        # 任务清单要能读出“已交付”判定。
        self.assertIn('elif status == "shipped":', text)
        self.assertIn("shipped_commit", text)


if __name__ == "__main__":
    unittest.main()
