"""Deterministic, auditable extraction for Inbox investigation attachments.

This reader deliberately makes no model call. A conclusion needs a record with
both a trade signal and product description; every extracted value retains the
source location and original excerpt that produced it.

``.xls`` is accepted because customs-data exports still use the legacy format,
including files that are really HTML tables or tab separated text.  The shared
:mod:`excel_reader` keeps the original sheet name and row number so citations
stay auditable.
"""
import csv
import io
import os
import re

from excel_reader import cell_text, read_sheets

_PRODUCT = re.compile(
    r'\b(?:pm{1,2}a|acrylic|plexiglas|plastic\s+(?:sheet|board|panel)s?)\b'
    r'|亚克力|压克力|有机玻璃|聚甲基丙烯酸甲酯|甲基丙烯酸甲酯|丙烯酸板|塑胶板|塑料板|塑胶片|塑料片|塑料板材',
    re.I)
_TRADE = re.compile(
    r'\b(?:import|importer|consignee|shipment|bill\s+of\s+lading|export|exporter)\b'
    r'|进口|出口|报关|收货人|发货人|提单',
    re.I)
_HS = re.compile(
    r'\b(?:hs\s*(?:code)?\s*[:#-]?\s*)?(?:39\d{4,8}|\d{6,10}|39\d{2}\.\d{2}(?:\.\d{2})?)\b',
    re.I)
_DATE = re.compile(
    r'\b(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]20\d{2})\b'
    r'|20\d{2}年\d{1,2}月(?:\d{1,2}日)?')
_MONEY = re.compile(r'(?:[$¥￥€£]|USD|EUR|CNY|RMB)\s?[\d,]+(?:\.\d+)?', re.I)
_WEIGHT = re.compile(
    r'\b[\d,.]+\s*(?:kg|kgs|kilograms?|lb|lbs|tons?|mt)\b|[\d,.]+\s*(?:千克|公斤|吨|克)', re.I)
_QUANTITY = re.compile(
    r'\b[\d,.]+\s*(?:pcs?|pieces?|sheets?|units?|rolls?)\b|[\d,.]+\s*(?:件|个|张|片|卷|平方米|平方英尺)',
    re.I)
_FIELD_ALIASES = {
    'company_or_entity': ('company', 'entity', 'supplier', 'shipper', 'buyer', 'consignee', 'name',
                          '公司名称', '企业名称', '客户名称'),
    'importer': ('importer', 'consignee', 'buyer', '进口商', '收货人', '进口方', '进口企业', '买方'),
    'exporter': ('exporter', 'supplier', 'shipper', 'seller', '出口商', '发货人', '出口方', '出口企业', '卖方'),
    'product_description': ('product', 'description', 'commodity', 'goods', 'item',
                            '商品描述', '商品名称', '品名', '产品描述', '产品名称', '货物描述', '规格'),
    'hs_code': ('hs', 'tariff', '海关编码', '商品编码', '税则号', 'hs编码'),
    'country_or_region': ('country', 'origin', 'destination', 'region',
                          '国家', '地区', '原产国', '目的国', '起运国', '运抵国'),
    'date': ('date', 'shipment date', 'import date', 'export date',
             '日期', '进口日期', '出口日期', '报关日期', '提单日期'),
    'quantity': ('quantity', 'qty', 'pieces', 'units', '数量'),
    'weight': ('weight', 'net weight', 'gross weight', '重量', '净重', '毛重'),
    'amount': ('amount', 'value', 'price', 'usd', 'total', '金额', '价值', '总值', '价格', '美元'),
    'unit': ('unit', 'uom', '单位'),
}
# Customs exports can be large. Read enough rows to decide, but keep the
# stored evidence (and the JSON written to the database) bounded.
_MAX_RECORDS = 3000
_MAX_FIELD_FACTS = 200
_MAX_CITATIONS = 20
_HEADER_SCAN_ROWS = 15


def _csv_rows(raw):
    return list(csv.reader(io.StringIO(raw.decode('utf-8-sig', 'replace'))))


def _header_index(rows):
    """Find the row that looks most like a header (customs files often have title rows)."""
    best_index, best_score = 0, 0
    for index, row in enumerate(rows[:_HEADER_SCAN_ROWS]):
        headers = [cell_text(value).casefold() for value in row]
        score = 0
        for aliases in _FIELD_ALIASES.values():
            if any(alias in header for header in headers for alias in aliases):
                score += 1
        if score > best_score:
            best_index, best_score = index, score
    return best_index


