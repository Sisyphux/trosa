"""网络异常模拟验收：发布链路在 SSH 传输 / GitHub API 故障下的行为。

验收目标（不触网、不碰生产）：

* SSH 会话建立前失败（主机不可达、拒绝连接、认证失败）时，传输层报告
  "未发送、可安全重跑"（exit 2），绝不误报为"结果未知"。
* SSH 会话建立后断开、结果不可知时，传输层只报告 "未知"（exit 124），
  绝不当作"命令失败/未发射"。
* 远端命令的退出码、stdout 与引号内容原样传回（经 base64 传输，不被本机或
  远端登录 shell 二次解释）。
* trosa-release publish 在发射结果未知时输出 unknown 状态与恢复指引，
  而不是"发射失败、production 未变更"。
* `trosa-release check --release-id` 恢复查询能归一出
  submitted/checking/success/failed/unknown 五种状态。
* GitHub compare API 瞬态故障有界重试后成功；持续故障仍 fail closed。
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import release_baseline as baseline


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                          cwd=str(ROOT), **kwargs)


def clean_env(**extra):
    """Build a child environment free of ambient release/role state.

    A dev/review session exports ``TRADE_OS_AGENT_ROLE=dev`` (or ``review``).
    If these transport/state-machine tests inherited it, ``trosa-release
    publish`` would (correctly) refuse at the role guard before exercising the
    behaviour under test, producing a false failure. The child only sees the
    ``TRADE_OS_*`` variables the test passes explicitly.
    """
    base = {k: v for k, v in os.environ.items() if not k.startswith("TRADE_OS_")}
    # Pin the SSH host to a name that cannot resolve: no test may reach the
    # production alias (trosa-new) through default routing.
    base["TRADE_OS_SSH_HOST"] = "test-host.invalid"
    base.update(extra)
    return base


def write_script(path: Path, body: str) -> str:
    path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


class FakeHttpResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class BaselineRetryTests(unittest.TestCase):
    """GitHub API 瞬态故障重试 + fail-close 保持。"""

    def test_transient_503_is_retried_and_recovers(self):
        calls = {"n": 0}
        sleeps = []

        def flaky(request, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 503, "Service Unavailable", {}, None)
            return FakeHttpResponse(b'{"status": "ahead"}')

        status = baseline.fetch_compare_status(
            "owner/repo", "a" * 40, "b" * 40, urlopen=flaky,
            sleep=sleeps.append)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(sleeps, [1])
        self.assertEqual(status, "ahead")

    def test_persistent_transport_failure_fails_closed(self):
        def boom(request, timeout=None):
            raise OSError("network down")

        sleeps = []
        decision = baseline.assess("a" * 40, "b" * 40, "owner/repo",
                                   urlopen=boom, sleep=sleeps.append)
        self.assertFalse(decision["allow"])
        self.assertEqual(decision["reason"], "compare_unavailable")
        # 1 次初始 + 2 次重试 = 3 次有界尝试，绝不无限重试。
        self.assertEqual(len(sleeps), 2)

    def test_definitive_404_is_not_retried(self):
        calls = {"n": 0}

        def not_found(request, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError("", 404, "Not Found", {}, None)

        with self.assertRaises(urllib.error.HTTPError):
            baseline.fetch_compare_status(
                "owner/repo", "a" * 40, "b" * 40, urlopen=not_found)
        self.assertEqual(calls["n"], 1)

    def test_token_is_sent_as_bearer_header(self):
        captured = {}

        def opener(request, timeout=None):
            captured["auth"] = request.headers.get("Authorization")
            return FakeHttpResponse(b'{"status": "identical"}')

        baseline.fetch_compare_status("owner/repo", "a" * 40, "b" * 40,
                                      token="tok-123", urlopen=opener)
        self.assertEqual(captured["auth"], "Bearer tok-123")

    def test_assess_passes_token_through_to_transport(self):
        captured = {}

        def opener(request, timeout=None):
            captured["auth"] = request.headers.get("Authorization")
            return FakeHttpResponse(b'{"status": "ahead"}')

        decision = baseline.assess("a" * 40, "b" * 40, "owner/repo",
                                   token="tok-123", urlopen=opener)
        self.assertTrue(decision["allow"])
        self.assertEqual(captured["auth"], "Bearer tok-123")


# 模拟 ssh：不联网。最后一个参数就是远端命令，本机以同样的方式执行它；
# STUB_SSH_MODE 决定本次“连接”的结局。
SSH_STUB = r'''#!/usr/bin/env bash
case "${STUB_SSH_MODE:-run}" in
  refused)
    echo "ssh: connect to host test-host port 22: Connection refused" >&2
    exit 255 ;;
  denied)
    echo "trosa-operator@test-host: Permission denied (publickey)." >&2
    exit 255 ;;
  dropped)
    echo "Connection reset by 203.0.113.9 port 22" >&2
    exit 255 ;;
  exit7)
    exit 7 ;;
esac
for last; do :; done
bash -c "$last"
'''


class RunSshSimulationTests(unittest.TestCase):
    """传输层故障注入：驱动 run-ssh-command.sh，ssh 由上面的桩替代。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="trosa-release-net-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        write_script(bin_dir / "ssh", SSH_STUB.split("\n", 1)[1])
        self.path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    def transport(self, remote_command: str, mode: str = "run"):
        env = clean_env(PATH=self.path, STUB_SSH_MODE=mode)
        return run(["bash", "deploy/cloud/run-ssh-command.sh",
                    "test-host", remote_command], env=env)

    def test_remote_output_and_quoting_survive_the_round_trip(self):
        proc = self.transport("printf '%s\\n' \"it's ok\" \"$HOME\" | head -c 6")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "it's o")

    def test_remote_exit_code_is_passed_through(self):
        proc = self.transport("exit 7")
        self.assertEqual(proc.returncode, 7, proc.stderr)

    def test_nested_stdin_reader_sees_end_of_file(self):
        # 远端命令里读 stdin 的工具（pg_dump / docker exec 等）必须立刻拿到 EOF，
        # 不能把传输层的通道吞掉。
        proc = self.transport("cat; echo done")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "done")

    def test_connection_refused_is_not_submitted(self):
        proc = self.transport("echo hi", mode="refused")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("was not sent", proc.stderr)
        self.assertNotIn("hi", proc.stdout)

    def test_auth_failure_is_not_submitted(self):
        proc = self.transport("echo hi", mode="denied")
        self.assertEqual(proc.returncode, 2, proc.stderr)

    def test_connection_dropped_after_start_is_unknown_not_failure(self):
        proc = self.transport("echo hi", mode="dropped")
        self.assertEqual(proc.returncode, 124, proc.stderr)
        self.assertIn("outcome unknown", proc.stderr)

    def test_remote_exit_7_is_not_mistaken_for_transport_failure(self):
        proc = self.transport("echo hi", mode="exit7")
        self.assertEqual(proc.returncode, 7, proc.stderr)


