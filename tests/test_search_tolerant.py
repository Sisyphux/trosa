import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location('crm_app_tolerant_search_test', ROOT / 'app.py')
app_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app_module)


def hits(tokens, values, fuzzy=False, pinyin=False):
    normalized = [app_module._search_normalize(token) for token in tokens]
    return app_module._search_token_hits(normalized, values, fuzzy=fuzzy, pinyin=pinyin)


class TolerantSearchTokenTests(unittest.TestCase):
    def test_spaces_punctuation_and_accents_do_not_matter(self):
        self.assertEqual(hits(['acme-corp'], ['Acme Corp Ltd']), {'acme-corp'})
        self.assertEqual(hits(['acmecorp'], ['ACME Corp']), {'acmecorp'})
        self.assertEqual(hits(['jose'], ['José García']), {'jose'})
        self.assertEqual(hits(['garcía'], ['Jose Garcia']), {'garcía'})

    def test_phone_numbers_match_across_formatting(self):
        self.assertEqual(hits(['812-3456'], ['+62 812 3456 789']), {'812-3456'})
        self.assertEqual(hits(['+6281234'], ['62 812-34']), {'+6281234'})

    def test_neighbouring_fields_never_join_into_a_false_hit(self):
        self.assertEqual(hits(['abcd'], ['xxab', 'cdxx']), set())

    def test_typos_only_match_when_fuzzy_and_long_enough(self):
        self.assertEqual(hits(['acrylc'], ['Acrylic Sheets']), set())
        self.assertEqual(hits(['acrylc'], ['Acrylic Sheets'], fuzzy=True), {'acrylc'})
        self.assertEqual(hits(['mohamad'], ['Mohamed Ali'], fuzzy=True), {'mohamad'})
        self.assertEqual(hits(['sheest'], ['Acrylic Sheets'], fuzzy=True), {'sheest'})
        # Short words and a wrong first letter stay exact.
        self.assertEqual(hits(['zeta'], ['Zata Trading'], fuzzy=True), set())
        self.assertEqual(hits(['brylic'], ['Acrylic'], fuzzy=True), set())

    def test_unrelated_words_do_not_match(self):
        self.assertEqual(hits(['plastic'], ['Acrylic Sheets'], fuzzy=True), set())

    def test_match_levels_rank_exact_above_tolerant(self):
        level = app_module._search_match_level
        self.assertEqual(level('Zeta Trading', 'zeta'), 2)
        self.assertEqual(level('Acme Corp', 'acme-corp'), 0.5)
        self.assertEqual(level('Acrylic Sheets', 'acrylc'), 0)
        self.assertEqual(level('Acrylic Sheets', 'acrylc', fuzzy=True), 0.25)
        bonus = app_module._CUSTOMER_SEARCH_MATCH_BONUS
        self.assertLess(bonus[0.25], bonus[0.5])
        self.assertLess(bonus[0.5], bonus[1])


class SynonymAndPinyinTests(unittest.TestCase):
    def test_chinese_and_english_terms_find_each_other(self):
        self.assertEqual(hits(['acrylic'], ['亚克力分销']), {'acrylic'})
        self.assertEqual(hits(['亚克力'], ['Acrylic Sheet Distributor']), {'亚克力'})
        self.assertEqual(hits(['pmma'], ['有机玻璃板']), {'pmma'})
        self.assertEqual(hits(['分销商'], ['Acrylic Sheet Distributor']), {'分销商'})
        self.assertEqual(hits(['distributors'], ['亚克力经销商']), {'distributors'})
        self.assertEqual(hits(['laser'], ['激光加工商']), {'laser'})

    def test_english_terms_match_whole_words_only(self):
        self.assertEqual(hits(['sheet'], ['Acrylic Sheets']), {'sheet'})
        self.assertEqual(hits(['wholesaler'], ['Sheeting Co']), set())
        self.assertEqual(hits(['批发'], ['Wholesale Plastics']), {'批发'})
        self.assertEqual(hits(['批发'], ['Wholesalers Inc']), {'批发'})
        self.assertEqual(hits(['板材'], ['Sheeting Co']), set())

    def test_only_a_whole_known_term_expands(self):
        self.assertEqual(hits(['acryl'], ['亚克力']), set())
        self.assertEqual(hits(['banana'], ['亚克力']), set())

    def test_pinyin_finds_chinese_text_only_when_asked(self):
        self.assertEqual(hits(['yakeli'], ['亚克力分销']), set())
        self.assertEqual(hits(['yakeli'], ['亚克力分销'], pinyin=True), {'yakeli'})
        self.assertEqual(hits(['fenxiao'], ['亚克力分销'], pinyin=True), {'fenxiao'})
        self.assertEqual(hits(['zw'], ['张伟'], pinyin=True), {'zw'})
        self.assertEqual(hits(['zhangwei'], ['张伟 Ltd'], pinyin=True), {'zhangwei'})

    def test_pinyin_does_not_overreach(self):
        self.assertEqual(hits(['z'], ['张伟'], pinyin=True), set())
        self.assertEqual(hits(['wl'], ['张伟'], pinyin=True), set())
        self.assertEqual(hits(['yakeli'], ['Acrylic Co'], pinyin=True), set())
        self.assertEqual(hits(['k1'], ['亚克力'], pinyin=True), set())


