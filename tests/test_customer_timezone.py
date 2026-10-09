"""Customer timezone: country inference, manual override, and reset.

Timezone is a system-inferred display fact.  These regressions run against the
isolated SQLite development boundary and cover the whole product loop:

* the deterministic country -> IANA inference table (including aliases,
  multi-timezone defaults, and values that must stay empty),
* inference on create and on a country change,
* a manual override that is never replaced again by inference,
* reset back to inference,
* rejection of an illegal IANA name,
* cross-user read isolation.
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
import customer_timezone


def load_app():
    spec = importlib.util.spec_from_file_location('trosa_customer_timezone_test', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


class CustomerTimezoneInferenceTest(unittest.TestCase):
    def test_common_countries(self):
        infer = customer_timezone.infer_timezone
        self.assertEqual(infer('美国'), 'America/New_York')
        self.assertEqual(infer('阿联酋'), 'Asia/Dubai')
        self.assertEqual(infer('印度'), 'Asia/Kolkata')
        self.assertEqual(infer('英国'), 'Europe/London')
        self.assertEqual(infer('德国'), 'Europe/Berlin')
        self.assertEqual(infer('沙特阿拉伯'), 'Asia/Riyadh')

    def test_multi_timezone_countries_use_default(self):
        infer = customer_timezone.infer_timezone
        self.assertEqual(infer('美国'), 'America/New_York')
        self.assertEqual(infer('澳大利亚'), 'Australia/Sydney')
        self.assertEqual(infer('加拿大'), 'America/Toronto')
        self.assertEqual(infer('巴西'), 'America/Sao_Paulo')
        self.assertEqual(infer('俄罗斯'), 'Europe/Moscow')

    def test_aliases(self):
        infer = customer_timezone.infer_timezone
        self.assertEqual(infer('US'), 'America/New_York')
        self.assertEqual(infer('usa'), 'America/New_York')
        self.assertEqual(infer('United States'), 'America/New_York')
        self.assertEqual(infer('沙特'), 'Asia/Riyadh')
        self.assertEqual(infer('Brazil'), 'America/Sao_Paulo')
        self.assertEqual(infer('Ecuador'), 'America/Guayaquil')

    def test_blank_and_worldwide_stay_empty(self):
        infer = customer_timezone.infer_timezone
        for value in ('', None, '   ', '全球'):
            self.assertEqual(infer(value), '', f'{value!r} should stay empty')

    def test_messy_values_resolve_to_most_likely_zone(self):
        infer = customer_timezone.infer_timezone
        cases = {
            '迪拜': 'Asia/Dubai',
            'Dubai': 'Asia/Dubai',
            '马来西亚？': 'Asia/Kuala_Lumpur',
            '美国\n加州': 'America/New_York',
            '美国/加拿大': 'America/New_York',
            '美国/土耳其': 'America/New_York',
            '美国犹他州': 'America/New_York',
            '美国\n（佛罗里达州）': 'America/New_York',
            '西班牙马德里': 'Europe/Madrid',
            '埃及（2%）': 'Africa/Cairo',
            '菲律宾（6%，普通10%）': 'Asia/Manila',
            '苏里南\n南美': 'America/Paramaribo',
            'Dominican Republic多米尼加共和国': 'America/Santo_Domingo',
        }
        for value, expected in cases.items():
            self.assertEqual(infer(value), expected, f'{value!r}')

    def test_newly_mapped_countries(self):
        infer = customer_timezone.infer_timezone
        cases = {
            '加纳': 'Africa/Accra',
            'Ghana': 'Africa/Accra',
            'Jamaica': 'America/Jamaica',
            'Kenya': 'Africa/Nairobi',
            'Uruguay': 'America/Montevideo',
            'Paraguay': 'America/Asuncion',
            'Bolivia': 'America/La_Paz',
            'Nicaragua': 'America/Managua',
            'Austria': 'Europe/Vienna',
            'Barbados': 'America/Barbados',
            'Belize': 'America/Belize',
            'El Salvador': 'America/El_Salvador',
            'Grenada': 'America/Grenada',
            'Guyana': 'America/Guyana',
            'Bahamas': 'America/Nassau',
            'Aruba': 'America/Aruba',
            'Antigua and Barbuda': 'America/Antigua',
            'Bangladesh': 'Asia/Dhaka',
            'Botswana': 'Africa/Gaborone',
            'China': 'Asia/Shanghai',
            'Norway': 'Europe/Oslo',
            'Saint Lucia': 'America/St_Lucia',
            'Sri Lanka': 'Asia/Colombo',
            'Tanzania': 'Africa/Dar_es_Salaam',
            'Uganda': 'Africa/Kampala',
            'Trinidad and Tobago': 'America/Port_of_Spain',
        }
        for value, expected in cases.items():
            self.assertEqual(infer(value), expected, f'{value!r}')

    def test_known_timezone_validation(self):
        self.assertTrue(customer_timezone.is_known_timezone('America/New_York'))
        self.assertTrue(customer_timezone.is_known_timezone('UTC'))
        self.assertFalse(customer_timezone.is_known_timezone('Mars/Phobos'))
        self.assertFalse(customer_timezone.is_known_timezone(''))
        self.assertEqual(customer_timezone.normalize_timezone('Europe/London'), 'Europe/London')
        self.assertEqual(customer_timezone.normalize_timezone('not a zone'), '')


class CustomerTimezoneApiTest(unittest.TestCase):
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

    def put_customer(self, customer_id, payload):
        return self.client.put(f'/api/customers/{customer_id}', json=payload)

    def test_create_infers_timezone_from_country(self):
        customer_id = self.create_customer('推断客户', country='美国')
        customer = self.get_customer(customer_id)
        self.assertEqual(customer['timezone'], 'America/New_York')
        self.assertEqual(customer['timezone_source'], 'inferred')

    def test_create_with_unrecognized_country_leaves_timezone_empty(self):
        customer_id = self.create_customer('未知国家客户', country='全球')
        customer = self.get_customer(customer_id)
        self.assertEqual(customer['timezone'], '')
        self.assertEqual(customer['timezone_source'], '')

    def test_create_infers_timezone_from_messy_country(self):
        customer_id = self.create_customer('加州客户', country='美国\n加州')
        customer = self.get_customer(customer_id)
        self.assertEqual(customer['timezone'], 'America/New_York')
        self.assertEqual(customer['timezone_source'], 'inferred')

    def test_manual_override_is_not_replaced_by_country_change(self):
        customer_id = self.create_customer('手动时区客户', country='美国')
        response = self.put_customer(customer_id, {'timezone': 'Europe/London'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        customer = self.get_customer(customer_id)
        self.assertEqual(customer['timezone'], 'Europe/London')
        self.assertEqual(customer['timezone_source'], 'manual')

        # Changing the country must not silently overwrite a manual value.
        response = self.put_customer(customer_id, {'country': '德国'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        customer = self.get_customer(customer_id)
        self.assertEqual(customer['timezone'], 'Europe/London')
        self.assertEqual(customer['timezone_source'], 'manual')

    def test_country_change_reinfers_an_inferred_timezone(self):
        customer_id = self.create_customer('重算时区客户', country='美国')
        self.assertEqual(self.get_customer(customer_id)['timezone'], 'America/New_York')
        response = self.put_customer(customer_id, {'country': '德国'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        customer = self.get_customer(customer_id)
        self.assertEqual(customer['timezone'], 'Europe/Berlin')
        self.assertEqual(customer['timezone_source'], 'inferred')

    def test_invalid_iana_name_is_rejected(self):
        customer_id = self.create_customer('非法时区客户', country='美国')
        response = self.put_customer(customer_id, {'timezone': 'Mars/Phobos'})
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        self.assertEqual(self.get_customer(customer_id)['timezone'], 'America/New_York')

    def test_empty_timezone_resets_to_inference(self):
        customer_id = self.create_customer('重置时区客户', country='美国')
        self.put_customer(customer_id, {'timezone': 'Europe/London'})
        response = self.put_customer(customer_id, {'timezone': ''})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        customer = self.get_customer(customer_id)
        self.assertEqual(customer['timezone'], 'America/New_York')
        self.assertEqual(customer['timezone_source'], 'inferred')

    def test_summary_and_ledger_expose_timezone(self):
        customer_id = self.create_customer('列表时区客户', country='澳大利亚')
        summary = self.client.get(f'/api/customers/{customer_id}/summary')
        self.assertEqual(summary.status_code, 200, summary.get_data(as_text=True))
        self.assertEqual(summary.get_json()['timezone'], 'Australia/Sydney')

        ledger = self.client.get(f'/api/customers/ledger/rows?ids={customer_id}')
        self.assertEqual(ledger.status_code, 200, ledger.get_data(as_text=True))
        rows = ledger.get_json()['rows']
        self.assertEqual(rows[0]['timezone'], 'Australia/Sydney')
        self.assertEqual(rows[0]['timezone_source'], 'inferred')

    def test_cross_user_read_isolation(self):
        customer_id = self.create_customer('私有客户', country='美国')
        other = self.module.app.test_client()
        self.assertEqual(other.post('/api/auth/login', json={'user': 'amy'}).status_code, 200)
        response = other.get(f'/api/customers/{customer_id}')
        self.assertEqual(response.status_code, 404)
        # A cross-user write must not succeed either.
        response = other.put(f'/api/customers/{customer_id}', json={'timezone': 'Europe/London'})
        self.assertIn(response.status_code, (403, 404))


if __name__ == '__main__':
    unittest.main()
