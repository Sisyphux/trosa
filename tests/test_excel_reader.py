import datetime
import os
import tempfile
import unittest

from excel_reader import cell_text, read_sheets


class ExcelReaderTest(unittest.TestCase):
    def test_excel_2003_xml_with_index_gaps_is_readable(self):
        xml = '''<?xml version="1.0"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
          xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
  <Worksheet ss:Name="Imports">
    <Table>
      <Row><Cell><Data ss:Type="String">Importer</Data></Cell>
           <Cell><Data ss:Type="String">Product</Data></Cell></Row>
      <Row><Cell><Data ss:Type="String">Acme</Data></Cell>
           <Cell ss:Index="3"><Data ss:Type="String">PMMA sheet</Data></Cell></Row>
    </Table>
  </Worksheet>
</Workbook>'''
        path = os.path.join(tempfile.mkdtemp(), 'export.xls')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(xml)
        sheets = read_sheets(path)
        self.assertEqual(sheets[0][0], 'Imports')
        self.assertEqual(sheets[0][1][1], ['Acme', None, 'PMMA sheet'])

    def test_cell_text_normalizes_dates_and_whole_numbers(self):
        self.assertEqual(cell_text(datetime.datetime(2024, 3, 15)), '2024-03-15')
        self.assertEqual(cell_text(datetime.datetime(2024, 3, 15, 8, 30)), '2024-03-15 08:30')
        self.assertEqual(cell_text(1200.0), '1200')
        self.assertEqual(cell_text(12.5), '12.5')

    def test_unknown_binary_is_rejected(self):
        path = os.path.join(tempfile.mkdtemp(), 'random.xls')
        with open(path, 'wb') as handle:
            handle.write(b'\x00\x01\x02 binary')
        with self.assertRaises(ValueError):
            read_sheets(path)
