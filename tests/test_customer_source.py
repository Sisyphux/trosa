"""Customer source is a user-authored fact with a fixed option list.

These regressions run against the isolated SQLite development boundary and
cover the whole product loop: create with an optional source, read it back,
edit it, filter the list by it, and reject anything outside the option list.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_customer_source_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


class CustomerSourceTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        self.original_demo = os.environ.get('CRM_SEED_DEMO_DATA')
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.init_all_dbs()
        self.module = load_app()
        self.client = self.module.app.test_client()
        with self.client.session_transaction() as session:
            session['user'] = 'hamid'

    def tearDown(self):
        db.cancel_safety_backup()
        db.set_db_user(None)
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    def create_customer(self, name, **fields):
        response = self.client.post('/api/customers', json={'name': name, 'company': name, **fields})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()['id']

    def get_customer(self, customer_id):
        response = self.client.get(f'/api/customers/{customer_id}')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def test_option_normalization_and_rejection(self):
        normalize = self.module._normalize_customer_source
        self.assertEqual(normalize(''), '')
        self.assertEqual(normalize('展会'), '展会')
        self.assertEqual(normalize('LinkedIn'), 'LinkedIn')
        self.assertEqual(normalize('linkedin'), 'LinkedIn')
        self.assertEqual(normalize('customs data'), '海关数据')
        with self.assertRaises(self.module.CrmWriteError):
            normalize('随机来源')

    def test_create_is_optional_and_round_trips(self):
        plain = self.create_customer('无来源客户')
        self.assertEqual(self.get_customer(plain)['source'], '')

        sourced = self.create_customer(
            '展会客户', source='展会', source_detail='SIGN CHINA 2026',
        )
        customer = self.get_customer(sourced)
        self.assertEqual(customer['source'], '展会')
        self.assertEqual(customer['source_detail'], 'SIGN CHINA 2026')

    def test_create_rejects_unknown_source(self):
        response = self.client.post('/api/customers', json={'name': '错误来源', 'source': '猜的'})
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))

    def test_update_changes_and_clears_source(self):
        customer_id = self.create_customer('可修改来源', source='展会')
        response = self.client.put(
            f'/api/customers/{customer_id}',
            json={'source': 'LinkedIn', 'source_detail': '采购负责人主页'},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        customer = self.get_customer(customer_id)
        self.assertEqual(customer['source'], 'LinkedIn')
        self.assertEqual(customer['source_detail'], '采购负责人主页')

        cleared = self.client.put(f'/api/customers/{customer_id}', json={'source': '', 'source_detail': ''})
        self.assertEqual(cleared.status_code, 200, cleared.get_data(as_text=True))
        customer = self.get_customer(customer_id)
        self.assertEqual(customer['source'], '')
        self.assertEqual(customer['source_detail'], '')

    def test_partial_update_preserves_source(self):
        customer_id = self.create_customer('局部更新', source='客户转介绍')
        response = self.client.put(f'/api/customers/{customer_id}', json={'notes': '只改备注'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.get_customer(customer_id)['source'], '客户转介绍')

    def test_list_filters_by_source(self):
        exhibition = self.create_customer('展会客户A', source='展会')
        self.create_customer('领英客户B', source='LinkedIn')
        self.create_customer('无来源客户C')

        response = self.client.get('/api/customers', query_string={'source': '展会'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        ids = {item['id'] for item in payload['customers']}
        self.assertIn(exhibition, ids)
        self.assertTrue(all(item.get('source') == '展会' for item in payload['customers']))


if __name__ == '__main__':
    unittest.main()
