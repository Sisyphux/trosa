"""Backup transfer hard-dependency removal regressions.

The behavior this suite protects:

* a database-sensitive release still requires a reliable, checksum-verified,
  restorable backup before production changes;
* that authoritative backup lives in the cloud (ECS durable snapshot, with an
  optional OSS mirror) and is the release gate;
* downloading the archive to the Mac is optional and a transfer failure
  (workbench download / scp / SSH file stream) NEVER blocks the release;
* failures are distinguishable: backup_failed vs backup_verification_failed vs
  code-release failure vs local_download_failed.

Everything here is offline: ECS is replaced by stubs and a fake PostgreSQL
backup runner.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP_REMOTE = ROOT / "deploy" / "cloud" / "backup-remote.sh"
BACKUP_WORKBENCH = ROOT / "deploy" / "cloud" / "backup-workbench.sh"
RELEASE_REMOTE = ROOT / "deploy" / "cloud" / "release-remote.sh"
RELEASE_COMMIT = ROOT / "deploy" / "cloud" / "release-commit.sh"
TROSA_RELEASE = ROOT / "deploy" / "cloud" / "trosa-release"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def clean_env(**extra) -> dict:
    base = {k: v for k, v in os.environ.items() if not k.startswith("TRADE_OS_")}
    base.update(extra)
    return base


def write_exec(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def backup_json_line(proc: subprocess.CompletedProcess) -> dict | None:
    for line in proc.stdout.splitlines():
        if line.startswith("TROSA_BACKUP_JSON "):
            return json.loads(line[len("TROSA_BACKUP_JSON "):])
    return None


def make_fake_pg(root: Path, *, sha_body: str = "sha256") -> Path:
    """Create a fake $TRADE_OS_POSTGRES_ROOT with a stub backup.sh.

    ``sha_body`` selects what the stub reports: ``sha256`` (correct),
    ``wrong`` (mismatched checksum), or ``none`` (missing lines).
    """
    pg = root / "pg"
    pg.mkdir(parents=True, exist_ok=True)
    if sha_body == "none":
        body = "#!/usr/bin/env bash\nprintf 'backup=backups/x.dump\\n'\n"
    else:
        bad = "deadbeef" if sha_body == "wrong" else ""
        body = (
            "#!/usr/bin/env bash\n"
            "mkdir -p backups\n"
            "dump=backups/x.dump\n"
            "printf 'DUMPDATA' > \"$dump\"\n"
            "if command -v sha256sum >/dev/null 2>&1; then "
            "sha=$(sha256sum \"$dump\" | awk '{print $1}'); else "
            "sha=$(shasum -a 256 \"$dump\" | awk '{print $1}'); fi\n"
            f"if [ -n '{bad}' ]; then sha='{bad}'; fi\n"
            "printf 'backup=%s\\n' \"$dump\"\n"
            "printf 'sha256=%s\\n' \"$sha\"\n"
        )
    write_exec(pg / "backup.sh", body)
    return pg


def run_backup_remote(pg: Path, backup_root: Path, restore_cmd: str,
                      **env) -> subprocess.CompletedProcess:
    extra = {
        "TRADE_OS_POSTGRES_ROOT": str(pg),
        "TRADE_OS_RELEASE_BACKUP_ROOT": str(backup_root),
        "TRADE_OS_PG_RESTORE_LIST_CMD": restore_cmd,
    }
    extra.update(env)
    return subprocess.run(
        ["bash", str(BACKUP_REMOTE), "rel-test-1"],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT),
        env=clean_env(**extra),
    )


class BackupRemoteTests(unittest.TestCase):
    """backup-remote.sh: verified cloud backup, no download dependency."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="trosa-backup-remote-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.backup_root = self.tmp / "release-backups"

    def test_verified_snapshot_is_durable_and_restorable(self):
        pg = make_fake_pg(self.tmp)
        proc = run_backup_remote(pg, self.backup_root, "true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = backup_json_line(proc)
        self.assertIsNotNone(doc, proc.stdout)
        self.assertEqual(doc["status"], "ok")
        self.assertEqual(doc["failure_class"], None)
        self.assertEqual(doc["storage"], "ecs-durable")
        self.assertTrue(doc["verified"])
        self.assertTrue(doc["restorable"])
        self.assertGreater(doc["size_bytes"], 0)
        snap = Path(doc["path"])
        self.assertTrue(snap.is_file())
        self.assertEqual(snap.parent.name, "rel-test-1")

    def test_missing_backup_runner_is_backup_failed(self):
        empty = self.tmp / "no-runner"
        empty.mkdir()
        proc = run_backup_remote(empty, self.backup_root, "true")
        self.assertEqual(proc.returncode, 10, proc.stderr)
        doc = backup_json_line(proc)
        self.assertEqual(doc["failure_class"], "backup_failed")

    def test_checksum_mismatch_is_verification_failed(self):
        pg = make_fake_pg(self.tmp, sha_body="wrong")
        proc = run_backup_remote(pg, self.backup_root, "true")
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(backup_json_line(proc)["failure_class"],
                         "backup_verification_failed")

    def test_non_restorable_dump_is_verification_failed(self):
        pg = make_fake_pg(self.tmp)
        proc = run_backup_remote(pg, self.backup_root, "false")
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(backup_json_line(proc)["failure_class"],
                         "backup_verification_failed")

    def test_oss_configured_without_uploader_fails_closed(self):
        pg = make_fake_pg(self.tmp)
        proc = run_backup_remote(
            pg, self.backup_root, "true",
            TRADE_OS_BACKUP_OSS_URI="oss://bucket/prefix")
        self.assertEqual(proc.returncode, 12, proc.stderr)
        self.assertEqual(backup_json_line(proc)["failure_class"],
                         "backup_verification_failed")

    def test_oss_mirror_self_report_must_match(self):
        pg = make_fake_pg(self.tmp)
        proc = run_backup_remote(
            pg, self.backup_root, "true",
            TRADE_OS_BACKUP_OSS_URI="oss://bucket/prefix",
            TRADE_OS_BACKUP_UPLOAD_CMD="printf 'size=1\\nsha256=deadbeef\\n'")
        self.assertEqual(proc.returncode, 12, proc.stderr)

    def test_oss_mirror_success_is_recorded(self):
        pg = make_fake_pg(self.tmp)
        cmd = ("printf 'oss_uri=%s\\n' {remote}; "
               "printf 'size=%s\\n' \"$(stat -c%s {local} 2>/dev/null || stat -f%z {local})\"; "
               "if command -v sha256sum >/dev/null 2>&1; then "
               "printf 'sha256=%s\\n' \"$(sha256sum {local} | awk '{print $1}')\"; "
               "else printf 'sha256=%s\\n' \"$(shasum -a 256 {local} | awk '{print $1}')\"; fi")
        proc = run_backup_remote(
            pg, self.backup_root, "true",
            TRADE_OS_BACKUP_OSS_URI="oss://bucket/prefix",
            TRADE_OS_BACKUP_UPLOAD_CMD=cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = backup_json_line(proc)
        self.assertEqual(doc["storage"], "ecs-durable+oss")
        self.assertTrue(doc["oss"]["verified"])
        self.assertTrue(doc["oss"]["uri"].startswith("oss://bucket/prefix/"))


class BackupWorkbenchTests(unittest.TestCase):
    """backup-workbench.sh: cloud gate first, local download optional."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="trosa-backup-wb-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.env_file = self.tmp / "workbench.env"
        self.env_file.write_text(
            "TRADE_OS_ECS_REGION=test-region\n"
            "TRADE_OS_ECS_INSTANCE_ID=i-test\n"
            "TRADE_OS_SSH_HOST=stub-host\n",
            encoding="utf-8")
        self.local_dir = self.tmp / "local"
        self.local_dir.mkdir()
        self.runner = write_exec(self.tmp / "runner.sh", "#!/usr/bin/env bash\nexit 0\n")

    def wb(self, *args, runner_body: str, fetch_body: str | None = None,
           extra_env: dict | None = None):
        write_exec(self.runner, "#!/usr/bin/env bash\n" + runner_body)
        opts = {
            "TRADE_OS_WORKBENCH_ENV": str(self.env_file),
            "TRADE_OS_BACKUP_RUNNER": str(self.runner),
            "TRADE_OS_LOCAL_BACKUP_DIR": str(self.local_dir),
            "TRADE_OS_BACKUP_TRANSFER": "auto",
        }
        opts.update(extra_env or {})
        env = clean_env(**opts)
        if fetch_body is not None:
            fetch = write_exec(self.tmp / "fetch.sh", "#!/usr/bin/env bash\n" + fetch_body)
            env["TRADE_OS_BACKUP_FETCH"] = str(fetch)
        return subprocess.run(
            ["bash", str(BACKUP_WORKBENCH), *args],
            capture_output=True, text=True, timeout=60, cwd=str(ROOT), env=env)

    def test_cloud_backup_failure_aborts_with_class(self):
        proc = self.wb(
            runner_body="printf 'TROSA_BACKUP_JSON {\"status\":\"failed\","
                        "\"failure_class\":\"backup_verification_failed\"}\\n'\n"
                        "exit 11\n")
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertIn("TROSA_BACKUP_STATUS backup_verification_failed", proc.stderr)

    def test_cloud_only_never_downloads(self):
        proc = self.wb(
            "--cloud-only",
            runner_body="printf 'TROSA_BACKUP_JSON {\"status\":\"ok\"}\\n'\n"
                        "printf 'SHA256=abc\\n'\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("TROSA_BACKUP_STATUS ok", proc.stdout)
        self.assertIn("跳过本地下载", proc.stdout)

    def test_download_failure_is_local_only_and_distinct(self):
        proc = self.wb(
            "--download=auto",
            runner_body="printf 'TROSA_BACKUP_JSON {\"status\":\"ok\"}\\n'\n"
                        "printf 'ARCHIVE=/tmp/x.tar.gz\\n'\n"
                        "printf 'SHA256=abc\\n'\n",
            fetch_body="exit 1\n")
        self.assertEqual(proc.returncode, 20, proc.stderr)
        self.assertIn("TROSA_BACKUP_STATUS ok", proc.stdout)
        self.assertIn("TROSA_LOCAL_ARCHIVE local_download_failed", proc.stderr)

    def test_download_success_verifies_local_checksum(self):
        archive = self.tmp / "src.tar.gz"
        import tarfile
        with tarfile.open(archive, "w:gz") as tar:
            data = self.tmp / "payload.txt"
            data.write_text("hello", encoding="utf-8")
            tar.add(str(data), arcname="payload.txt")
        sha = subprocess.run(["shasum", "-a", "256", str(archive)],
                             capture_output=True, text=True).stdout.split()[0]
        proc = self.wb(
            "--download=auto",
            runner_body=(
                "printf 'TROSA_BACKUP_JSON {\"status\":\"ok\"}\\n'\n"
                f"printf 'SHA256={sha}\\n'\n"
                "printf 'ARCHIVE=/tmp/trosa-postgres-backup-X.tar.gz\\n'\n"),
            fetch_body=f'cp "{archive}" "$2/$(basename "$1")"\n',
            extra_env={"TRADE_OS_BACKUP_FETCH_ARCHIVE": str(archive)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("TROSA_LOCAL_ARCHIVE ok", proc.stdout)

    def test_hanging_download_is_bounded_and_local_only(self):
        # A transport that hangs must not stall the release: the attempt is
        # killed by the watchdog and classified local_download_failed.
        proc = self.wb(
            "--download=auto",
            runner_body="printf 'TROSA_BACKUP_JSON {\"status\":\"ok\"}\\n'\n"
                        "printf 'SHA256=abc\\n'\n",
            fetch_body="exec sleep 30\n",
            extra_env={"TRADE_OS_BACKUP_DOWNLOAD_TIMEOUT": "2",
                       "TRADE_OS_BACKUP_TRANSFER": "ssh",
                       "TRADE_OS_SSH_HOST": "stub-host"})
        self.assertEqual(proc.returncode, 20, proc.stderr)
        self.assertIn("TROSA_BACKUP_STATUS ok", proc.stdout)
        self.assertIn("TROSA_LOCAL_ARCHIVE local_download_failed", proc.stderr)

    def test_download_checksum_mismatch_is_local_only(self):
        proc = self.wb(
            "--download=auto",
            runner_body="printf 'TROSA_BACKUP_JSON {\"status\":\"ok\"}\\n'\n"
                        "printf 'SHA256=expected\\n'\n",
            fetch_body='printf notanarchive > "$2/$(basename "$1")"\n')
        self.assertEqual(proc.returncode, 20, proc.stderr)
        self.assertIn("TROSA_BACKUP_STATUS ok", proc.stdout)


class BackupHardDependencyContractTests(unittest.TestCase):
    """Static contracts: the release gate never depends on a Mac download."""

    def test_release_remote_uses_cloud_helper_not_workbench_download(self):
        text = read(RELEASE_REMOTE)
        self.assertIn("backup-remote.sh", text)
        self.assertNotIn("backup-workbench.sh", text)

    def test_release_remote_classifies_failures(self):
        text = read(RELEASE_REMOTE)
        for token in ("classify_failure", "backup_failed",
                      "backup_verification_failed", "release_failed",
                      'backup) printf \'backup_failed\''):
            self.assertIn(token, text)
        # The result contract exposes the class to clients.
        self.assertIn('"failure_class"', text)

    def test_release_commit_no_longer_gates_on_local_backup(self):
        text = read(RELEASE_COMMIT)
        self.assertNotIn("数据库敏感改动本地备份", text)
        self.assertIn("--require-local-backup", text)
        self.assertIn("TROSA_RELEASE_LOCAL_ARCHIVE local_download_failed", text)
        # The authoritative cloud backup is still required before publish:
        self.assertIn("release-remote.sh", text + read(RELEASE_REMOTE))

    def test_client_exposes_failure_class(self):
        self.assertIn("TROSA_RELEASE_FAILURE_CLASS", read(TROSA_RELEASE))
        self.assertIn('"failure_class"', read(TROSA_RELEASE))

    def test_classify_failure_mapping(self):
        text = read(RELEASE_REMOTE)
        start = text.index("classify_failure() {")
        end = text.index("\n}\n", start) + len("\n}\n")
        fn = text[start:end]
        cases = [
            ("failed", "backup", "backup_failed"),
            ("failed", "backup_verify", "backup_verification_failed"),
            ("failed", "backup_store", "backup_verification_failed"),
            ("failed", "migrate", "release_failed"),
            ("failed", "health", "release_failed"),
            ("success", "done", ""),
            ("refused", "baseline", "release_refused"),
            ("busy", "lock", "release_busy"),
        ]
        for status, phase, expected in cases:
            proc = subprocess.run(
                ["bash", "-c", fn + f"\nclassify_failure {status} {phase}"],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.stdout, expected, (status, phase, proc.stderr))

    def test_write_result_embeds_valid_python_for_failure_class(self):
        # Regression: the failure_class value must be embedded as a Python
        # literal. Embedding JSON ``null`` into the heredoc made the runner
        # produce a 0-byte DEPLOY_RESULT.json, so polling never resolved.
        text = read(RELEASE_REMOTE)

        def extract(name: str) -> str:
            start = text.index(f"{name}() {{")
            # The function body may contain a bare ``}`` (the Python result
            # dict), so terminate on a function end followed by a blank line.
            end = text.index("\n}\n\n", start)
            return text[start:end + len("\n}\n")]

        classify = extract("classify_failure")
        write = extract("write_result")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "wr.sh"
            script.write_text(
                "set -uo pipefail\n"
                f"RESULT_FILE='{tmp}/result.json'\n"
                f"LAST_RESULT='{tmp}/last.json'\n"
                f"STATE_FILE='{tmp}/state.json'\n"
                f"RELEASE_DIR='{tmp}'\n"
                f"REMOTE_ROOT='{tmp}'\n"
                "printf '{}' > \"$STATE_FILE\"\n"
                "RELEASE_ID=rel-x; COMMIT_SHA=abc; MODE=deploy\n"
                "MIRROR_LAST_RESULT=1; FAILURE_CLASS=\"\"\n"
                "NOW() { date -u +%Y-%m-%dT%H:%M:%SZ; }\n"
                "atomic_write() { cat > \"$1\"; }\n"
                "read_current_release() { printf 'none'; }\n"
                "release_commit() { printf 'unknown'; }\n"
                "append_ledger() { :; }\n"
                + classify + write +
                "write_result success done\n"
                "FAILURE_CLASS='backup_failed'\n"
                "write_result failed backup\n"
                "cat \"$RESULT_FILE\"\n",
                encoding="utf-8")
            proc = subprocess.run(["bash", str(script)], capture_output=True,
                                  text=True, timeout=30, cwd=str(ROOT))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            doc = json.loads(Path(tmp, "result.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["status"], "failed")
            self.assertEqual(doc["failure_class"], "backup_failed")

    def test_backup_remote_is_shell_valid(self):
        proc = subprocess.run(["bash", "-n", str(BACKUP_REMOTE)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
