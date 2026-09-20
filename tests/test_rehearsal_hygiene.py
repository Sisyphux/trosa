"""Unit tests for ``tools/rehearsal_hygiene.py``.

These tests never touch real processes: the scan pipeline is driven with a
synthetic ``ps`` table and a temporary ``$TMPDIR``.  The only system interaction
is removed via injected ``port_lookup``, ``run`` and ``remove_dir`` callables.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import rehearsal_hygiene as hygiene  # noqa: E402


def proc(pid, ppid, etime, command):
    from rehearsal_hygiene import ProcessInfo  # local import keeps names tidy
    return ProcessInfo(pid=pid, ppid=ppid, etime_seconds=etime, command=command)


class EtimeTests(unittest.TestCase):
    def test_parses_all_ps_shapes(self):
        self.assertEqual(hygiene.parse_etime("05:03"), 303)
        self.assertEqual(hygiene.parse_etime("01:02:03"), 3723)
        self.assertEqual(hygiene.parse_etime("2-01:02:03"), 2 * 86400 + 3723)
        self.assertEqual(hygiene.parse_etime("garbage"), 0)
        self.assertEqual(hygiene.parse_etime(""), 0)


class ProcessParsingTests(unittest.TestCase):
    PS_OUTPUT = (
        "  101     1 01:00:00 /usr/bin/python /repo/serve_rehearsal.py\n"
        "  202   101 00:10 /bin/zsh /repo/deploy/cloud/release-test.sh\n"
        "bad line\n"
        "  303     1 00:00:30 /opt/pg/bin/postgres -D /repo/.local/postgres-rehearsal/data -p 55432\n"
    )

    def test_parses_and_keeps_spaced_commands(self):
        processes = hygiene.parse_ps_output(self.PS_OUTPUT)
        self.assertEqual([p.pid for p in processes], [101, 202, 303])
        self.assertEqual(processes[0].command, "/usr/bin/python /repo/serve_rehearsal.py")
        self.assertEqual(processes[1].ppid, 101)

    def test_web_process_detection(self):
        web = proc(1, 1, 60, "/usr/bin/python /repo/serve_rehearsal.py")
        self.assertTrue(hygiene.is_rehearsal_web_process(web))
        self.assertEqual(hygiene.rehearsal_web_tree(web), "/repo")

        compile_only = proc(2, 1, 1, "python -m py_compile app.py serve_rehearsal.py")
        self.assertFalse(hygiene.is_rehearsal_web_process(compile_only))

        unittest_run = proc(3, 1, 1, "python -m unittest tests.test_runtime_contract serve_rehearsal")
        self.assertFalse(hygiene.is_rehearsal_web_process(unittest_run))

    def test_postgres_master_detection_and_fields(self):
        master = proc(10, 1, 100, "/opt/pg/bin/postgres -D /repo/.local/postgres-rehearsal/data -p 55432")
        self.assertTrue(hygiene.is_rehearsal_postgres_process(master))
        self.assertEqual(hygiene.postgres_rehearsal_data_dir(master), "/repo/.local/postgres-rehearsal/data")
        self.assertEqual(hygiene.postgres_rehearsal_port(master), 55432)

        child = proc(11, 10, 100, "postgres: checkpointer")
        self.assertFalse(hygiene.is_rehearsal_postgres_process(child))

        unrelated = proc(12, 1, 100, "/opt/pg/bin/postgres -D /var/lib/postgresql/data -p 5432")
        self.assertFalse(hygiene.is_rehearsal_postgres_process(unrelated))

    def test_web_classification_uses_live_parent(self):
        orphan = proc(100, 1, 60, "/python /repo/serve_rehearsal.py")
        self.assertEqual(hygiene.classify_web_process(orphan, {}), "orphan")

        active = proc(200, 199, 60, "/python /repo/serve_rehearsal.py")
        parent = proc(199, 1, 60, "/bin/zsh /repo/deploy/cloud/release-test.sh")
        by_pid = {199: parent}
        self.assertEqual(hygiene.classify_web_process(active, by_pid), "active")

        missing_parent = proc(300, 299, 60, "/python /repo/serve_rehearsal.py")
        self.assertEqual(hygiene.classify_web_process(missing_parent, {}), "orphan")


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self.tmp.name
        self.now = 1_000_000.0
        # Two stale artifacts and one referenced artifact.
        self.stale_dir = os.path.join(self.tmpdir, "trosa-release-test.ABC123")
        os.makedirs(self.stale_dir)
        self.socket_dir = os.path.join(self.tmpdir, "trosa-pg-deadbeef")
        os.makedirs(self.socket_dir)
        self.live_dir = os.path.join(self.tmpdir, "trosa-release-gate.LIVE")
        os.makedirs(self.live_dir)
        old = self.now - 7200
        for path in (self.stale_dir, self.socket_dir, self.live_dir):
            os.utime(path, (old, old))

    def tearDown(self):
        self.tmp.cleanup()

    def _scan(self, processes):
        return hygiene.scan(
            processes=processes,
            tmpdir=self.tmpdir,
            now=self.now,
            min_process_age=0,
            min_temp_age=60,
            port_lookup=lambda pid: {100: 51234}.get(pid),
            self_pid=999999,
        )

    def test_orphan_web_process_is_reported(self):
        processes = [
            proc(100, 1, 120, "/python /repo/serve_rehearsal.py"),
            proc(999999, 1, 1, "/python hygiene"),
        ]
        report = self._scan(processes)
        self.assertEqual(report["summary"]["web_orphans"], 1)
        self.assertEqual(report["web"][0]["port"], 51234)
        self.assertFalse(report["gate_running"])

    def test_active_web_process_is_not_orphan(self):
        processes = [
            proc(100, 200, 120, "/python /repo/serve_rehearsal.py"),
            proc(200, 1, 120, "/bin/zsh /repo/deploy/cloud/release-test.sh"),
        ]
        report = self._scan(processes)
        self.assertEqual(report["summary"]["web_orphans"], 0)
        self.assertEqual(report["summary"]["web_active"], 1)
        self.assertTrue(report["gate_running"])

    def test_temp_classification_referenced_vs_stale(self):
        processes = [
            proc(100, 1, 120, f"/bin/zsh -c cd {self.live_dir} && run"),
        ]
        report = self._scan(processes)
        by_path = {item["path"]: item["status"] for item in report["temp"]}
        self.assertEqual(by_path[self.stale_dir], "stale")
        self.assertEqual(by_path[self.socket_dir], "stale")
        self.assertEqual(by_path[self.live_dir], "referenced")

    def test_recent_temp_is_not_stale(self):
        fresh = os.path.join(self.tmpdir, "trosa-release-test.FRESH")
        os.makedirs(fresh)
        report = self._scan([])
        by_path = {item["path"]: item["status"] for item in report["temp"]}
        self.assertEqual(by_path[fresh], "recent")

    def test_temp_referenced_only_in_environment_is_not_stale(self):
        # Simulates a live gate whose CRM_DB_PATH/SERVICE_LOG points at the dir.
        report = hygiene.scan(
            processes=[],
            tmpdir=self.tmpdir,
            now=self.now,
            min_process_age=0,
            min_temp_age=60,
            port_lookup=lambda pid: None,
            self_pid=999999,
            environ_text=f"CRM_DB_PATH={self.live_dir}\n",
        )
        by_path = {item["path"]: item["status"] for item in report["temp"]}
        self.assertEqual(by_path[self.live_dir], "referenced")


class CleanPlanTests(unittest.TestCase):
    def setUp(self):
        self.report = {
            "web": [
                {"pid": 100, "ppid": 1, "tree": "/repo", "age_seconds": 300, "port": 1234, "status": "orphan"},
                {"pid": 200, "ppid": 1, "tree": "/repo", "age_seconds": 300, "port": 1235, "status": "active"},
            ],
            "postgres": [
                {"pid": 300, "tree": "/repo", "data_dir": "/repo/.local/postgres-rehearsal/data",
                 "port": 55432, "age_seconds": 300, "status": "idle"},
            ],
            "temp": [
                {"path": "/tmp/trosa-release-test.X", "kind": "dir", "purpose": "python regression data dir",
                 "age_seconds": 7200, "referenced_by": [], "status": "stale"},
                {"path": "/tmp/trosa-release.Y", "kind": "worktree", "purpose": "release worktree",
                 "age_seconds": 7200, "referenced_by": [], "status": "stale"},
                {"path": "/tmp/trosa-pg-Z", "kind": "dir", "purpose": "rehearsal pg socket dir",
                 "age_seconds": 7200, "referenced_by": [], "status": "stale"},
            ],
        }

    def test_plan_reclaims_orphans_and_stale_but_not_active(self):
        plan = hygiene.build_clean_plan(self.report)
        targets = {action["target"] for action in plan}
        self.assertIn("100", targets)
        self.assertNotIn("200", targets)
        self.assertIn("/tmp/trosa-release-test.X", targets)
        self.assertIn("/tmp/trosa-pg-Z", targets)
        # Worktrees are skipped without --include-worktrees.
        self.assertNotIn("/tmp/trosa-release.Y", targets)
        # PostgreSQL is skipped without --include-postgres.
        self.assertFalse(any(action["action"] == "stop-postgres" for action in plan))

    def test_plan_includes_opt_in_resources(self):
        plan = hygiene.build_clean_plan(self.report, include_postgres=True, include_worktrees=True)
        actions = {action["action"] for action in plan}
        self.assertIn("stop-postgres", actions)
        self.assertIn("remove", actions)
        targets = {action["target"] for action in plan}
        self.assertIn("/tmp/trosa-release.Y", targets)

    def test_plan_respects_min_process_age(self):
        report = {
            "web": [{"pid": 100, "ppid": 1, "tree": "/repo", "age_seconds": 3, "port": None, "status": "orphan"}],
            "postgres": [],
            "temp": [],
        }
        plan = hygiene.build_clean_plan(report, min_process_age=60)
        self.assertEqual(plan, [])

    def test_apply_kill_reports_missing_pid(self):
        actions = [{"action": "kill", "kind": "web", "target": "2100000000", "detail": ""}]
        results = hygiene.apply_clean_plan(actions)
        self.assertEqual(results[0]["outcome"], "already-gone")

    def test_apply_remove_uses_injected_remover(self):
        removed: list[str] = []
        actions = [
            {"action": "remove", "kind": "dir", "target": "/tmp/trosa-pg-1", "detail": ""},
        ]
        results = hygiene.apply_clean_plan(actions, remove_dir=removed.append)
        self.assertEqual(removed, ["/tmp/trosa-pg-1"])
        self.assertEqual(results[0]["outcome"], "done")


class CliTests(unittest.TestCase):
    def test_parser_exposes_expected_commands(self):
        parser = hygiene.build_parser()
        for command in ("scan", "clean", "watch"):
            args = parser.parse_args([command])
            self.assertTrue(callable(args.func))

    def test_clean_defaults_to_dry_run(self):
        args = hygiene.build_parser().parse_args(["clean"])
        self.assertFalse(args.apply)
        self.assertFalse(args.include_postgres)


class BrowserAcceptanceSourceTests(unittest.TestCase):
    """The service launcher must exec so SERVICE_PID is the real server."""

    def test_service_is_started_with_exec(self):
        source = (ROOT / "tools" / "browser_acceptance.sh").read_text(encoding="utf-8")
        self.assertIn("exec env", source)
        self.assertIn("SERVICE_PID=$!", source)
        self.assertIn('kill "$SERVICE_PID"', source)

    def test_startup_reaps_prior_orphans_only(self):
        source = (ROOT / "tools" / "browser_acceptance.sh").read_text(encoding="utf-8")
        self.assertIn("tools/rehearsal_hygiene.py", source)
        self.assertIn("--orphans-only", source)


if __name__ == "__main__":
    unittest.main()
