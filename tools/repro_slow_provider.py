#!/usr/bin/env python3
"""Loopback OpenAI-compatible model stub that answers after a fixed delay.

Used to reproduce Cloudflare error 524: a web request that waits on a slow model
holds the origin open past the edge timeout.  Point the isolated dev server at it
with ``DEEPSEEK_BASE_URL=http://127.0.0.1:<port>`` and ``LLM_BACKEND=deepseek``.
It binds 127.0.0.1 only and never forwards anything anywhere.

    .venv/bin/python tools/repro_slow_provider.py --port 18199 --delay 110
"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLY = {
    'summary': '客户询问亚克力标牌 500 件的报价',
    'needs': ['亚克力标牌'], 'key_facts': ['500 件'], 'intent': '中性',
    'mentioned_company': '', 'mentioned_contact': '', 'message_date': '',
    'suggested_next_action': '', 'direction': 'inbound',
}


def make_handler(delay):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get('Content-Length') or 0)
            self.rfile.read(length)
            time.sleep(delay)
            body = json.dumps({'choices': [{'message': {'content': json.dumps(REPLY, ensure_ascii=False)},
                                            'finish_reason': 'stop'}]}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Slow loopback model stub for 524 reproductions.')
    parser.add_argument('--port', type=int, required=True, help='loopback port to listen on')
    parser.add_argument('--delay', type=float, required=True, help='seconds to wait before answering')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(args.delay)).serve_forever()


if __name__ == '__main__':
    main()
