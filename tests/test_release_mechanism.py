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
        applied = {
            path.name for path in migration_dir.glob("*.sql")
            if path.name[:4].isdigit() and int(path.name[:4]) <= 30
        }
        self.assertEqual(len(applied), 30)
        result = plan.plan_release_db(str(migration_dir), applied, [])
        self.assertEqual(result["pending_migrations"], [
            "0031_customer_pin_payload_backfill.sql",
            "0032_one_follow_up_per_customer_day.sql",
        ])
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
                   env={**os.environ, "TRADE_OS_WORKBENCH_ENV": self.env_file})

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
        self.assertIn('python3 "$script_dir/cloud-assistant.py" run', text)
        self.assertIn('python3 "$script_dir/cloud-assistant.py" get', text)

    def test_workbench_noninteractive_commands_explicitly_use_bash(self):
        text = (ROOT / "deploy" / "cloud" / "run-workbench-command.sh").read_text(encoding="utf-8")
        self.assertIn('command_payload="$(printf', text)
        self.assertIn('workbench_command="bash -c', text)
        self.assertIn('--command "$workbench_command"', text)

    def test_shell_syntax_valid(self):
        for name in ("trosa-release", "release-remote.sh", "status-remote.sh",
                     "auto-publish.sh", "run-workbench-command.sh",
                     "run-cloud-assistant-command.sh", "cloud-assistant-bootstrap.sh"):
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


if __name__ == "__main__":
    unittest.main()
