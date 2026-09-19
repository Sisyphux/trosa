"""网络异常模拟验收：发布链路在 Cloud Assistant / GitHub API 故障下的行为。

验收目标（不触网、不碰生产）：

* RunCommand 已被接受但 DescribeInvocations 持续失败时，传输层只报告
  "已提交但结果未知"（exit 124 + CLOUD_ASSISTANT_PENDING），绝不当作
  "命令失败/未发射"。
* DescribeInvocations 偶发失败（一次网络抖动）时，同一调用在截止时间内
  自动重试并最终拿到真实结果。
* Cloud Assistant API 在提交前被明确拒绝（HTTP 4xx）时才允许报告
  "未提交、可直接重跑"。
* trosa-release publish 在发射结果未知时输出 unknown 状态与恢复指引，
  而不是"发射失败、production 未变更"。
* `trosa-release check --release-id` 恢复查询能归一出
  submitted/checking/success/failed/unknown 五种状态。
* GitHub compare API 瞬态故障有界重试后成功；持续故障仍 fail closed。
"""

import json
import os
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


class RunCloudAssistantSimulationTests(unittest.TestCase):
    """传输层故障注入：直接驱动 run-cloud-assistant-command.sh。"""

    def setUp(self):
        self.counter = 0
        self.tmp = tempfile.mkdtemp(prefix="trosa-release-net-")

    def transport(self, run_body: str, get_body: str,
                  extra_env: dict | None = None):
        # stub cloud-assistant.py：run/get 行为由传入的 Python 代码片段决定。
        self.counter += 1
        client = Path(self.tmp) / f"ca-{self.counter}.py"
        client.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "op = sys.argv[1] if len(sys.argv) > 1 else ''\n"
            f"def run_stub():\n{run_body}\n"
            f"def get_stub():\n{get_body}\n"
            "{'run': run_stub, 'get': get_stub}[op]()\n",
            encoding="utf-8")
        client.chmod(client.stat().st_mode | stat.S_IEXEC)
        env = {**os.environ,
               "TRADE_OS_CLOUD_ASSISTANT_CLIENT": str(client),
               "TRADE_OS_CLOUD_ASSISTANT_TIMEOUT": "10",
               "TRADE_OS_CLOUD_ASSISTANT_POLL_GRACE": "2",
               **(extra_env or {})}
        return run(["bash", "deploy/cloud/run-cloud-assistant-command.sh",
                    "i-1", "cn-x", "echo hi"], env=env)

    def test_accepted_but_describe_always_failing_is_unknown_not_failure(self):
        # RunCommand 接受（返回 InvokeId），DescribeInvocations 一直网络失败：
        # 只能报告"已提交但结果未知"，不得当作命令失败。
        run_body = '    print(\'{"InvokeId": "inv-1", "CommandId": "cmd-1"}\')\n'
        get_body = '    print("stub network failure", file=sys.stderr)\n    raise SystemExit(2)\n'
        proc = self.transport(run_body, get_body)
        self.assertEqual(proc.returncode, 124, proc.stderr)
        self.assertIn("CLOUD_ASSISTANT_PENDING invoke_id=inv-1", proc.stderr)

    def test_transient_describe_failure_retries_and_succeeds(self):
        # 第 1 次 get 网络失败，第 2 次返回完成且退出码 0。
        flag = f"{self.tmp}/called"
        run_body = '    print(\'{"InvokeId": "inv-1", "CommandId": "cmd-1"}\')\n'
        get_body = (
            '    import base64, json, os\n'
            f'    if not os.path.exists({flag!r}):\n'
            f'        open({flag!r}, "w").close()\n'
            '        print("stub network failure", file=sys.stderr)\n'
            '        raise SystemExit(2)\n'
            '    out = base64.b64encode(b"hi").decode()\n'
            '    row = {"InvocationStatus": "Success", "ExitCode": 0, "Output": out}\n'
            '    doc = {"Invocations": {"Invocation": [dict(row, InvokeInstances={"InvokeInstance": [row]})]}}\n'
            '    print(json.dumps(doc))\n'
        )
        proc = self.transport(run_body, get_body)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("hi", proc.stdout)

    def test_explicit_api_rejection_is_not_submitted(self):
        # Cloud Assistant 明确拒绝（4xx）：未提交，可以安全重跑。
        run_body = '    print("cloud-assistant: RunCommand HTTP 403: denied", file=sys.stderr)\n    raise SystemExit(2)\n'
        get_body = '    raise SystemExit(2)\n'
        proc = self.transport(run_body, get_body)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertNotIn("CLOUD_ASSISTANT_PENDING", proc.stderr)

    def test_ambiguous_run_failure_is_unknown(self):
        # RunCommand 请求发出后网络死亡（重试耗尽）：接受与否未知 → 124。
        run_body = '    print("CLOUD_ASSISTANT_AMBIGUOUS action=RunCommand", file=sys.stderr)\n    raise SystemExit(2)\n'
        get_body = '    raise SystemExit(2)\n'
        proc = self.transport(run_body, get_body)
        self.assertEqual(proc.returncode, 124, proc.stderr)


class ReleaseClientStateTests(unittest.TestCase):
    """trosa-release 状态机与恢复查询（stub 传输层，不发任何 API 请求）。"""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix="-workbench.env", delete=False, encoding="utf-8")
        handle.write("TRADE_OS_ECS_REGION=test-region\n")
        handle.write("TRADE_OS_ECS_INSTANCE_ID=i-test-instance\n")
        handle.write(f"PROJECT_ROOT={ROOT}\n")
        handle.close()
        self.env_file = handle.name
        self.addCleanup(os.remove, self.env_file)
        self.commit = run(["git", "rev-parse", "HEAD"]).stdout.strip()

    def release(self, *args, transport_body: str):
        transport = write_script(
            Path(tempfile.mkdtemp(prefix="trosa-release-tr-")) / "transport.sh",
            transport_body)
        env = {**os.environ,
               "TRADE_OS_WORKBENCH_ENV": self.env_file,
               "TRADE_OS_CLOUD_ASSISTANT_TRANSPORT": transport,
               "TRADE_OS_RELEASE_POLL_INTERVAL": "1",
               "TRADE_OS_RELEASE_POLL_TIMEOUT": "30"}
        return run(["bash", "deploy/cloud/trosa-release", *args], env=env)

    def test_launch_unknown_is_not_reported_as_release_failure(self):
        # 发射命令被接受但结果未知（124）：不得判定发布失败，也不得声称
        # "production 未变更"。
        body = 'echo "Cloud Assistant invocation is still pending" >&2\nexit 124\n'
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
        # Cloud Assistant 提交前明确拒绝：未发射，可安全重跑。
        body = 'echo "cloud-assistant: RunCommand HTTP 403: denied" >&2\nexit 2\n'
        proc = self.release("publish", "--commit", self.commit,
                            "--release-id", "rel-test-reject",
                            transport_body=body)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("production 未变更", proc.stderr)

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


if __name__ == "__main__":
    unittest.main()