class ReleaseClientStateTests(unittest.TestCase):
    """trosa-release 状态机与恢复查询（stub 传输层，不发任何 SSH 请求）。"""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix="-workbench.env", delete=False, encoding="utf-8")
        handle.write("TRADE_OS_SSH_HOST=test-host.invalid\n")
        handle.write(f"PROJECT_ROOT={ROOT}\n")
        handle.close()
        self.env_file = handle.name
        self.addCleanup(os.remove, self.env_file)
        self.commit = run(["git", "rev-parse", "HEAD"]).stdout.strip()

    def release(self, *args, transport_body: str):
        transport = write_script(
            Path(tempfile.mkdtemp(prefix="trosa-release-tr-")) / "transport.sh",
            transport_body)
        env = clean_env(
            TRADE_OS_WORKBENCH_ENV=self.env_file,
            TRADE_OS_SSH_TRANSPORT=transport,
            TRADE_OS_RELEASE_POLL_INTERVAL="1",
            TRADE_OS_RELEASE_POLL_TIMEOUT="30")
        return run(["bash", "deploy/cloud/trosa-release", *args], env=env)

    def test_launch_unknown_is_not_reported_as_release_failure(self):
        # 发射命令结果未知（124，SSH 断开）：不得判定发布失败，也不得声称
        # "production 未变更"。
        body = 'echo "ssh: connection dropped mid-launch" >&2\nexit 124\n'
        proc = self.release("publish", "--commit", self.commit,
                            "--release-id", "rel-test-unknown",
                            transport_body=body)
        self.assertEqual(proc.returncode, 5, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertNotIn("发射失败", combined)
        self.assertNotIn("production 未变更", combined)
        self.assertIn("production 状态未知", proc.stderr)
        self.assertIn("check --release-id rel-test-unknown", proc.stderr)
        self.assertIn("TROSA_RELEASE_STATE unknown", combined)

    def test_launch_rejection_still_reports_unchanged_production(self):
        # SSH 会话未建立：发射脚本未送达，production 未变更，可安全重跑。
        body = 'echo "ssh: connect to host test-host port 22: Connection refused" >&2\nexit 2\n'
        proc = self.release("publish", "--commit", self.commit,
                            "--release-id", "rel-test-reject",
                            transport_body=body)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("production 未变更", proc.stderr)

    def test_launch_sends_server_launcher_over_ssh(self):
        # 发射时服务器上不需要预装任何脚本：启动器与参数随同一条 SSH 命令发送。
        captured = Path(tempfile.mkdtemp(prefix="trosa-release-cap-")) / "cmd.txt"
        self.addCleanup(lambda: captured.unlink(missing_ok=True))
        # Only the launch command is captured; the polls that follow are plain cat calls.
        body = (
            f"case \"$2\" in *'sudo -n'*) printf '%s' \"$2\" > '{captured}' ;; esac\n"
            'printf "launched rel-test-shape mode=deploy\\n"\n'
            "printf '%s\\n' '{\"release\": \"rel-test-shape\", \"status\": \"success\"}'\n"
        )
        proc = self.release("publish", "--commit", self.commit,
                            "--release-id", "rel-test-shape",
                            transport_body=body)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sent = captured.read_text(encoding="utf-8")
        self.assertIn("sudo -n bash -s --", sent)
        self.assertIn("<<'TROSA_LAUNCH'", sent)
        self.assertIn("setsid nohup", sent)
        self.assertIn(f"'rel-test-shape' '{self.commit}'", sent)
        self.assertNotIn("release-runner", sent)

    def test_publish_terminal_states_emit_state_lines(self):
        for status, expected_state, rc in (
            ("success", "success", 0),
            ("refused", "failed", 3),
            ("busy", "failed", 6),
        ):
            doc = json.dumps({"release": "rel-test-terminal", "status": status,
                              "phase": "done"})
            body = (
                'printf "launched rel-test-terminal mode=deploy\\n"\n'
                f"printf '%s\\n' '{doc}'\n"
            )
            proc = self.release(
                "publish", "--commit", self.commit,
                "--release-id", "rel-test-terminal",
                transport_body=body)
            self.assertEqual(proc.returncode, rc, (status, proc.stderr))
            self.assertIn(f"TROSA_RELEASE_STATE {expected_state}",
                          proc.stdout + proc.stderr)
            if status == "success":
                self.assertIn("TROSA_RELEASE_SUCCESS", proc.stdout)

    def test_publish_failure_emits_failure_class(self):
        doc = json.dumps({"release": "rel-test-bkfail", "status": "failed",
                          "phase": "backup",
                          "failure_class": "backup_verification_failed"})
        body = (
            'printf "launched rel-test-bkfail mode=deploy\\n"\n'
            f"printf '%s\\n' '{doc}'\n"
        )
        proc = self.release("publish", "--commit", self.commit,
                            "--release-id", "rel-test-bkfail",
                            transport_body=body)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("TROSA_RELEASE_STATE failed", combined)
        self.assertIn("TROSA_RELEASE_FAILURE_CLASS backup_verification_failed",
                      combined)

    def test_check_reports_failure_class(self):
        doc = json.dumps({"release": "rel-check-bk", "status": "failed",
                          "phase": "backup",
                          "failure_class": "backup_failed"})
        body = f"printf '%s\\n' '{doc}'\n"
        proc = self.release("check", "--release-id", "rel-check-bk",
                            transport_body=body)
        out = json.loads(proc.stdout)
        self.assertEqual(out["state"], "failed")
        self.assertEqual(out["failure_class"], "backup_failed")

    def test_check_recovers_terminal_state(self):
        doc = json.dumps({
            "release": "rel-check-1", "commit": self.commit,
            "status": "success", "phase": "done",
            "production": {"id": "rel-check-1", "commit": self.commit},
        })
        body = f"printf '%s\\n' '{doc}'\n"
        proc = self.release("check", "--release-id", "rel-check-1",
                            transport_body=body)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["state"], "success")
        self.assertEqual(out["status"], "success")

    def test_check_maps_in_progress_to_checking(self):
        doc = json.dumps({"release": "rel-check-2", "status": "in_progress",
                          "phase": "started"})
        body = f"printf '%s\\n' '{doc}'\n"
        proc = self.release("check", "--release-id", "rel-check-2",
                            transport_body=body)
        out = json.loads(proc.stdout)
        self.assertEqual(out["state"], "checking")

    def test_check_reports_unknown_when_no_result_exists(self):
        body = 'printf "cat: no such file\\n" >&2\n'
        proc = self.release("check", "--release-id", "rel-check-missing",
                            transport_body=body)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["state"], "unknown")
        self.assertEqual(out["status"], "unknown")

    def test_check_maps_failed_family_to_failed(self):
        for status in ("failed", "rolled_back", "rollback_failed"):
            doc = json.dumps({"release": "rel-check-3", "status": status,
                              "phase": "health"})
            body = f"printf '%s\\n' '{doc}'\n"
            proc = self.release("check", "--release-id", "rel-check-3",
                                transport_body=body)
            out = json.loads(proc.stdout)
            self.assertEqual(out["state"], "failed", status)


