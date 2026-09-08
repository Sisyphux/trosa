import http.client
import importlib.util
import ipaddress
import threading
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
GATEWAY_PATH = ROOT / 'deploy' / 'macos' / 'weekly-lan-gateway.py'


def load_gateway():
    spec = importlib.util.spec_from_file_location('weekly_lan_gateway_test', GATEWAY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DummyUpstreamResponse:
    def __init__(self, body=b'ok', status=200, content_type='text/plain; charset=utf-8'):
        self.status = status
        self._body = body
        self.headers = Message()
        self.headers['Content-Type'] = content_type
        self.headers['Cache-Control'] = 'no-store'
        self.headers['Set-Cookie'] = 'must-not-reach-the-lan-browser=1'

    def read(self):
        return self._body

    def close(self):
        pass


class WeeklyLanGatewayTest(unittest.TestCase):
    def setUp(self):
        self.gateway = load_gateway()
        self.gateway.GATEWAY_TOKEN = 'a' * 64
        self.gateway.CLIENT_NETWORKS = (ipaddress.ip_network('127.0.0.1/32'),)

    def test_route_allowlist_excludes_normal_crm_apis(self):
        self.assertTrue(self.gateway._path_allowed('/'))
        self.assertTrue(self.gateway._path_allowed('/api/auth/users'))
        self.assertTrue(self.gateway._path_allowed('/api/weekly-summary/hamid?limit=10'))
        self.assertTrue(self.gateway._path_allowed('/api/overview/customers/amy/12'))
        self.assertTrue(self.gateway._path_allowed('/assets/sidebar-tree-lines-v2.webp'))
        self.assertFalse(self.gateway._path_allowed('/api/auth/users/hamid'))
        self.assertFalse(self.gateway._path_allowed('/api/customers'))
        self.assertFalse(self.gateway._path_allowed('/api/settings'))
        self.assertFalse(self.gateway._path_allowed('/api/backup/list'))

    def test_root_is_forwarded_with_private_token_and_writes_are_blocked(self):
        captured = []

        def fake_urlopen(request, **_kwargs):
            captured.append(request)
            return DummyUpstreamResponse(body=b'<html>weekly</html>', content_type='text/html')

        server = self.gateway.WeeklyGatewayServer(('127.0.0.1', 0), self.gateway.WeeklyGatewayHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(self.gateway.urllib.request, 'urlopen', side_effect=fake_urlopen):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                connection.request('GET', '/')
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b'<html>weekly</html>')
                self.assertIsNone(response.getheader('Set-Cookie'))
                connection.close()

                self.assertEqual(captured[0].full_url, 'https://app.trosa.space/?weekly=1')
                self.assertEqual(captured[0].get_header('X-tradeos-weekly-gateway'), 'a' * 64)

                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                connection.request('GET', '/api/auth/users')
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                connection.close()
                self.assertEqual(captured[-1].full_url, 'https://app.trosa.space/api/auth/users')

                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                connection.request('GET', '/api/customers')
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                connection.close()

                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                connection.request('POST', '/api/weekly-summary')
                response = connection.getresponse()
                self.assertEqual(response.status, 405)
                response.read()
                connection.close()

                self.assertEqual(len(captured), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_transient_upstream_failure_is_retried_before_returning_to_browser(self):
        attempts = []

        def fake_urlopen(_request, **_kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                raise urllib.error.URLError('temporary network interruption')
            return DummyUpstreamResponse(body=b'weekly report')

        server = self.gateway.WeeklyGatewayServer(('127.0.0.1', 0), self.gateway.WeeklyGatewayHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(self.gateway.urllib.request, 'urlopen', side_effect=fake_urlopen), \
                    mock.patch.object(self.gateway.time, 'sleep') as sleep:
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                connection.request('GET', '/api/weekly-summary/hamid')
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b'weekly report')
                connection.close()

            self.assertEqual(len(attempts), 2)
            sleep.assert_called_once_with(self.gateway.UPSTREAM_RETRY_DELAY_SECONDS)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_office_address_check_requires_exact_mac_address(self):
        completed = mock.Mock(stdout='en0: flags=8863\n\tinet 192.168.0.58 netmask 0xffffff00\n')
        with mock.patch.object(self.gateway.subprocess, 'run', return_value=completed):
            self.assertTrue(self.gateway._office_address_present())
        completed.stdout = 'en0: flags=8863\n\tinet 192.168.3.106 netmask 0xffffff00\n'
        with mock.patch.object(self.gateway.subprocess, 'run', return_value=completed):
            self.assertFalse(self.gateway._office_address_present())


if __name__ == '__main__':
    unittest.main()
