#!/usr/bin/env python3
"""Bridge cloudflared's TCP edge connection through a local HTTP CONNECT proxy.

The local network currently blocks direct Cloudflare Tunnel traffic on port 7844,
while the user's local proxy can establish CONNECT tunnels to that port. This
small stdlib-only helper exposes a loopback TCP endpoint and a loopback DNS
endpoint for cloudflared; it never handles CRM traffic or credentials.
"""

from __future__ import annotations

import os
import select
import signal
import socket
import struct
import threading
from itertools import cycle


LISTEN_HOST = "127.0.0.1"
EDGE_PORT = int(os.environ.get("TRADE_OS_EDGE_PROXY_PORT", "7844"))
DNS_HOST = "127.0.0.1"
DNS_PORT = int(os.environ.get("TRADE_OS_EDGE_DNS_PORT", "15353"))
HTTP_PROXY_HOST = os.environ.get("TRADE_OS_HTTP_PROXY_HOST", "127.0.0.1")
HTTP_PROXY_PORT = int(os.environ.get("TRADE_OS_HTTP_PROXY_PORT", "7892"))
EDGE_HOSTS = (
    "region1.v2.argotunnel.com",
    "region2.v2.argotunnel.com",
)
EDGE_HOST_SET = {name.encode("ascii") for name in EDGE_HOSTS}
STOP = threading.Event()
EDGE_HOST_CYCLE = cycle(EDGE_HOSTS)


def _read_until_headers(sock: socket.socket) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > 64 * 1024:
            raise RuntimeError("HTTP proxy response headers are too large")
    return bytes(data)


def _connect_through_http_proxy() -> socket.socket:
    edge_host = next(EDGE_HOST_CYCLE)
    proxy = socket.create_connection((HTTP_PROXY_HOST, HTTP_PROXY_PORT), timeout=12)
    request = (
        f"CONNECT {edge_host}:{EDGE_PORT} HTTP/1.1\r\n"
        f"Host: {edge_host}:{EDGE_PORT}\r\n"
        "Proxy-Connection: Keep-Alive\r\n"
        "\r\n"
    ).encode("ascii")
    proxy.sendall(request)
    response = _read_until_headers(proxy)
    first_line = response.split(b"\r\n", 1)[0]
    if b" 200 " not in first_line:
        proxy.close()
        raise RuntimeError(f"HTTP proxy refused CONNECT: {first_line.decode(errors='replace')}")
    proxy.settimeout(None)
    return proxy


def _pump(source: socket.socket, target: socket.socket) -> None:
    try:
        while not STOP.is_set():
            readable, _, _ = select.select([source], [], [], 1)
            if not readable:
                continue
            payload = source.recv(64 * 1024)
            if not payload:
                break
            target.sendall(payload)
    except (OSError, ValueError):
        pass


def _bridge(client: socket.socket) -> None:
    upstream = None
    try:
        upstream = _connect_through_http_proxy()
        left = threading.Thread(target=_pump, args=(client, upstream), daemon=True)
        right = threading.Thread(target=_pump, args=(upstream, client), daemon=True)
        left.start()
        right.start()
        left.join()
        right.join()
    except (OSError, RuntimeError):
        pass
    finally:
        for sock in (client, upstream):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def _decode_qname(packet: bytes, offset: int) -> tuple[str, int]:
    labels = []
    while offset < len(packet):
        length = packet[offset]
        offset += 1
        if length == 0:
            return ".".join(labels).encode("idna").decode().lower(), offset
        if length & 0xC0 or offset + length > len(packet):
            raise ValueError("compressed or invalid DNS question")
        labels.append(packet[offset : offset + length].decode("ascii"))
        offset += length
    raise ValueError("unterminated DNS question")


def _dns_response(packet: bytes) -> bytes:
    if len(packet) < 12:
        return b""
    question_count = struct.unpack("!H", packet[4:6])[0]
    if question_count < 1:
        return b""
    try:
        name, offset = _decode_qname(packet, 12)
        if offset + 4 > len(packet):
            return b""
        qtype, qclass = struct.unpack("!HH", packet[offset : offset + 4])
        question_end = offset + 4
    except (UnicodeError, ValueError):
        return b""

    question = packet[12:question_end]
    flags = struct.unpack("!H", packet[2:4])[0]
    response_flags = 0x8000 | (flags & 0x0100)
    answer = b""
    answer_count = 0
    if name in EDGE_HOSTS and qclass == 1 and qtype == 1:
        answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 30, 4) + socket.inet_aton(LISTEN_HOST)
        answer_count = 1

    header = struct.pack("!HHHHHH", struct.unpack("!H", packet[:2])[0], response_flags, 1, answer_count, 0, 0)
    return header + question + answer


def _dns_udp_server() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((DNS_HOST, DNS_PORT))
    sock.settimeout(1)
    try:
        while not STOP.is_set():
            try:
                packet, address = sock.recvfrom(4096)
            except socket.timeout:
                continue
            response = _dns_response(packet)
            if response:
                sock.sendto(response, address)
    finally:
        sock.close()


def _dns_tcp_client(client: socket.socket) -> None:
    try:
        length_data = client.recv(2)
        if len(length_data) != 2:
            return
        length = struct.unpack("!H", length_data)[0]
        packet = client.recv(length)
        response = _dns_response(packet)
        client.sendall(struct.pack("!H", len(response)) + response)
    except OSError:
        pass
    finally:
        client.close()


def _dns_tcp_server() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((DNS_HOST, DNS_PORT))
    sock.listen(8)
    sock.settimeout(1)
    try:
        while not STOP.is_set():
            try:
                client, _ = sock.accept()
            except socket.timeout:
                continue
            threading.Thread(target=_dns_tcp_client, args=(client,), daemon=True).start()
    finally:
        sock.close()


def _tcp_server() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((LISTEN_HOST, EDGE_PORT))
    sock.listen(16)
    sock.settimeout(1)
    try:
        while not STOP.is_set():
            try:
                client, _ = sock.accept()
            except socket.timeout:
                continue
            threading.Thread(target=_bridge, args=(client,), daemon=True).start()
    finally:
        sock.close()


def _stop_handler(_signum: int, _frame: object) -> None:
    STOP.set()


def main() -> None:
    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)
    threads = [
        threading.Thread(target=_tcp_server, daemon=True),
        threading.Thread(target=_dns_udp_server, daemon=True),
        threading.Thread(target=_dns_tcp_server, daemon=True),
    ]
    for thread in threads:
        thread.start()
    while not STOP.wait(1):
        if not all(thread.is_alive() for thread in threads):
            raise RuntimeError("cloudflared proxy helper stopped unexpectedly")


if __name__ == "__main__":
    main()
