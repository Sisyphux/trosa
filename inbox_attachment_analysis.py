"""Deterministic, auditable extraction for Inbox investigation attachments.

This deliberately does not ask a model to invent a finding.  Every returned
fact has a sheet/row, page, or text excerpt citation and the conclusion is
``insufficient`` unless explicit import evidence is present.
"""
import csv
import io
import os
import re
from zipfile import ZipFile

_RELATED = re.compile(r'\b(pm{1,2}a|acrylic|plexiglas|plastic\s+sheet)\b', re.I)
_IMPORT = re.compile(r'\b(import|importer|consignee|shipment|bill\s+of\s+lading)\b', re.I)

def _csv_rows(raw):
    text = raw.decode('utf-8-sig', 'replace')
    return list(csv.reader(io.StringIO(text)))

def _xlsx_rows(raw):
    # Keep a dependency-free CSV-like view of OOXML shared strings/sheets.
    # The raw XML excerpts remain citations when richer spreadsheet tooling is
    # absent in a development install.
    with ZipFile(io.BytesIO(raw)) as zf:
        names = [n for n in zf.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml')]
        return [(os.path.basename(name), re.sub(r'<[^>]+>', ' ', zf.read(name).decode('utf-8', 'replace'))) for name in names]

def analyze_import_file(path, filename=''):
    ext = os.path.splitext(filename or path)[1].lower()
    raw = open(path, 'rb').read()
    citations, lines = [], []
    try:
        if ext == '.csv':
            for number, row in enumerate(_csv_rows(raw), 1):
                text = ' | '.join(row).strip()
                if text: lines.append(('CSV', number, text))
        elif ext in ('.xlsx', '.xls') and ext == '.xlsx':
            for sheet, text in _xlsx_rows(raw): lines.append((sheet, 1, text))
        elif ext == '.pdf':
            try:
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(raw))
                lines = [('PDF', i + 1, page.extract_text() or '') for i, page in enumerate(reader.pages)]
            except Exception as exc: return {'status': 'analysis_failed', 'error': 'PDF 提取失败：' + str(exc), 'facts': [], 'citations': []}
        elif ext in ('.png', '.jpg', '.jpeg'):
            return {'status': 'insufficient', 'facts': [], 'citations': [], 'missing': ['请提供可读取的表格/PDF，或人工填写截图中的进口记录。']}
        else: return {'status': 'analysis_failed', 'error': '不支持的证据格式', 'facts': [], 'citations': []}
    except Exception as exc:
        return {'status': 'analysis_failed', 'error': '文件解析失败：' + str(exc), 'facts': [], 'citations': []}
    related = []
    for source, number, text in lines:
        if _RELATED.search(text) and _IMPORT.search(text):
            excerpt = re.sub(r'\s+', ' ', text).strip()[:500]
            related.append(excerpt); citations.append({'source': source, 'row_or_page': number, 'excerpt': excerpt})
    if related:
        return {'status': 'supported', 'facts': ['发现与 PMMA/亚克力板有关的进口记录'], 'citations': citations,
                'confidence': 'medium', 'missing': [], 'next_action': '解除调查阻塞；仅准备外联，不自动发送。'}
    return {'status': 'insufficient', 'facts': [], 'citations': [], 'confidence': 'low',
            'missing': ['过去 24 个月内含产品描述与进口主体的记录'], 'next_action': '保留问题，等待补充证据。'}
