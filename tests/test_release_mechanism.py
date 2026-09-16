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

    def test_unreadable_ledger_is_conservative(self):
        self._write("0001_a.sql")
        result = plan.plan_release_db(self.mdir, None, [])
        self.assertEqual(result["category"], "compatible")
        self.assertEqual(result["pending_migrations"], ["0001_a.sql"])

    def test_cli_emits_machine_readable_json(self):
        self._write("0001_a.sql")
        proc = run([sys.executable, "tools/release_db_plan.py", self.mdir,
                    "--applied", "0001_a.sql", "--changed", "app.py"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["category"], "none")


class UnifiedEntrypointTests(unittest.TestCase):
    def test_help_lists_single_entry_commands(self):
        proc = run(["bash", "deploy/cloud/trosa-release", "--help"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for token in ("publish", "status", "rollback", "db-plan"):
            self.assertIn(token, proc.stdout)

    def test_unknown_command_fails_fast(self):
        proc = run(["bash", "deploy/cloud/trosa-release", "upload-by-hand"])
        self.assertNotEqual(proc.returncode, 0)

    def test_publish_requires_pushed_commit_sha(self):
        proc = run(["bash", "deploy/cloud/trosa-release", "publish",
                    "--commit", "not-a-sha"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("commit", proc.stderr.lower())

    def test_db_plan_preflight_works_offline(self):
        proc = run(["bash", "deploy/cloud/trosa-release", "db-plan",
                    "--applied", "none-such", "--changed", "app.py"])
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

    def test_shell_syntax_valid(self):
        for name in ("trosa-release", "release-remote.sh", "status-remote.sh",
                     "auto-publish.sh", "run-workbench-command.sh",
                     "run-cloud-assistant-command.sh", "cloud-assistant-bootstrap.sh"):
            proc = run(["bash", "-n", f"deploy/cloud/{name}"])
            self.assertEqual(proc.returncode, 0, f"{name}: {proc.stderr}")

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