class AgentRoleIsolationTests(unittest.TestCase):
    """The gate neutralizes ambient role, but the boundary still refuses dev.

    The regression these protect against: a dev/review session exports
    ``TRADE_OS_AGENT_ROLE``, the normal release regressions inherit it and the
    role guard rejects their stubbed publish calls, so the suite fails for the
    wrong reason. Isolation must not silently drop the boundary itself.
    """

    def test_gate_drops_agent_role_for_python_regression(self):
        text = (ROOT / "deploy" / "cloud" / "release-test.sh").read_text(
            encoding="utf-8")
        self.assertIn("-u TRADE_OS_AGENT_ROLE", text)

    def test_clean_env_does_not_inherit_agent_role(self):
        original = os.environ.get("TRADE_OS_AGENT_ROLE")
        os.environ["TRADE_OS_AGENT_ROLE"] = "dev"
        try:
            self.assertNotIn("TRADE_OS_AGENT_ROLE", clean_env())
        finally:
            if original is None:
                os.environ.pop("TRADE_OS_AGENT_ROLE", None)
            else:
                os.environ["TRADE_OS_AGENT_ROLE"] = original

    def test_publish_still_refused_under_explicit_dev_role(self):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix="-workbench.env", delete=False, encoding="utf-8")
        handle.write("TRADE_OS_SSH_HOST=test-host.invalid\n")
        handle.close()
        self.addCleanup(os.remove, handle.name)
        env = clean_env(
            TRADE_OS_WORKBENCH_ENV=handle.name,
            TRADE_OS_AGENT_ROLE="dev")
        proc = run(["bash", "deploy/cloud/trosa-release", "publish",
                    "--commit", "a" * 40, "--release-id", "rel-dev-refused"],
                   env=env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("发布被拒绝", proc.stderr)
        self.assertIn("TRADE_OS_AGENT_ROLE=dev", proc.stderr)


if __name__ == "__main__":
    unittest.main()
