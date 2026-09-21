"""Deterministic, auditable extraction for Inbox investigation attachments.

This reader deliberately makes no model call. A conclusion needs a record with
both a trade signal and product description; every extracted value retains the
source location and original excerpt that produced it.
"""
import csv
import io
import os
import re

from openpyxl import load_workbook

_PRODUCT = re.compile(r'\b(pm{1,2}a|acrylic|plexiglas|plastic\s+sheet)\b', re.I)
_TRADE = re.compile(r'\b(import|importer|consignee|shipment|bill\s+of\s+lading|export|exporter)\b', re.I)
_HS = re.compile(r'\b(?:hs\s*(?:code)?\s*[:#-]?\s*)?(?:39\d{4,8}|\d{6,10})\b', re.I)
_DATE = re.compile(r'\b(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]20\d{2})\b')
_MONEY = re.compile(r'(?:\$|USD|EUR|CNY|RMB)\s?[\d,]+(?:\.\d+)?', re.I)
_WEIGHT = re.compile(r'\b[\d,.]+\s*(?:kg|kgs|kilograms?|lb|lbs|tons?|mt)\b', re.I)
_QUANTITY = re.compile(r'\b[\d,.]+\s*(?:pcs?|pieces?|sheets?|units?|rolls?)\b', re.I)
_FIELD_ALIASES = {
    'company_or_entity': ('company', 'entity', 'supplier', 'shipper', 'buyer', 'consignee', 'name'),
    'importer': ('importer', 'consignee', 'buyer'), 'exporter': ('exporter', 'supplier', 'shipper', 'seller'),
    'product_description': ('product', 'description', 'commodity', 'goods', 'item'),
    'hs_code': ('hs', 'tariff'), 'country_or_region': ('country', 'origin', 'destination', 'region'),
    'date': ('date', 'shipment date', 'import date', 'export date'),
    'quantity': ('quantity', 'qty', 'pieces', 'units'), 'weight': ('weight', 'net weight', 'gross weight'),
    'amount': ('amount', 'value', 'price', 'usd', 'total'), 'unit': ('unit', 'uom'),
}


def _csv_rows(raw):
    return list(csv.reader(io.StringIO(raw.decode('utf-8-sig', 'replace'))))


def _tabular_records(rows, source):
    if not rows:
        return []
    headers = [str(value or '').strip() for value in rows[0]]
    records = []
    for row_number, row in enumerate(rows[1:], 2):
        values = [str(value or '').strip() for value in row]
        text = ' | '.join(values).strip()
        if not text:
            continue
        fields = {}
        for field, aliases in _FIELD_ALIASES.items():
            for index, header in enumerate(headers):
                if index < len(values) and any(alias in header.casefold() for alias in aliases) and values[index]:
                    fields[field] = values[index]
                    break
        for field, pattern in (('hs_code', _HS), ('date', _DATE), ('amount', _MONEY), ('weight', _WEIGHT), ('quantity', _QUANTITY)):
            match = pattern.search(text)
            if match and field not in fields:
                fields[field] = match.group(0)
        records.append({'source': source, 'row_or_page': row_number, 'text': text[:1000], 'fields': fields})
    return records


def _xlsx_records(raw):
    workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    records = []
    for sheet in workbook.worksheets:
        records.extend(_tabular_records(list(sheet.iter_rows(values_only=True)), sheet.title))
    return records


def _pdf_records(raw):
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, '此环境未安装 PDF 文本读取器；无法安全分析 PDF。'
    reader = PdfReader(io.BytesIO(raw))
    records = []
    for page_number, page in enumerate(reader.pages, 1):
        text = re.sub(r'\s+', ' ', page.extract_text() or '').strip()
        if text:
            records.append({'source': 'PDF', 'row_or_page': page_number, 'text': text[:1000], 'fields': {}})
    return records, ''


def _citation(record):
    return {'source': record['source'], 'row_or_page': record['row_or_page'], 'excerpt': record['text'],
            'method': 'deterministic_tabular_or_text_reader', 'confidence': 'high'}


def analyze_import_file(path, filename=''):
    """Analyze one local user file and return only evidence-backed conclusions."""
    ext = os.path.splitext(filename or path)[1].lower()
    try:
        with open(path, 'rb') as handle:
            raw = handle.read()
        if ext == '.csv':
            records = _tabular_records(_csv_rows(raw), 'CSV')
        elif ext == '.xlsx':
            records = _xlsx_records(raw)
        elif ext == '.xls':
            return {'status': 'analysis_failed', 'error': '旧 XLS 格式不受支持；请另存为 XLSX 或 CSV 后重试。', 'facts': [], 'citations': [], 'field_facts': []}
        elif ext == '.pdf':
            records, error = _pdf_records(raw)
            if records is None:
                return {'status': 'analysis_failed', 'error': error, 'facts': [], 'citations': [], 'field_facts': []}
        elif ext in ('.png', '.jpg', '.jpeg'):
            return {'status': 'insufficient', 'error': '', 'facts': [], 'citations': [], 'field_facts': [],
                    'missing': ['当前环境没有受控 OCR 读取能力；请上传 CSV、XLSX 或文本型 PDF，或人工填写截图中的记录。'],
                    'next_action': '保留问题，等待可审计的文字证据。'}
        else:
            return {'status': 'analysis_failed', 'error': '不支持的证据格式', 'facts': [], 'citations': [], 'field_facts': []}
    except Exception as exc:
        return {'status': 'analysis_failed', 'error': '文件解析失败：' + str(exc), 'facts': [], 'citations': [], 'field_facts': []}
    field_facts = [{'field': key, 'value': value, 'citation': _citation(record)} for record in records for key, value in record['fields'].items()]
    # Column names are meaningful evidence too: a row below an ``Importer``
    # header is a trade record even when its cell value does not repeat that
    # word.  Product matching remains confined to the same row.
    trade_records = [record for record in records if _TRADE.search(record['text']) or
                     record['fields'].get('importer') or record['fields'].get('exporter')]
    supported = [record for record in trade_records if _PRODUCT.search(record['text']) or
                 _PRODUCT.search(record['fields'].get('product_description', ''))]
    if supported:
        return {'status': 'supported', 'facts': ['发现含进口/出口主体和 PMMA/亚克力产品描述的记录'],
                'citations': [_citation(record) for record in supported], 'field_facts': field_facts,
                'confidence': 'medium', 'missing': [],
                'matching_basis': '同一可读记录同时包含贸易角色与目标产品描述；未仅依据单个关键词判断。',
                'next_action': '解除调查阻塞；仅准备外联，不自动发送。'}
    if trade_records:
        return {'status': 'not_supported', 'facts': ['发现可读的贸易记录，但没有目标 PMMA/亚克力产品描述'],
                'citations': [_citation(record) for record in trade_records], 'field_facts': field_facts,
                'confidence': 'medium', 'missing': [],
                'matching_basis': '存在贸易记录，但相关产品字段或文本明确未支持当前调查判断。',
                'next_action': '记录调查结论并按规则降低开发优先级；不自动发送。'}
    return {'status': 'insufficient', 'facts': [], 'citations': [], 'field_facts': field_facts, 'confidence': 'low',
            'missing': ['过去 24 个月内含产品描述与进口主体的记录'],
            'matching_basis': '文件可读，但没有同时满足贸易主体和产品描述的可审计记录。',
            'next_action': '保留问题，等待补充证据。'}