def _tabular_records(rows, source, limit=_MAX_RECORDS):
    if not rows or limit <= 0:
        return []
    header_index = _header_index(rows)
    headers = [cell_text(value) for value in rows[header_index]]
    records = []
    for row_number, row in enumerate(rows[header_index + 1:], header_index + 2):
        if len(records) >= limit:
            break
        values = [cell_text(value) for value in row]
        text = ' | '.join(values).strip()
        if not text.replace('|', '').strip():
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


def _spreadsheet_records(raw, filename):
    records = []
    for sheet_name, rows in read_sheets(raw=raw, filename=filename):
        records.extend(_tabular_records(rows, sheet_name, limit=_MAX_RECORDS - len(records)))
        if len(records) >= _MAX_RECORDS:
            break
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


def _field_facts(records):
    facts = []
    for record in records:
        for key, value in record['fields'].items():
            if len(facts) >= _MAX_FIELD_FACTS:
                return facts
            facts.append({'field': key, 'value': value, 'citation': _citation(record)})
    return facts


def analyze_import_file(path, filename=''):
    """Analyze one local user file and return only evidence-backed conclusions."""
    ext = os.path.splitext(filename or path)[1].lower()
    try:
        with open(path, 'rb') as handle:
            raw = handle.read()
        if ext == '.csv':
            records = _tabular_records(_csv_rows(raw), 'CSV')
        elif ext in ('.xlsx', '.xls', '.xlsm'):
            records = _spreadsheet_records(raw, filename or path)
        elif ext == '.pdf':
            records, error = _pdf_records(raw)
            if records is None:
                return {'status': 'analysis_failed', 'error': error, 'facts': [], 'citations': [], 'field_facts': []}
        elif ext in ('.png', '.jpg', '.jpeg'):
            return {'status': 'insufficient', 'error': '', 'facts': [], 'citations': [], 'field_facts': [],
                    'missing': ['当前环境没有受控 OCR 读取能力；请上传 CSV、XLSX、XLS 或文本型 PDF，或人工填写截图中的记录。'],
                    'next_action': '保留问题，等待可审计的文字证据。'}
        else:
            return {'status': 'analysis_failed', 'error': '不支持的证据格式', 'facts': [], 'citations': [], 'field_facts': []}
    except Exception as exc:
        return {'status': 'analysis_failed', 'error': '文件解析失败：' + str(exc), 'facts': [], 'citations': [], 'field_facts': []}
    field_facts = _field_facts(records)
    # Column names are meaningful evidence too: a row below an ``Importer``
    # header is a trade record even when its cell value does not repeat that
    # word.  Product matching remains confined to the same row.
    trade_records = [record for record in records if _TRADE.search(record['text']) or
                     record['fields'].get('importer') or record['fields'].get('exporter')]
    supported = [record for record in trade_records if _PRODUCT.search(record['text']) or
                 _PRODUCT.search(record['fields'].get('product_description', ''))]
    if supported:
        return {'status': 'supported', 'facts': ['发现含进口/出口主体和 PMMA/亚克力产品描述的记录'],
                'citations': [_citation(record) for record in supported[:_MAX_CITATIONS]], 'field_facts': field_facts,
                'confidence': 'medium', 'missing': [],
                'matching_basis': '同一可读记录同时包含贸易角色与目标产品描述；未仅依据单个关键词判断。',
                'next_action': '解除调查阻塞；仅准备外联，不自动发送。'}
    if trade_records:
        return {'status': 'not_supported', 'facts': ['发现可读的贸易记录，但没有目标 PMMA/亚克力产品描述'],
                'citations': [_citation(record) for record in trade_records[:_MAX_CITATIONS]], 'field_facts': field_facts,
                'confidence': 'medium', 'missing': [],
                'matching_basis': '存在贸易记录，但相关产品字段或文本明确未支持当前调查判断。',
                'next_action': '记录调查结论并按规则降低开发优先级；不自动发送。'}
    return {'status': 'insufficient', 'facts': [], 'citations': [], 'field_facts': field_facts, 'confidence': 'low',
            'missing': ['过去 24 个月内含产品描述与进口主体的记录'],
            'matching_basis': '文件可读，但没有同时满足贸易主体和产品描述的可审计记录。',
            'next_action': '保留问题，等待补充证据。'}
