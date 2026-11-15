"""The account guard must see Sela provenance on the initial account insert."""
import json
import unittest
from unittest import mock

import trosa_domain


class _StopAtAccount(Exception):
    pass


class _Connection:
    def __init__(self):
        self.account_params = None

    def execute(self, sql, params=()):
        if 'INSERT INTO trosa.accounts' in sql:
            self.account_params = params
            raise _StopAtAccount
        return self

    def fetchone(self):
        return (1,)


class SelaCustomerAccountMarkerTests(unittest.TestCase):
    def test_sela_marker_is_present_at_initial_account_insert(self):
        connection = _Connection()
        with mock.patch.object(trosa_domain, 'postgres_mode', return_value=True):
            with self.assertRaises(_StopAtAccount):
                trosa_domain.create_customer(connection, values={
                    'company': 'Example', 'import_source': 'sela-v2', 'external_source': 'sela',
                })
        payload = json.loads(connection.account_params[-1])
        self.assertEqual(payload['import_source'], 'sela-v2')
        self.assertEqual(payload['external_source'], 'sela')

    def test_manual_and_partial_markers_do_not_identify_sela(self):
        for values in ({}, {'import_source': 'sela-v2'}, {'external_source': 'sela'}):
            with self.subTest(values=values):
                connection = _Connection()
                with mock.patch.object(trosa_domain, 'postgres_mode', return_value=True):
                    with self.assertRaises(_StopAtAccount):
                        trosa_domain.create_customer(connection, values=values)
                self.assertEqual(json.loads(connection.account_params[-1]), {})
