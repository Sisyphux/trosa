"""Release mechanism regressions: DB planning, entrypoint routing, fault clarity.

Covers the new formal release system without touching production:

* tools/release_db_plan.py classification (none / compatible / destructive /
  sensitive_runtime), including comment/string-literal false positives;
* deploy/cloud/trosa-release unified entrypoint (help, arg validation,
  db-plan preflight, no SSH usage in the release path);
* deploy/cloud/release-remote.sh + status-remote.sh shell validity and the
  machine-readable result/status contract shape.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import release_db_plan as plan


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                          cwd=str(ROOT), **kwargs)


def clean_env(**extra):
    """Child process environment without the caller's release/agent-role state.

    These entrypoint tests exercise ``trosa-release`` (including the publish
    argument checks) and must not inherit ``TRADE_OS_AGENT_ROLE`` from a
    dev/review session, or the role guard would reject the call before the
    behaviour under test is reached.
    """
    base = {k: v for k, v in os.environ.items() if not k.startswith("TRADE_OS_")}
    base.update(extra)
    return base


class ReleaseIdTests(unittest.TestCase):
    def test_valid_ids(self):
        for value in ("rel-20260916090000-abcdef1", "rollback-20260916",
                      "auto-20260912085022-9d7d87b", "v1.2_3-abc"):
            self.assertTrue(plan.is_valid_release_id(value), value)

    def test_invalid_ids_reject_path_traversal_and_empty(self):
        for value in ("", "../escape", "a/b", "has space", "semi;colon",
                      "x" * 129, "rel$(evil)", "a'b"):
            self.assertFalse(plan.is_valid_release_id(value), value)


class DestructiveSqlTests(unittest.TestCase):
    def test_destructive_statements_detected(self):
        self.assertTrue(plan.is_destructive_sql("ALTER TABLE t DROP COLUMN c;"))
        self.assertTrue(plan.is_destructive_sql("truncate table foo"))
        self.assertTrue(plan.is_destructive_sql("DELETE FROM trosa.accounts WHERE 1=1"))
        self.assertTrue(plan.is_destructive_sql("DROP TABLE trosa.old;"))

    def test_index_constraint_replacement_is_compatible(self):
        # Dropping a superseded index/constraint loses no business data
        # (the replacement is created in the same migration). It stays
        # auto-publishable; the compatible path still backs up first.
        self.assertFalse(plan.is_destructive_sql("DROP INDEX IF EXISTS idx_foo;"))
        self.assertFalse(plan.is_destructive_sql(
            "ALTER TABLE t DROP CONSTRAINT c;"))

    def test_comments_and_literals_do_not_trigger(self):
        self.assertFalse(plan.is_destructive_sql(
            "-- This migration is forward-only. It copies keys, never DROP TABLE.\n"
            "UPDATE trosa.accounts SET name='x';"))
        self.assertFalse(plan.is_destructive_sql(
            "/* DROP TABLE mentioned in a block comment */\nSELECT 1;"))
        self.assertFalse(plan.is_destructive_sql(
            "INSERT INTO audit_log(note) VALUES ('user asked to DROP TABLE foo');"))

    def test_normal_migrations_are_compatible(self):
        self.assertFalse(plan.is_destructive_sql(
            "BEGIN;\nCREATE TABLE IF NOT EXISTS trosa.foo(id bigint primary key);\n"
            "CREATE OR REPLACE VIEW trade_os_compat.foo AS SELECT * FROM trosa.foo;\nCOMMIT;"))
        self.assertFalse(plan.is_destructive_sql(
            "BEGIN;\nUPDATE trosa.account_legacy_refs SET x=1 WHERE y=2;\nCOMMIT;"))

    def test_trigger_plumbing_is_not_destructive(self):
        # 0010 rewrites trigger functions whose bodies contain row-sync
        # DELETEs. Those install future write behavior; they do not delete
        # data at migration time, so they must stay auto-publishable.
        text = (ROOT / "migrations" / "0010_postgres_user_scoped_external_ids.sql").read_text(encoding="utf-8")
        self.assertFalse(plan.is_destructive_sql(text))

    def test_migration_time_teardown_is_destructive(self):
        # 0009 drops a compat table inside a DO block, which executes during
        # the migration itself. Future migrations shaped like this must
        # require explicit approval, even with a backup in place.
        text = (ROOT / "migrations" / "0009_postgres_final_integrity_boundaries.sql").read_text(encoding="utf-8")
        self.assertTrue(plan.is_destructive_sql(text))


class DatabaseSensitivePathTests(unittest.TestCase):
    """Migrations documentation must not look like executable DB change.

    ``migrations/README.md`` describes DROP/DELETE keywords for readers; it
    never runs at migration time, so it must not force a backup or trip the
    destructive heuristic that publishes rely on.
    """

    def test_migrations_markdown_is_not_sensitive(self):
        self.assertFalse(plan.is_db_sensitive_path("migrations/README.md"))
        self.assertTrue(plan.is_db_sensitive_path("migrations/0034_customer_history_binding.sql"))

    def test_runtime_and_production_paths_are_sensitive(self):
        self.assertTrue(plan.is_db_sensitive_path("db.py"))
        self.assertTrue(plan.is_db_sensitive_path("tools/unified_postgres_migration.py"))
        self.assertTrue(plan.is_db_sensitive_path("deploy/postgres-production/backup.sh"))
        self.assertFalse(plan.is_db_sensitive_path("docs/README.md"))
        self.assertFalse(plan.is_db_sensitive_path("app/static/app.js"))

    def test_release_commit_heuristic_only_reads_executable_files(self):
        text = (ROOT / "deploy" / "cloud" / "release-commit.sh").read_text(encoding="utf-8")
        self.assertIn("DB_DIFF_FILES", text)
        self.assertIn("migrations/*.sql) DB_SQL_FILES", text)


class PlanReleaseDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mdir = os.path.join(self.tmp.name, "migrations")
        os.makedirs(self.mdir)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, contents="BEGIN; SELECT 1; COMMIT;"):
        with open(os.path.join(self.mdir, name), "w", encoding="utf-8") as handle:
            handle.write(contents)

    def test_no_pending_no_changes_is_none(self):
        self._write("0001_a.sql")
        result = plan.plan_release_db(self.mdir, {"0001_a.sql"}, [])
        self.assertEqual(result["category"], "none")
        self.assertFalse(result["requires_backup"])
        self.assertTrue(result["allow_auto_apply"])
        self.assertEqual(result["migration_count"], 0)

    def test_new_safe_file_is_compatible_and_needs_backup(self):
        self._write("0001_a.sql")
        self._write("0002_b.sql", "BEGIN; CREATE TABLE trosa.n(id int); COMMIT;")
        result = plan.plan_release_db(self.mdir, {"0001_a.sql"}, [])
        self.assertEqual(result["category"], "compatible")
        self.assertEqual(result["pending_migrations"], ["0002_b.sql"])
        self.assertTrue(result["requires_backup"])
        self.assertTrue(result["allow_auto_apply"])

    def test_new_destructive_file_blocks_auto_apply(self):
        self._write("0001_a.sql")
        self._write("0002_drop.sql", "BEGIN; DROP TABLE trosa.old; COMMIT;")
        result = plan.plan_release_db(self.mdir, {"0001_a.sql"}, [])
        self.assertEqual(result["category"], "destructive")
        self.assertEqual(result["destructive_files"], ["0002_drop.sql"])
        self.assertTrue(result["requires_backup"])
        self.assertFalse(result["allow_auto_apply"])

    def test_runtime_code_change_without_new_migration_is_sensitive(self):
        self._write("0001_a.sql")
        result = plan.plan_release_db(self.mdir, {"0001_a.sql"}, ["db.py"])
        self.assertEqual(result["category"], "sensitive_runtime")
        self.assertEqual(result["pending_migrations"], [])
        self.assertTrue(result["requires_backup"])
        self.assertTrue(result["allow_auto_apply"])

    def test_unrelated_code_change_is_none(self):
        self._write("0001_a.sql")
        result = plan.plan_release_db(self.mdir, {"0001_a.sql"}, ["app/static/app.js"])
        self.assertEqual(result["category"], "none")

    def test_migrations_markdown_change_is_not_sensitive(self):
        self._write("0001_a.sql")
        result = plan.plan_release_db(self.mdir, {"0001_a.sql"}, ["migrations/README.md"])
        self.assertEqual(result["category"], "none")
        self.assertEqual(result["sensitive_paths"], [])

    def test_unreadable_ledger_is_rejected(self):
        self._write("0001_a.sql")
        with self.assertRaisesRegex(ValueError, "ledger is required"):
            plan.plan_release_db(self.mdir, None, [])

    def test_error_text_cannot_be_an_applied_migration_name(self):
        self._write("0001_a.sql")
        with self.assertRaisesRegex(ValueError, "invalid applied migration filename"):
            plan.plan_release_db(self.mdir, {"LEDGER_UNREADABLE: psycopg missing"}, [])

    def test_production_ledger_leaves_compatible_migrations(self):
        migration_dir = ROOT / "migrations"
        # Use the actual files rather than synthesizing names: this confirms
        # a historic destructive migration is excluded before classification.
        # Expected pending migrations are derived from the directory so adding
        # a parallel task's forward migration does not break this release test.
        local = sorted(
            path.name for path in migration_dir.glob("*.sql")
            if path.name[:4].isdigit()
        )
        applied = {name for name in local if int(name[:4]) <= 30}
        expected_pending = [name for name in local if int(name[:4]) > 30]
        self.assertEqual(len(applied), 30)
        result = plan.plan_release_db(str(migration_dir), applied, [])
        self.assertEqual(result["pending_migrations"], expected_pending)
        expected_destructive = [
            name for name in expected_pending
            if plan.is_destructive_sql(
                (migration_dir / name).read_text(encoding="utf-8")
            )
        ]
        if expected_destructive:
            self.assertEqual(result["category"], "destructive")
            self.assertEqual(result["destructive_files"], expected_destructive)
        else:
            self.assertEqual(result["category"], "compatible")
            self.assertEqual(result["destructive_files"], [])
        self.assertNotIn("0009_postgres_final_integrity_boundaries.sql", result["pending_migrations"])

    def test_cli_emits_machine_readable_json(self):
        self._write("0001_a.sql")
        proc = run([sys.executable, "tools/release_db_plan.py", self.mdir,
                    "--applied", "0001_a.sql", "--changed", "app.py"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["category"], "none")

    def test_cli_rejects_error_text_as_applied_ledger(self):
        self._write("0001_a.sql")
        proc = run([sys.executable, "tools/release_db_plan.py", self.mdir,
                    "--applied", "LEDGER_UNREADABLE:psycopg missing"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid applied migration ledger", proc.stderr)


class UnifiedEntrypointTests(unittest.TestCase):
    """trosa-release must be exercisable from any clean checkout.

    Its routing file (`deploy/cloud/workbench.env`) is local-only and never
    committed, so every invocation here supplies its own dummy routing file
    instead of depending on the one machine that owns the real thing. A gate
    that only passes where local secrets happen to exist is not a gate: the
    same checks must hold in a fresh clone, a task worktree and the release
    worktree, where no routing file exists by design.
    """

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix="-workbench.env", delete=False, encoding="utf-8")
        handle.write("TRADE_OS_ECS_REGION=test-region\n")
        handle.write("TRADE_OS_ECS_INSTANCE_ID=i-test-instance\n")
        handle.write(f"PROJECT_ROOT={ROOT}\n")
        handle.close()
        self.env_file = handle.name
        self.addCleanup(os.remove, self.env_file)

    def release(self, *args):
        return run(["bash", "deploy/cloud/trosa-release", *args],
                   env=clean_env(TRADE_OS_WORKBENCH_ENV=self.env_file))

    def test_help_lists_single_entry_commands(self):
        proc = self.release("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for token in ("publish", "status", "rollback", "db-plan"):
            self.assertIn(token, proc.stdout)

    def test_unknown_command_fails_fast(self):
        proc = self.release("upload-by-hand")
        self.assertNotEqual(proc.returncode, 0)

    def test_publish_requires_pushed_commit_sha(self):
        proc = self.release("publish", "--commit", "not-a-sha")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("commit", proc.stderr.lower())

    def test_db_plan_preflight_works_offline(self):
        proc = self.release("db-plan",
                            "--applied", "0001_unified_trade_os.sql",
                            "--changed", "app.py")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertIn(doc["category"], ("compatible", "destructive"))

    def test_release_path_never_uses_ssh_transport(self):
        text = (ROOT / "deploy" / "cloud" / "trosa-release").read_text(encoding="utf-8")
        self.assertIn("run-cloud-assistant-command.sh", text)
        self.assertIn("Cloud Assistant", text)
        # No scp/ssh invocation may remain in the release path.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            self.assertNotRegex(stripped, r"(^|\s)(ssh|scp)\s",
                                f"SSH transport leaked into unified entry: {line}")

    def test_cloud_assistant_client_does_not_require_executable_bit(self):
        text = (ROOT / "deploy" / "cloud" / "run-cloud-assistant-command.sh").read_text(encoding="utf-8")
        # The client is always invoked via `python3` (default
        # $script_dir/cloud-assistant.py; env override is a test hook only).
        self.assertIn('client="${TRADE_OS_CLOUD_ASSISTANT_CLIENT:-$script_dir/cloud-assistant.py}"', text)
        self.assertIn('python3 "$client" run', text)
        self.assertIn('python3 "$client" get', text)

    def test_workbench_noninteractive_commands_explicitly_use_bash(self):
        text = (ROOT / "deploy" / "cloud" / "run-workbench-command.sh").read_text(encoding="utf-8")
        self.assertIn('command_payload="$(printf', text)
        self.assertIn('workbench_command="bash -c', text)
        self.assertIn('--command "$workbench_command"', text)

    def test_shell_syntax_valid(self):
        for name in ("trosa-release", "release-remote.sh", "status-remote.sh",
                     "auto-publish.sh", "release-commit.sh", "release-test.sh",
                     "release-env.sh", "agent-worktree.sh", "run-workbench-command.sh",
                     "run-cloud-assistant-command.sh", "cloud-assistant-bootstrap.sh",
                     "lib-release-gate.sh"):
            proc = run(["bash", "-n", f"deploy/cloud/{name}"])
            self.assertEqual(proc.returncode, 0, f"{name}: {proc.stderr}")

    def test_remote_runner_uses_formal_venv_and_fails_before_classifier(self):
        text = (ROOT / "deploy" / "cloud" / "release-remote.sh").read_text(encoding="utf-8")
        ledger_start = text.index("# ---- db plan")
        ledger_failure = text.index('write_result "failed" "db_plan" "migration ledger unreadable"', ledger_start)
        classifier = text.index('release_db_plan.py', ledger_start)
        destructive_refusal = text.index('write_result "refused" "db_plan" "destructive database change', ledger_start)
        self.assertIn('formal_python="$REMOTE_ROOT/venv/bin/python"', text)
        self.assertLess(ledger_failure, classifier)
        self.assertLess(ledger_failure, destructive_refusal)
        self.assertNotIn("LEDGER_UNREADABLE", text[ledger_start:])

    def test_readonly_ecs_db_plan_capability_is_fixed_and_secret_free(self):
        text = (ROOT / "deploy" / "cloud" / "cloud-assistant-bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn("db-plan-readonly accepts no arguments", text)
        self.assertIn("install -d -m 0711 -o root -g root /usr/local/lib/trosa", text)
        self.assertIn("TROSA_DB_PLAN_COMMIT must be an exact 40-character", text)
        self.assertIn("/usr/local/lib/trosa/release_db_plan.py", text)
        self.assertIn("https://codeload.github.com/Sisyphux/trosa/tar.gz/${planner_commit}", text)
        self.assertIn("/usr/local/lib/trosa/db-plan-migrations", text)
        self.assertIn("runuser -u tradeos", text)
        self.assertIn("#!/opt/trade-os/venv/bin/python", text)
        self.assertIn("SELECT name FROM audit.schema_migrations", text)
        self.assertIn("release_db_plan.py", text)
        self.assertIn('db-plan-readonly ""', text)
        self.assertIn('"migration_ledger_unreadable"', text)
        self.assertNotIn("print(database_url", text)
        self.assertNotIn("print(pgpassfile", text)
        self.assertNotIn("str(exc)", text)

    def test_remote_runner_rejects_bad_arguments_without_side_effects(self):
        proc = run(["bash", "deploy/cloud/release-remote.sh",
                    "/opt/trade-os", "trade-os", "../evil", "abc",
                    "https://github.com/o/r", "deploy"])
        self.assertIn(proc.returncode, (1, 2))

    def test_status_remote_emits_machine_readable_lines(self):
        # Read-only: systemctl/curl failures degrade to down/unknown values,
        # but the contract lines must always be present.
        proc = run(["bash", "deploy/cloud/status-remote.sh"])
        self.assertIn("TROSA_MANAGER_STATUS ", proc.stdout)
        self.assertIn("TROSA_DEPLOY_JSON ", proc.stdout)
        deploy_line = [line for line in proc.stdout.splitlines()
                       if line.startswith("TROSA_DEPLOY_JSON ")][-1]
        doc = json.loads(deploy_line[len("TROSA_DEPLOY_JSON "):])
        for key in ("deploy_state", "last_result", "release_manifest",
                    "migration_ledger"):
            self.assertIn(key, doc)


class ResultContractTests(unittest.TestCase):
    """The DEPLOY_RESULT.json shape agents rely on must stay stable."""

    REQUIRED_KEYS = {"release", "commit", "mode", "phase", "status",
                     "production", "previous", "backup", "migration",
                     "health", "error", "next_action", "updated_at"}
    TERMINAL_STATUSES = {"success", "failed", "rolled_back", "rollback_failed",
                         "refused", "in_progress"}

    def test_runner_writes_all_contract_keys(self):
        text = (ROOT / "deploy" / "cloud" / "release-remote.sh").read_text(encoding="utf-8")
        for key in self.REQUIRED_KEYS:
            self.assertIn(f'"{key}"', text, f"result contract missing key: {key}")

    def test_client_handles_every_terminal_status(self):
        text = (ROOT / "deploy" / "cloud" / "trosa-release").read_text(encoding="utf-8")
        for status in ("success", "refused", "rolled_back"):
            self.assertIn(status, text, f"client has no branch for status: {status}")

    def test_runner_reports_in_progress_before_mutation(self):
        # A dropped local network must never look like a silent hang: the
        # runner records in_progress on ECS first, so a re-run can poll it.
        text = (ROOT / "deploy" / "cloud" / "release-remote.sh").read_text(encoding="utf-8")
        self.assertEqual(text.count('write_result "in_progress" "started"'), 2)

    def test_polling_endpoint_is_single_line_json(self):
        text = (ROOT / "deploy" / "cloud" / "release-remote.sh").read_text(encoding="utf-8")
        self.assertIn("separators=(',',':')", text)

    def test_client_poll_parser_handles_compact_result(self):
        doc = {"release": "rel-x", "status": "rolled_back", "phase": "health",
               "next_action": "fix and re-run"}
        compact = json.dumps(doc, sort_keys=True, separators=(",", ":"))
        stream = "TROSA_MANAGER_COMMAND_RUNNING\n" + compact + "\n"
        best = None
        for line in stream.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                candidate = json.loads(line)
            except Exception:
                continue
            if candidate.get("release") == "rel-x":
                best = candidate
        self.assertEqual(best["status"], "rolled_back")


class AgentWorktreeContractTests(unittest.TestCase):
    """The task-isolation entrypoint must keep its boundary commands wired.

    A parallel-agent workflow only works if every session can (a) discover
    where it is, (b) move unattributed in-flight changes out of the main
    workspace, and (c) see migration-number conflicts.  These are cheap
    contract checks, not a full end-to-end run.
    """

    def test_help_lists_boundary_commands(self):
        proc = run(["bash", "deploy/cloud/agent-worktree.sh", "--help"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for command in ("status", "preflight", "create", "adopt", "sync", "publish", "remove"):
            self.assertIn(command, proc.stdout)

    def test_status_reports_current_environment(self):
        proc = run(["bash", "deploy/cloud/agent-worktree.sh", "status"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("环境：", proc.stdout)

    def test_unknown_command_fails(self):
        proc = run(["bash", "deploy/cloud/agent-worktree.sh", "definitely-not-a-command"])
        self.assertNotEqual(proc.returncode, 0)


# --------------------------------------------------------------------------- #
# 门禁对象身份复用与并行门禁（release-gate-reuse）
# --------------------------------------------------------------------------- #

LIB_RELEASE_GATE = ROOT / "deploy" / "cloud" / "lib-release-gate.sh"
RELEASE_COMMIT = ROOT / "deploy" / "cloud" / "release-commit.sh"
RELEASE_TEST = ROOT / "deploy" / "cloud" / "release-test.sh"
AGENT_WORKTREE = ROOT / "deploy" / "cloud" / "agent-worktree.sh"
BROWSER_ACCEPTANCE = ROOT / "tools" / "browser_acceptance.sh"


def _git(repo, *args, check=True):
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc


def _init_repo(repo):
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")


def _commit(repo, message="c"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


class ReleaseGateIdentityTests(unittest.TestCase):
    """复用成立的四项锚定：tree / gate_impl / external / base。

    候选树是一个独立的 detached worktree（模拟 release 候选），门禁实现来自主仓
    （模拟“门禁定义来自正在运行的入口本身，而不是被测候选代码”）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = base / "repo"
        _init_repo(self.repo)
        (self.repo / "deploy" / "cloud").mkdir(parents=True)
        (self.repo / "tools").mkdir()
        (self.repo / "migrations").mkdir()
        (self.repo / "deploy" / "cloud" / "release-test.sh").write_text("v1\n", encoding="utf-8")
        (self.repo / "tools" / "x.py").write_text("t1\n", encoding="utf-8")
        (self.repo / "migrations" / "0001_a.sql").write_text("select 1;\n", encoding="utf-8")
        self.base = _commit(self.repo, "init")
        self.cand = base / "cand"
        _git(self.repo, "worktree", "add", "--detach", "-q", str(self.cand), self.base)

    def _identity_fields(self, base=None):
        base = base or self.base
        proc = subprocess.run(
            ["bash", "-c",
             f'source "{LIB_RELEASE_GATE}"; release_gate_identity "$1" "$2" "$3"',
             "_", str(self.cand), str(self.repo), base],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        key, tree, gate_impl, external, b = proc.stdout.strip().split("\t")
        return {"key": key, "tree": tree, "gate_impl": gate_impl,
                "external": external, "base": b}

    def _identity_line(self, fields):
        return "\t".join(fields[k] for k in ("key", "tree", "gate_impl", "external", "base"))

    def _register(self, fields, task="t"):
        proc = subprocess.run(
            ["bash", "-c",
             f'source "{LIB_RELEASE_GATE}"; release_gate_register "$1" "$2" "$3"',
             "_", str(self.repo / ".git"), self._identity_line(fields), task],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def _lookup(self, fields):
        proc = subprocess.run(
            ["bash", "-c",
             f'source "{LIB_RELEASE_GATE}"; release_gate_lookup "$1" "$2"',
             "_", str(self.repo / ".git"), self._identity_line(fields)],
            capture_output=True, text=True,
        )
        return proc.returncode == 0, proc.stdout

    def test_same_tree_and_base_hits(self):
        ident = self._identity_fields()
        self._register(ident)
        hit, line = self._lookup(ident)
        self.assertTrue(hit, "同一棵树 + 同一基线应命中已验收账本")
        self.assertIn("tree=", line)

    def test_base_advance_misses_even_when_tree_is_unchanged(self):
        ident = self._identity_fields()
        self._register(ident)
        _git(self.repo, "commit", "--allow-empty", "-qm", "base advances")
        advanced = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(advanced, self.base)
        moved = self._identity_fields(advanced)
        self.assertEqual(moved["tree"], ident["tree"], "树未变，只有基线前进")
        hit, _ = self._lookup(moved)
        self.assertFalse(hit, "基线前进必须回到全量")

    def test_gate_implementation_change_misses(self):
        ident = self._identity_fields()
        self._register(ident)
        # 改门禁实现（主仓 deploy/cloud）并提交，但候选树与基线参数保持不变。
        (self.repo / "deploy" / "cloud" / "release-test.sh").write_text("v2\n", encoding="utf-8")
        _commit(self.repo, "gate implementation changes")
        changed = self._identity_fields(self.base)
        self.assertEqual(changed["tree"], ident["tree"], "候选树未变")
        self.assertNotEqual(changed["gate_impl"], ident["gate_impl"], "门禁实现哈希必须变化")
        hit, _ = self._lookup(changed)
        self.assertFalse(hit, "门禁实现改动必须回到全量")

    def test_external_input_change_misses(self):
        ident = self._identity_fields()
        self._register(ident)
        # 迁移目录是显式外部输入：新增一个未跟踪迁移文件即改变 external 锚定。
        (self.cand / "migrations" / "0002_x.sql").write_text("select 2;\n", encoding="utf-8")
        changed = self._identity_fields(self.base)
        self.assertEqual(changed["tree"], ident["tree"], "未提交文件不影响 tree hash")
        self.assertNotEqual(changed["external"], ident["external"], "外部输入哈希必须变化")
        hit, _ = self._lookup(changed)
        self.assertFalse(hit, "外部输入变化必须回到全量")

    def test_missing_ledger_is_a_miss(self):
        hit, _ = self._lookup(self._identity_fields())
        self.assertFalse(hit, "没有账本记录时必须 miss（fail closed）")


class VerifiedTreeRegistrationTests(unittest.TestCase):
    """test --task 只在干净工作树上登记可复用证据；脏工作树不产生可复用证据。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = base / "repo"
        self.worktrees = base / "worktrees"
        self.worktrees.mkdir()
        _init_repo(self.repo)
        (self.repo / "README.md").write_text("repo\n", encoding="utf-8")
        cloud = self.repo / "deploy" / "cloud"
        cloud.mkdir(parents=True)
        for name in ("agent-worktree.sh", "release-env.sh",
                     "lib-release-lock.sh", "lib-release-gate.sh"):
            shutil.copy2(ROOT / "deploy" / "cloud" / name, cloud / name)
        hooks = cloud / "git-hooks"
        hooks.mkdir()
        for name in ("pre-commit", "commit-msg"):
            shutil.copy2(ROOT / "deploy" / "cloud" / "git-hooks" / name, hooks / name)
        (cloud / "release-test.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        _commit(self.repo, "init")
        self.base = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        _git(self.repo, "worktree", "add", "-q", "-b", "agent/task",
             str(self.worktrees / "task"), "main")
        self.wt = self.worktrees / "task"
        self.meta_dir = self.repo / ".git" / "trosa-tasks"
        self.meta_dir.mkdir()
        (self.meta_dir / "task.json").write_text(json.dumps({
            "task": "task", "branch": "agent/task", "path": str(self.wt),
            "status": "active",
        }), encoding="utf-8")

    def _run_test(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("TRADE_OS_")}
        env["TRADE_OS_AGENT_ROLE"] = "release"
        env["TRADE_OS_WORKTREE_ROOT"] = str(self.worktrees)
        return subprocess.run(
            ["bash", str(self.repo / "deploy" / "cloud" / "agent-worktree.sh"),
             "test", "--task", "task"],
            capture_output=True, text=True, env=env, cwd=str(self.repo), timeout=60,
        )

    def _meta(self):
        return json.loads((self.meta_dir / "task.json").read_text(encoding="utf-8"))

    def _ledger_lines(self):
        ledger = self.meta_dir / ".verified-trees"
        if not ledger.exists():
            return []
        return [line for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_clean_gate_registers_tree_and_release_side_lookup_hits(self):
        proc = self._run_test()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        head_tree = _git(self.wt, "rev-parse", "HEAD^{tree}").stdout.strip()
        meta = self._meta()
        self.assertEqual(meta.get("reusable_tree"), "1")
        self.assertEqual(meta.get("verified_tree"), head_tree)
        lines = self._ledger_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn(f"tree={head_tree}", lines[0])
        # release 侧：候选=任务树，门禁实现=主仓 deploy/cloud，base=main，应命中。
        ident = subprocess.run(
            ["bash", "-c",
             f'source "{LIB_RELEASE_GATE}"; release_gate_identity "$1" "$2" "$3"',
             "_", str(self.wt), str(self.repo), self.base],
            capture_output=True, text=True,
        )
        self.assertEqual(ident.returncode, 0, ident.stderr)
        hit = subprocess.run(
            ["bash", "-c",
             f'source "{LIB_RELEASE_GATE}"; release_gate_lookup "$1" "$2"',
             "_", str(self.repo / ".git"), ident.stdout.strip()],
            capture_output=True, text=True,
        )
        self.assertEqual(hit.returncode, 0, hit.stdout + hit.stderr)

    def test_dirty_worktree_is_not_reusable(self):
        self.assertEqual(self._run_test().returncode, 0)
        before = len(self._ledger_lines())
        (self.wt / "scratch-untracked.txt").write_text("dirty\n", encoding="utf-8")
        proc = self._run_test()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        meta = self._meta()
        self.assertEqual(meta.get("reusable_tree"), "0")
        self.assertEqual(meta.get("verified_tree"), "")
        self.assertEqual(len(self._ledger_lines()), before,
                         "脏工作树不得新增已验收树记录")


class ReleaseGateParallelRunnerTests(unittest.TestCase):
    """并行门禁：输出按分支分组、任一失败仍等待另一支并向上返回失败。"""

    def _run(self, fail_second):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        second = "exit 7" if fail_second else "echo B-end"
        script = f'''
set -euo pipefail
source "{LIB_RELEASE_GATE}"
a() {{ echo A-start; sleep 0.2; echo A-end; }}
b() {{ echo B-start; {second}; }}
rc=0
release_gate_run_parallel "$1" a b || rc=$?
echo "RC=$rc"
'''
        return subprocess.run(["bash", "-c", script, "_", tmp.name],
                              capture_output=True, text=True, timeout=30)

    def test_both_branches_run_and_succeed(self):
        proc = self._run(False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=0", proc.stdout)
        # 输出分组：A 分支完整结束后才打印 B 分支，不交错。
        self.assertLess(proc.stdout.index("A-end"), proc.stdout.index("分支 b 输出"))

    def test_failure_waits_for_the_other_branch_and_reports_failure(self):
        proc = self._run(True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=1", proc.stdout)
        self.assertIn("A-end", proc.stdout, "失败时仍应等待另一支结束，不遗留后台进程")


class CommitGateEnforcementTests(unittest.TestCase):
    """release-commit.sh --commit 与 --branch 使用同一完成证据判定。"""

    HARNESS = r"""
set -euo pipefail
export LC_ALL=C
fail() { printf '发布被拒绝：%s\n' "$*" >&2; exit 1; }
GIT_COMMON_DIR="__COMMON__"
BASE_SHA="$1"
TARGET_BRANCH=main
DRY_RUN="$4"
__FUNCTION__
enforce_agent_commit_ready "$2" "$3"
echo ALLOW
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        _init_repo(self.repo)
        (self.repo / "f.txt").write_text("base\n", encoding="utf-8")
        self.base = _commit(self.repo, "init")
        _git(self.repo, "checkout", "-q", "-b", "agent/task")
        (self.repo / "f.txt").write_text("task\n", encoding="utf-8")
        self.tip = _commit(self.repo, "task work")
        self.meta_dir = self.repo / ".git" / "trosa-tasks"
        self.meta_dir.mkdir()
        self._write_meta(status="active", verify="ok", verified=self.tip)

    def _write_meta(self, status, verify, verified):
        (self.meta_dir / "task.json").write_text(
            json.dumps({"task": "task", "status": status,
                        "verify_result": verify, "verified_commit": verified}),
            encoding="utf-8",
        )

    def _run(self, sha=None, dry_run=0):
        script = RELEASE_COMMIT.read_text(encoding="utf-8")
        start = script.index("enforce_agent_commit_ready() {")
        end = script.index("\n}\n", start) + len("\n}\n")
        program = (
            self.HARNESS
            .replace("__COMMON__", str(self.repo / ".git"))
            .replace("__FUNCTION__", script[start:end])
        )
        return subprocess.run(
            ["bash", "-c", program, "_", self.base, "deadbeef", sha or self.tip, str(dry_run)],
            capture_output=True, text=True, cwd=str(self.repo),
        )

    def test_ready_commit_allows(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ALLOW", proc.stdout)

    def test_missing_evidence_is_refused(self):
        (self.meta_dir / "task.json").unlink()
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("找不到对应的完成证据", proc.stderr)

    def test_failed_gate_is_refused(self):
        self._write_meta(status="active", verify="failed", verified=self.tip)
        self.assertNotEqual(self._run().returncode, 0)

    def test_commit_behind_base_is_refused(self):
        _git(self.repo, "checkout", "-q", "main")
        (self.repo / "new.txt").write_text("move\n", encoding="utf-8")
        new_base = _commit(self.repo, "main moves")
        _git(self.repo, "checkout", "-q", "agent/task")
        # 用新的 base 重新运行 harness：task tip 不再包含最新 main。
        script = RELEASE_COMMIT.read_text(encoding="utf-8")
        start = script.index("enforce_agent_commit_ready() {")
        end = script.index("\n}\n", start) + len("\n}\n")
        program = (self.HARNESS
                   .replace("__COMMON__", str(self.repo / ".git"))
                   .replace("__FUNCTION__", script[start:end]))
        proc = subprocess.run(
            ["bash", "-c", program, "_", new_base, "deadbeef", self.tip, "0"],
            capture_output=True, text=True, cwd=str(self.repo),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("未基于最新", proc.stderr)

    def test_dry_run_does_not_enforce(self):
        self._write_meta(status="active", verify="", verified="")
        proc = self._run(dry_run=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class ReleaseGateReuseContractTests(unittest.TestCase):
    """门禁执行结构、复用锚定与 fail-closed 入口必须保持接线。"""

    def test_release_test_runs_independent_branches_in_parallel(self):
        text = RELEASE_TEST.read_text(encoding="utf-8")
        for token in (
            "release_gate_run_parallel",
            "run_python_regression_branch",
            "run_rehearsal_browser_branch",
            "run_extension_branch",
            "RELEASE_GATE_PARALLEL_PIDS",
            "TROSA_BROWSER_ACCEPTANCE_REUSE_REHEARSAL=1",
            "REHEARSAL_GATE_PORT",
        ):
            self.assertIn(token, text, token)

    def test_browser_acceptance_reuses_gate_owned_rehearsal(self):
        text = BROWSER_ACCEPTANCE.read_text(encoding="utf-8")
        for token in ("TROSA_BROWSER_ACCEPTANCE_REUSE_REHEARSAL",
                      "STOP_REHEARSAL_ON_EXIT",
                      "不会把浏览器验收标记为 SKIP"):
            self.assertIn(token, text, token)

    def test_release_commit_reuses_verified_tree_and_keeps_quick_check(self):
        text = RELEASE_COMMIT.read_text(encoding="utf-8")
        for token in ("release_gate_lookup", "gate reused for tree=",
                      'release-test.sh" --quick --dir', "enforce_agent_commit_ready",
                      "release_gate_identity"):
            self.assertIn(token, text, token)

    def test_shared_lib_exposes_identity_ledger_and_runner(self):
        text = LIB_RELEASE_GATE.read_text(encoding="utf-8")
        for token in ("release_gate_identity", "release_gate_impl_hash",
                      "release_gate_external_hash", "release_gate_register",
                      "release_gate_lookup", "release_gate_run_parallel",
                      ".verified-trees"):
            self.assertIn(token, text, token)

    def test_agent_worktree_only_registers_clean_trees(self):
        text = AGENT_WORKTREE.read_text(encoding="utf-8")
        self.assertIn("register_verified_tree", text)
        self.assertIn("--untracked-files=all", text)
        self.assertIn("reusable_tree=1", text)
        self.assertIn("reusable_tree=0", text)


if __name__ == "__main__":
    unittest.main()
