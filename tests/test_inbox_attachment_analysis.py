import os
import tempfile
import unittest

from openpyxl import Workbook

from inbox_attachment_analysis import analyze_import_file


class InboxAttachmentAnalysisTest(unittest.TestCase):
    def _file(self, name, content):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, name)
        with open(path, 'wb') as handle:
            handle.write(content)
        return path

    def test_csv_import_evidence_is_supported_with_row_citation(self):
        path = self._file('imports.csv', b'company,product,shipment\nAcme,PMMA acrylic sheet,import shipment\n')
        result = analyze_import_file(path, 'imports.csv')
        self.assertEqual(result['status'], 'supported')
        self.assertEqual(result['citations'][0]['source'], 'CSV')
        self.assertEqual(result['citations'][0]['row_or_page'], 2)

    def test_unrelated_csv_is_insufficient(self):
        path = self._file('notes.csv', b'company,notes\nAcme,met at trade fair\n')
        self.assertEqual(analyze_import_file(path, 'notes.csv')['status'], 'insufficient')

    def test_unrelated_import_record_is_not_supported_with_citation(self):
        path = self._file('imports.csv', b'company,product,shipment\nAcme,coffee beans,import shipment\n')
        result = analyze_import_file(path, 'imports.csv')
        self.assertEqual(result['status'], 'not_supported')
        self.assertEqual(result['citations'][0]['row_or_page'], 2)

    def test_invalid_file_is_analysis_failed(self):
        path = self._file('broken.pdf', b'not a pdf')
        self.assertEqual(analyze_import_file(path, 'broken.pdf')['status'], 'analysis_failed')

    def test_xlsx_retains_sheet_name_row_and_field_citations(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, 'imports.xlsx')
        workbook = Workbook()
        workbook.active.title = 'Imports 2026'
        workbook.active.append(['Importer', 'Product description', 'HS code', 'Weight', 'Amount'])
        workbook.active.append(['Acme Imports', 'PMMA acrylic sheet', '392051', '1200 kg', 'USD 5000'])
        workbook.create_sheet('Notes').append(['Note'])
        workbook.save(path)
        result = analyze_import_file(path, 'imports.xlsx')
        self.assertEqual(result['status'], 'supported')
        self.assertEqual(result['citations'][0]['source'], 'Imports 2026')
        self.assertEqual(result['citations'][0]['row_or_page'], 2)
        self.assertTrue(any(fact['field'] == 'importer' and fact['value'] == 'Acme Imports'
                            for fact in result['field_facts']))
        self.assertTrue(all('method' in fact['citation'] for fact in result['field_facts']))

    def test_image_is_insufficient_when_controlled_ocr_is_unavailable(self):
        path = self._file('shipping.jpg', b'not decoded here')
        result = analyze_import_file(path, 'shipping.jpg')
        self.assertEqual(result['status'], 'insufficient')
        self.assertIn('OCR', result['missing'][0])

    def test_legacy_xls_reads_sheet_rows_chinese_headers_and_dates(self):
        path = os.path.join(os.path.dirname(__file__), 'fixtures', 'legacy_customs.xls')
        result = analyze_import_file(path, 'legacy_customs.xls')
        self.assertEqual(result['status'], 'supported')
        self.assertEqual(result['citations'][0]['source'], 'US Imports 2024')
        self.assertEqual(result['citations'][0]['row_or_page'], 3)
        facts = {(fact['field'], fact['value']) for fact in result['field_facts']}
        self.assertIn(('importer', 'TAP Plastics'), facts)
        self.assertIn(('product_description', '亚克力板 1.5mm'), facts)
        self.assertIn(('date', '2024-03-15'), facts)

    def test_html_table_disguised_as_xls_is_readable(self):
        html = ('<html><body><table><tr><th>Importer</th><th>Product description</th></tr>'
                '<tr><td>Acme Imports</td><td>PMMA acrylic sheet</td></tr></table></body></html>')
        path = self._file('customs.xls', html.encode('utf-8'))
        result = analyze_import_file(path, 'customs.xls')
        self.assertEqual(result['status'], 'supported')
        self.assertEqual(result['citations'][0]['row_or_page'], 2)

    def test_tab_separated_text_disguised_as_xls_is_readable(self):
        content = '进口商\t商品描述\nTAP Plastics\t亚克力板\n'.encode('utf-8')
        path = self._file('customs.xls', content)
        result = analyze_import_file(path, 'customs.xls')
        self.assertEqual(result['status'], 'supported')
        self.assertEqual(result['citations'][0]['row_or_page'], 2)

    def test_unreadable_legacy_xls_fails_with_actionable_error(self):
        path = self._file('broken.xls', b'\x00\x01legacy binary that is not a table')
        result = analyze_import_file(path, 'broken.xls')
        self.assertEqual(result['status'], 'analysis_failed')
        self.assertIn('Excel', result['error'])
