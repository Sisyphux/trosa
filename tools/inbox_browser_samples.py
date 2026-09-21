"""Generate non-sensitive fixed upload samples for rehearsal-only browser tests."""
from pathlib import Path
import base64
from openpyxl import Workbook


def create(directory):
    target = Path(directory); target.mkdir(parents=True, exist_ok=True)
    (target / 'supported.csv').write_text('Importer,Product description\nFixture Importer,PMMA acrylic sheet import\n', encoding='utf-8')
    book = Workbook(); sheet = book.active; sheet.title = 'Trade rows'; sheet.append(['Importer', 'Product description']); sheet.append(['Fixture Importer', 'coffee beans import']); book.save(target / 'not_supported.xlsx')
    # A tiny text PDF with a visible page-1 trade heading; pypdf extracts it.
    pdf = b'%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n4 0 obj<</Length 54>>stream\nBT /F1 12 Tf 30 250 Td (Shipment record without product description) Tj ET\nendstream endobj\n5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\nxref\n0 6\n0000000000 65535 f \n0000000010 00000 n \n0000000058 00000 n \n0000000115 00000 n \n0000000260 00000 n \n0000000365 00000 n \ntrailer<</Size 6/Root 1 0 R>>\nstartxref\n435\n%%EOF\n'
    (target / 'insufficient.pdf').write_bytes(pdf)
    (target / 'image_without_ocr.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL9WQAAAABJRU5ErkJggg=='))
    (target / 'broken.pdf').write_bytes(b'not a PDF')
    return target