class TolerantCustomerListTests(unittest.TestCase):
    def customer(self, customer_id, company):
        return {'id': customer_id, 'company': company, 'name': company, 'is_pinned': False, 'pinned_order': 0,
                'updated_at': '2026-09-18 09:00:00', 'customer_type': '', 'tags': '', 'level': '',
                'notes': '', 'profile': '', 'field': '', 'industry': '', 'website': '',
                'business_stage': '', 'business_role': '', 'customer_judgment': '',
                'country': '', 'status': '', 'created_at': '2026-09-18 09:00:00',
                'is_deleted': 0, 'deleted_at': None, 'last_interaction_on': '', 'next_task_on': ''}

    def search(self, query, customers):
        facts = {item['id']: {'next_task_date': '', 'next_task_title': '',
                              'latest_communication_date': '', 'latest_email_date': '',
                              'latest_email_status': '', 'has_contact': False,
                              'contact_state': 'uncontacted', 'waiting_reply': False,
                              'latest_activity': ''} for item in customers}
        kwargs = dict(business_stage='', level='', sort='updated_at', order='desc', include_deleted='0',
                      view='all', country_filter='', business_role='', field_filter='', judgment_filter='',
                      next_state='', source_filter='', last_from='', last_to='', tag_filter='',
                      days_min=0, days_max=0, page_value='', page=1, per_page=30,
                      interpreted_filters=[], silent_days=45, regular_days=75)
        with mock.patch.object(app_module, '_active_customers', return_value=[dict(item) for item in customers]), \
             mock.patch.object(app_module, '_customer_business_facts', return_value=facts), \
             mock.patch.object(app_module, '_customer_contacts', return_value=[]), \
             mock.patch.object(app_module, '_customer_search_match_contexts', return_value={}):
            payload = app_module._get_customers_postgres(
                mock.Mock(), cleaned_search=query, search_tokens=query.split(), **kwargs)
        return [item['id'] for item in payload['customers']]

    def test_loose_and_typo_queries_find_the_customer(self):
        customers = [self.customer(1, 'Acme Corp'), self.customer(2, 'Zeta Acrylic Sheets'),
                     self.customer(3, 'Border Plastics')]
        self.assertEqual(self.search('acme-corp', customers), [1])
        self.assertEqual(self.search('acrylc', customers), [2])
        self.assertEqual(self.search('zeta acrylc', customers), [2])
        self.assertEqual(self.search('nothing', customers), [])

    def test_chinese_english_and_pinyin_queries_find_the_customer(self):
        a = self.customer(1, 'Bright Signs LLC')
        a['field'] = '亚克力分销'
        b = self.customer(2, 'Other Co')
        b['field'] = 'Glass'
        self.assertEqual(self.search('acrylic', [a, b]), [1])
        self.assertEqual(self.search('yakeli', [a, b]), [1])
        self.assertEqual(self.search('acrylic distributor', [a, b]), [1])
        self.assertEqual(self.search('玻璃', [a, b]), [2])

    def test_exact_hit_ranks_before_tolerant_hit(self):
        customers = [self.customer(1, 'Acrylc Works'), self.customer(2, 'Acrylic Works')]
        self.assertEqual(self.search('acrylic', customers), [2, 1])


if __name__ == '__main__':
    unittest.main()
