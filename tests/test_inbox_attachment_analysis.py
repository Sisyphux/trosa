import os
import tempfile
import unittest

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

    def test_invalid_file_is_analysis_failed(self):
        path = self._file('broken.pdf', b'not a pdf')
        self.assertEqual(analyze_import_file(path, 'broken.pdf')['status'], 'analysis_failed')
