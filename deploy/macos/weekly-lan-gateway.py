#!/usr/bin/env python3
"""Expose the cloud weekly board on the Mac's fixed office LAN address.

This process never opens a database and never starts a second Trade OS app.
It listens only while the configured office IP is present, accepts only office
LAN clients, and forwards a deliberately small set of read-only weekly routes.
"""

from __future__ import annotations

import ipaddress
import os
import re
import signal
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


LISTEN_HOST = os.environ.get("TRADE_OS_WEEKLY_LAN_HOST", "192.168.0.58").strip()
LISTEN_PORT = int(os.environ.get("TRADE_OS_WEEKLY_LAN_PORT", "8080"))
UPSTREAM = os.environ.get("TRADE_OS_WEEKLY_UPSTREAM", "https://app.trosa.space").strip().rstrip("/")
GATEWAY_TOKEN = os.environ.get("TRADE_OS_WEEKLY_GATEWAY_TOKEN", "").strip()
CLIENT_NETWORKS = tuple(
    ipaddress.ip_network(item.strip(), strict=False)
    for item in os.environ.get("TRADE_OS_WEEKLY_ALLOWED_NETWORKS", "192.168.0.0/23").split(",")
    if item.strip()
)
STOP = threading.Event()
OFFICE_ADDRESS_CHECK_INTERVAL_SECONDS = 2.0
OFFICE_ADDRESS_GRACE_SECONDS = 20.0

_API_PATHS = (
    re.compile(r"^/api/auth/me$"),
    re.compile(r"^/api/network/ping$"),
    re.compile(r"^/api/version$"),
    re.compile(r"^/api/weekly-summary(?:/[a-z0-9_-]+)?$"),
    re.compile(r"^/api/overview/stats$"),
    re.compile(r"^/api/overview/all-customers$"),
    re.compile(r"^/api/overview/customers/[a-z0-9_-]+/\d+$"),
)
_STATIC_PATHS = ("/app.js", "/style.css", "/visual-v2.css", "/favicon.ico")
_STATIC_PREFIXES = ("/assets/", "/icons/")
_FORWARD_REQUEST_HEADERS = (
    "Accept",
    "Accept-Encoding",
    "Accept-Language",
    "If-Modified-Since",
    "If-None-Match",
    "Range",
)
_FORWARD_RESPONSE_HEADERS = {
    "accept-ranges",
    "cache-control",
    "content-disposition",
    "content-encoding",
    "content-language",
    "content-range",
    "content-type",
    "etag",
    "expires",
    "last-modified",
    "location",
    "permissions-policy",
    "pragma",
    "referrer-policy",
    "x-content-type-options",
    "x-robots-tag",
}


def _validate_configuration() -> None:
    try:
        address = ipaddress.ip_address(LISTEN_HOST)
    except ValueError as exc:
        raise RuntimeError("公司周报入口地址无效") from exc
    if address.version != 4 or address.is_loopback or address.is_unspecified:
        raise RuntimeError("公司周报入口必须使用固定的局域网 IPv4 地址")
    parsed = urllib.parse.urlsplit(UPSTREAM)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
        raise RuntimeError("云端周报地址必须是 HTTPS 网站根地址")
    if len(GATEWAY_TOKEN) < 32:
        raise RuntimeError("公司周报入口密钥缺失或过短")
    if not CLIENT_NETWORKS:
        raise RuntimeError("公司局域网范围不能为空")


