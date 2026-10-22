"""Customer search understands a country in Chinese and in English.

Searching "瑞典" finds a customer whose country is stored as "Sweden" and the
other way round.  Only an exact country name expands; a partial input such as
"瑞" keeps the plain substring match, and company / e-mail hits are unchanged.
The PostgreSQL counterpart lives in ``test_postgres_rehearsal.py``.
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db
import customer_country_search as country_search


class CountryAliasTableTest(unittest.TestCase):
    def test_exact_names_expand_partial_input_does_not(self):
        self.assertIn('sweden', country_search.country_alias_keys('瑞典'))
        self.assertIn('瑞典', country_search.country_alias_keys('  SWEDEN '))
        self.assertIn('荷兰', country_search.country_alias_keys('The Netherlands'))
        self.assertEqual(country_search.country_alias_keys('瑞'), frozenset())
        self.assertEqual(country_search.country_alias_keys('中'), frozenset())
        self.assertEqual(country_search.country_alias_keys('Swed'), frozenset())

    def test_table_covers_the_display_table_scope(self):
        groups = country_search.COUNTRY_SEARCH_GROUPS
        self.assertEqual(len(groups), 138)
        self.assertEqual(sum(len(spellings) for spellings in groups.values()), 178)
        for abbreviation, chinese in (('USA', '美国'), ('US', '美国'), ('UAE', '阿联酋'), ('UK', '英国')):
            self.assertIn(chinese, country_search.country_alias_keys(abbreviation))

    def test_whole_query_country_covers_each_word(self):
        aliases = country_search.country_aliases_for_tokens('United States', ['United', 'States'])
        self.assertIn('美国', aliases['United'])
        self.assertIn('美国', aliases['States'])
        self.assertEqual(country_search.country_aliases_for_tokens('Grupo 瑞', ['Grupo', '瑞']), {})


class CustomerCountrySearchTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        self.original_demo = os.environ.get('CRM_SEED_DEMO_DATA')
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.init_all_dbs()
        spec = importlib.util.spec_from_file_location('crm_app_country_search_test', ROOT / 'app.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.client = self.module.app.test_client()
        self.assertEqual(self.client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)
        # Stored values exactly as they arrive from imports (not via normalize_country).
        self.ids = {}
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            for key, company, country, email in (
                ('se_en', 'Nordic Pack AB', 'Sweden', 'buyer@nordicpack.se'),
                ('se_zh', 'Stockholm Box AB', '瑞典', 'info@stockholmbox.se'),
                ('us_abbr', 'Grupo Liberty Inc', 'USA', 'sales@liberty.com'),
                ('us_full', 'Texas Crate LLC', 'United States', 'hello@texascrate.com'),
                ('us_zh', 'Ohio Carton Co', '美国', 'buy@ohiocarton.com'),
                ('ae_abbr', 'Gulf Trade FZE', 'UAE', 'ops@gulftrade.ae'),
                ('ae_zh', 'Sharjah Pack LLC', '阿联酋', 'pack@sharjah.ae'),
                ('mx', 'Grupo Monterrey', 'Mexico', 'compras@monterrey.mx'),
                ('nl', 'Delft Paper BV', 'The Netherlands', 'inkoop@delftpaper.nl'),
                ('ru', 'Ruishi Trading', '俄罗斯', 'ruishi@trade.ru'),
            ):
                cursor = conn.execute(
                    "INSERT INTO customers (name, company, country, field, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (company + ' Buyer', company, country, '包装', '2026-01-01', '2026-01-01'))
                self.ids[key] = cursor.lastrowid
                conn.execute("INSERT INTO contacts (customer_id, name, email, is_primary) VALUES (?,?,?,1)",
                             (cursor.lastrowid, company + ' Contact', email))
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        db.cancel_safety_backup()
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    def _list(self, query, **extra):
        response = self.client.get('/api/customers', query_string={'search': query, **extra})
        self.assertEqual(response.status_code, 200, response.get_json())
        return {item['id'] for item in response.get_json()['customers']}

    def _ledger(self, query):
        response = self.client.get('/api/customers/ledger', query_string={'search': query})
        self.assertEqual(response.status_code, 200, response.get_json())
        return {entry[0] for entry in response.get_json()['index']}

    def _ids(self, *keys):
        return {self.ids[key] for key in keys}

    def test_chinese_name_finds_english_country_and_back(self):
        sweden = self._ids('se_en', 'se_zh')
        for query in ('瑞典', 'Sweden', 'sweden', '  SWEDEN  '):
            self.assertEqual(self._list(query), sweden, query)
            self.assertEqual(self._ledger(query), sweden, query)
        # The Swedish row is ranked as a country hit, not as an unexplained result.
        payload = self.client.get('/api/customers', query_string={'search': '瑞典'}).get_json()
        english = next(item for item in payload['customers'] if item['id'] == self.ids['se_en'])
        self.assertEqual(english['match_context']['field'], 'country')
        self.assertEqual(english['country'], 'Sweden')

    def test_united_states_and_uae_spellings_are_one_country(self):
        united_states = self._ids('us_abbr', 'us_full', 'us_zh')
        for query in ('美国', 'USA', 'usa', 'United States', 'the United States'):
            self.assertEqual(self._list(query), united_states, query)
        uae = self._ids('ae_abbr', 'ae_zh')
        for query in ('阿联酋', 'UAE', 'United Arab Emirates'):
            self.assertEqual(self._list(query), uae, query)
        self.assertEqual(self._list('荷兰'), self._ids('nl'))
        self.assertEqual(self._list('墨西哥'), self._ids('mx'))

    def test_partial_input_keeps_substring_matching(self):
        # "瑞" only matches the stored Chinese spelling, exactly as before.
        self.assertEqual(self._list('瑞'), self._ids('se_zh'))
        self.assertEqual(self._list('Swed'), self._ids('se_en'))

    def test_company_and_email_hits_are_unchanged(self):
        self.assertEqual(self._list('Grupo'), self._ids('us_abbr', 'mx'))
        self.assertEqual(self._list('nordicpack'), self._ids('se_en'))
        self.assertEqual(self._list('@sharjah'), self._ids('ae_zh'))
        # A country word combined with another word still needs both to match.
        self.assertEqual(self._list('Grupo 墨西哥'), self._ids('mx'))
        self.assertEqual(self._list('Grupo 美国'), self._ids('us_abbr'))

    def test_country_filter_and_overview_also_understand_both_languages(self):
        self.assertEqual(self._list('', country='瑞典'), self._ids('se_en', 'se_zh'))
        self.assertEqual(self._list('', country='Swed'), self._ids('se_en'))
        overview = self.client.get('/api/overview/all-customers', query_string={'search': '瑞典'}).get_json()
        hamid = {item['id'] for item in overview['customers'] if item['owner'] == 'hamid'}
        self.assertEqual(hamid, self._ids('se_en', 'se_zh'))


if __name__ == '__main__':
    unittest.main()
