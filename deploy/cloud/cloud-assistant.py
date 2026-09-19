#!/usr/bin/env python3
"""Small, dependency-free Cloud Assistant client for the Trosa operator.

Credentials deliberately live outside this repository.  By default this reads
the active Workbench CLI profile from ~/.workbench/config.json (mode AK).  That
file is created by ``workbench config`` with mode 0600; no password, AccessKey
or secret is read from deploy/cloud/workbench.env.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# Bounded retry for transient transport failures only.  Server-side explicit
# rejections (HTTP 4xx) fail immediately; ambiguous network errors are retried
# because RunCommand carries a ClientToken, which makes a re-submission of the
# same command idempotent.  A read-only Describe* query is always retryable.
DEFAULT_ATTEMPTS = 3
_TRANSIENT_HTTP_CODES = frozenset({429, 500, 502, 503, 504})


def fail(message: str) -> None:
    print(f"cloud-assistant: {message}", file=sys.stderr)
    raise SystemExit(2)


def quote(value: object) -> str:
    return urllib.parse.quote(str(value), safe="~")


def credentials(path: str | None) -> tuple[str, str]:
    config = Path(path or os.environ.get("TRADE_OS_WORKBENCH_CONFIG", "~/.workbench/config.json")).expanduser()
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
        profile = data["profiles"][data["current"]]
        if profile.get("mode") != "AK":
            fail("当前 Workbench profile 不是 AK；请使用仅限 Trosa ECS 的 RAM AccessKey")
        key_id = profile["access_key_id"]
        secret = profile["access_key_secret"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        fail(f"无法读取受保护的 Workbench 凭据配置 {config}: {exc}")
    if not isinstance(key_id, str) or not isinstance(secret, str) or not key_id or not secret:
        fail("Workbench AK 配置不完整")
    return key_id, secret


def request(region: str, action: str, parameters: dict[str, object], credentials_file: str | None,
            *, urlopen=urllib.request.urlopen, attempts: int = DEFAULT_ATTEMPTS,
            sleep=time.sleep) -> dict:
    key_id, secret = credentials(credentials_file)
    params: dict[str, object] = {
        "Format": "JSON",
        "Version": "2014-05-26",
        "AccessKeyId": key_id,
        "SignatureMethod": "HMAC-SHA1",
        "Timestamp": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "SignatureVersion": "1.0",
        "SignatureNonce": str(uuid.uuid4()),
        "Action": action,
        **parameters,
    }
    canonical = "&".join(f"{quote(k)}={quote(v)}" for k, v in sorted(params.items()))
    string_to_sign = f"GET&%2F&{quote(canonical)}"
    signature = base64.b64encode(hmac.new(f"{secret}&".encode(), string_to_sign.encode(), hashlib.sha1).digest()).decode()
    query = canonical + "&Signature=" + quote(signature)
    endpoint = f"https://ecs.{region}.aliyuncs.com/?{query}"
    last_error = ""
    for attempt in range(max(1, attempts)):
        try:
            with urlopen(endpoint, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code in _TRANSIENT_HTTP_CODES and attempt + 1 < attempts:
                last_error = f"HTTP {exc.code}"
                sleep(attempt + 1)
                continue
            fail(f"{action} HTTP {exc.code}: {body}")
        except (OSError, json.JSONDecodeError) as exc:
            # A dropped connection is ambiguous: the request may already have
            # been accepted server-side.  Bounded retry (RunCommand is made
            # idempotent by ClientToken; reads are always safe) before giving
            # up with an explicit ambiguity marker.
            last_error = str(exc)
            if attempt + 1 < attempts:
                sleep(attempt + 1)
                continue
            print(f"CLOUD_ASSISTANT_AMBIGUOUS action={action} error={last_error[:120]}", file=sys.stderr)
            fail(f"{action} 请求失败: {last_error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run/query Trosa ECS Cloud Assistant commands")
    parser.add_argument("--credentials-file", help="受保护的 Workbench config.json 路径；默认 ~/.workbench/config.json")
    sub = parser.add_subparsers(dest="operation", required=True)
    run = sub.add_parser("run")
    run.add_argument("--region", required=True)
    run.add_argument("--instance-id", required=True)
    run.add_argument("--command", required=True)
    run.add_argument("--username", default="trosa-operator")
    run.add_argument("--timeout", type=int, default=120)
    run.add_argument("--client-token", default=None)
    get = sub.add_parser("get")
    get.add_argument("--region", required=True)
    get.add_argument("--invoke-id", required=True)
    get.add_argument("--command-id")
    agent = sub.add_parser("agent-status")
    agent.add_argument("--region", required=True)
    agent.add_argument("--instance-id", required=True)
    args = parser.parse_args()
    if args.operation == "run":
        if not 10 <= args.timeout <= 3600:
            fail("--timeout 必须在 10 到 3600 秒之间")
        params: dict[str, object] = {
            "RegionId": args.region,
            "Type": "RunShellScript",
            "CommandContent": base64.b64encode(args.command.encode()).decode(),
            "ContentEncoding": "Base64",
            "InstanceId.1": args.instance_id,
            "Username": args.username,
            "Timeout": args.timeout,
            "KeepCommand": "false",
        }
        if args.client_token:
            params["ClientToken"] = args.client_token
        print(json.dumps(request(args.region, "RunCommand", params, args.credentials_file), ensure_ascii=False, sort_keys=True))
    elif args.operation == "get":
        params = {"RegionId": args.region, "InvokeId": args.invoke_id}
        if args.command_id:
            params["CommandId"] = args.command_id
        print(json.dumps(request(args.region, "DescribeInvocations", params, args.credentials_file), ensure_ascii=False, sort_keys=True))
    else:
        params = {"RegionId": args.region, "InstanceId.1": args.instance_id}
        print(json.dumps(request(args.region, "DescribeCloudAssistantStatus", params, args.credentials_file), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