def _office_address_present() -> bool:
    """Return whether this Mac currently owns the fixed office address."""
    try:
        output = subprocess.run(
            ["/sbin/ifconfig"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    for line in output.splitlines():
        fields = line.strip().split()
        if len(fields) >= 2 and fields[0] == "inet" and fields[1] == LISTEN_HOST:
            return True
    return False


def _client_allowed(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in CLIENT_NETWORKS)


def _path_allowed(raw_path: str) -> bool:
    path = urllib.parse.urlsplit(raw_path).path
    if path in ("/", "/share/weekly") or path in _STATIC_PATHS:
        return True
    if any(path.startswith(prefix) for prefix in _STATIC_PREFIXES):
        return True
    return any(pattern.fullmatch(path) for pattern in _API_PATHS)


def _upstream_path(raw_path: str) -> str:
    parsed = urllib.parse.urlsplit(raw_path)
    if parsed.path == "/" and not parsed.query:
        return "/?weekly=1"
    return raw_path


class WeeklyGatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TradeOS-Weekly-LAN"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._proxy("GET")

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._proxy("HEAD")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._reject_write()

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST

    def _reject_write(self) -> None:
        self._send_text(405, "公司周报入口只允许查看，不能修改数据。")

    def _proxy(self, method: str) -> None:
        if not _client_allowed(self.client_address[0]):
            self._send_text(403, "这个入口只允许公司局域网内的设备访问。")
            return
        if not _path_allowed(self.path):
            self._send_text(403, "公司周报入口只能查看本周工作。")
            return

        target = UPSTREAM + _upstream_path(self.path)
        headers = {
            name: self.headers[name]
            for name in _FORWARD_REQUEST_HEADERS
            if self.headers.get(name)
        }
        headers["X-TradeOS-Weekly-Gateway"] = GATEWAY_TOKEN
        headers["User-Agent"] = "TradeOS-Weekly-LAN/1.0"
        request = urllib.request.Request(target, headers=headers, method=method)
        try:
            response = urllib.request.urlopen(
                request,
                timeout=20,
                context=ssl.create_default_context(),
            )
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, urllib.error.URLError):
            self._send_text(502, "暂时无法连接云端周报，请稍后刷新。")
            return

        try:
            body = b"" if method == "HEAD" else response.read()
            self.send_response(response.status)
            for name, value in response.headers.items():
                lowered = name.lower()
                if lowered not in _FORWARD_RESPONSE_HEADERS:
                    continue
                if lowered == "location" and value.startswith(UPSTREAM):
                    value = value[len(UPSTREAM):] or "/"
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if method != "HEAD" and body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            response.close()

    def _send_text(self, status: int, message: str) -> None:
        body = (message + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, message: str, *args: object) -> None:
        # Keep logs useful without recording query strings or the gateway token.
        path = urllib.parse.urlsplit(self.path).path
        print("%s - %s %s - %s" % (self.client_address[0], self.command, path, message % args), flush=True)


class WeeklyGatewayServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _stop_handler(_signum: int, _frame: object) -> None:
    STOP.set()


def main() -> None:
    _validate_configuration()
    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)
    print("公司周报入口正在等待 Mac 连接公司网络。", flush=True)
    while not STOP.is_set():
        if not _office_address_present():
            STOP.wait(5)
            continue
        try:
            server = WeeklyGatewayServer((LISTEN_HOST, LISTEN_PORT), WeeklyGatewayHandler)
        except OSError as exc:
            print("公司周报入口暂时无法监听 %s:%s：%s" % (LISTEN_HOST, LISTEN_PORT, exc), flush=True)
            STOP.wait(5)
            continue
        server.timeout = 2
        print("公司周报入口已开启：http://%s:%s" % (LISTEN_HOST, LISTEN_PORT), flush=True)
        try:
            # A Wi-Fi/DHCP transition can make one ifconfig poll miss the
            # address briefly. Keep the read-only listener alive during that
            # short gap so clients do not see connection refused.
            last_address_seen = time.monotonic()
            next_address_check = last_address_seen
            while not STOP.is_set():
                now = time.monotonic()
                if now >= next_address_check:
                    if _office_address_present():
                        last_address_seen = now
                    next_address_check = now + OFFICE_ADDRESS_CHECK_INTERVAL_SECONDS
                if now - last_address_seen >= OFFICE_ADDRESS_GRACE_SECONDS:
                    break
                server.handle_request()
        finally:
            server.server_close()
            print("Mac 已离开公司网络，公司周报入口已关闭。", flush=True)


if __name__ == "__main__":
    main()
