"""Send barrier request contract without a live database or email provider."""
import unittest
import uuid
from unittest import mock

from tests.test_sela_company_ownership_snapshot import app, Result


class BarrierConnection:
    def __init__(self):
        self.row = None

    def execute(self, sql, params=None):
        if 'SELECT *' in sql:
            return Result([self.row] if self.row else [])
        if 'INSERT INTO audit.sela_send_barriers' in sql:
            send_id, user, identity_hash, recipient_hash, token = params
            self.row = dict(requested_by=user, identity_hash=identity_hash,
                            recipient_hash=recipient_hash, token=token,
                            state='PREPARED', fresh=True)
        if 'SET token=' in sql:
            self.row.update(token=params[0], fresh=True)
        if "SET state='ARMED'" in sql:
            self.row['state'] = 'ARMED'
        return Result([])

    def commit(self): pass
    def rollback(self): pass
    def close(self): pass


class SendBarrierContractTest(unittest.TestCase):
    def setUp(self):
        self.conn = BarrierConnection()
        self.payload = {'send_id': str(uuid.uuid4()), 'name': 'Example',
                        'domains': ['first.example', 'second.example'],
                        'recipient_hash': 'a' * 64}
        self.snapshot = mock.Mock(return_value={'matches': []})
        self.patches = [mock.patch.object(app, 'postgres_mode', return_value=True),
                        mock.patch.object(app, 'get_db', return_value=self.conn),
                        mock.patch.object(app, 'get_current_user', return_value='hamid'),
                        mock.patch.object(app, '_sela_company_ownership_snapshot', self.snapshot)]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def post(self, action, payload):
        with app.app.test_request_context(json=payload):
            result = app.sela_send_barrier.__wrapped__.__wrapped__(action)
            return app.app.make_response(result)

    def prepare(self):
        response = self.post('prepare', self.payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        return dict(self.payload, token=response.get_json()['token'])

    def test_expired_prepared_is_reissued_after_snapshot(self):
        original = self.prepare()
        self.conn.row['fresh'] = False
        self.assertEqual(self.post('prepare', dict(self.payload, recipient_hash='b' * 64)).status_code, 409)
        self.assertEqual(self.conn.row['token'], original['token'])
        self.snapshot.reset_mock()
        renewed = self.post('prepare', self.payload).get_json()
        self.snapshot.assert_called_once()
        self.assertTrue(renewed['fresh'])
        self.assertNotEqual(renewed['token'], original['token'])
        self.assertEqual(self.post('arm', original).status_code, 403)
        self.assertTrue(self.post('arm', dict(original, token=renewed['token'])).get_json()['acquired'])

    def test_expired_armed_and_terminal_are_not_reissued(self):
        original = self.prepare()
        for state in ('ARMED', 'SENT', 'CONFIRMED_NOT_SENT'):
            with self.subTest(state=state):
                self.conn.row.update(state=state, fresh=False)
                result = self.post('prepare', self.payload).get_json()
                self.assertEqual((result['state'], result['token'], result['fresh']),
                                 (state, original['token'], False))

    def test_reissued_token_still_checks_current_owner_on_arm(self):
        self.prepare()
        self.conn.row['fresh'] = False
        renewed = self.post('prepare', self.payload).get_json()
        self.snapshot.return_value = {'matches': [{'identity_status': 'confirmed',
            'accounts': [{'owner_username': 'amy'}]}]}
        self.assertEqual(self.post('arm', dict(self.payload, token=renewed['token'])).status_code, 409)
        self.assertEqual(self.conn.row['state'], 'PREPARED')

    def test_recipient_hash_required_canonical_and_bound_before_snapshot(self):
        self.payload.pop('domains')
        arm = self.prepare()
        self.snapshot.reset_mock()
        for value in (None, 'b' * 64, 'A' * 64, 'a' * 63, 123):
            with self.subTest(value=value):
                data = dict(arm)
                if value is None:
                    data.pop('recipient_hash')
                else:
                    data['recipient_hash'] = value
                self.assertIn(self.post('arm', data).status_code, (400, 409))
                self.assertEqual(self.conn.row['state'], 'PREPARED')
        self.snapshot.assert_not_called()
        self.assertTrue(self.post('arm', arm).get_json()['acquired'])
        self.assertEqual(self.post('arm', dict(arm, recipient_hash='b' * 64)).status_code, 409)

    def test_domains_are_bound_and_checked_for_other_owners(self):
        arm = self.prepare()
        self.assertEqual(self.snapshot.call_args.kwargs['domains'], self.payload['domains'])
        self.assertEqual(self.post('prepare', dict(self.payload, domains=['first.example'])).status_code, 409)
        self.assertEqual(self.post('arm', dict(arm, domains=['first.example'])).status_code, 409)
        self.snapshot.return_value = {'matches': [{'identity_status': 'confirmed',
            'accounts': [{'owner_username': 'amy'}]}]}
        response = self.post('arm', arm)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.snapshot.call_args.kwargs['domains'], self.payload['domains'])
        self.assertEqual(self.conn.row['state'], 'PREPARED')

    def test_domains_validation_and_domain_only_identity(self):
        for domains in ('first.example', [123], ['first.example'] * 13, ['bad domain'], ['person@example.com'], ['x' * 321]):
            with self.subTest(domains=domains):
                self.assertEqual(self.post('prepare', dict(self.payload, domains=domains)).status_code, 400)
        self.assertEqual(self.post('prepare', dict(self.payload, name='', domains=['first.example'] * 12)).status_code, 200)
