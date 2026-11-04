"""读取 Excel 表格行，供 Inbox 证据分析与 Excel 导入共用。

旧版 ``.xls`` 在外贸海关数据里仍很常见，而且不总是真正的 BIFF 工作簿：
报关/数据服务网站经常把 HTML 表格或制表符文本直接存成 ``.xls``，
Excel 的“另存为网页”也会产生 Excel 2003 XML。这里统一接受这几种来源，
并始终返回 ``(工作表名, 行列表)``，让调用方保留工作表与行号引用。

读取只依赖已有的 ``openpyxl`` 与 ``xlrd``，不新增依赖。
"""
import csv
import datetime
import io
import os
import xml.etree.ElementTree as ElementTree
from html.parser import HTMLParser

_CFB_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
_ZIP_MAGIC = b'PK\x03\x04'
_SS_NS = 'urn:schemas-microsoft-com:office:spreadsheet'
_XLSX_EXTENSIONS = ('.xlsx', '.xlsm', '.xltx', '.xltm')


def read_sheets(path=None, raw=None, filename=''):
    """Return ``[(sheet_name, rows), ...]`` for an .xlsx or legacy .xls file.

    ``raw`` may be passed instead of ``path`` (for uploads already in memory).
    Cell values keep their type where possible: dates become ``datetime``,
    whole numbers become ``int``, and formulas use their cached result.
    """
    if raw is None:
        if not path:
            raise ValueError('缺少要读取的文件')
        with open(path, 'rb') as handle:
            raw = handle.read()
    name = filename or path or ''
    ext = os.path.splitext(str(name))[1].lower()
    if ext in _XLSX_EXTENSIONS or raw[:4] == _ZIP_MAGIC:
        return _xlsx_sheets(raw)
    if ext == '.xls' or raw[:8] == _CFB_MAGIC:
        return _xls_sheets(raw)
    raise ValueError('不支持的表格格式')


def cell_text(value):
    """One cell as display text; dates and floats become stable, readable text."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, datetime.datetime):
        if value.hour or value.minute or value.second:
            return value.strftime('%Y-%m-%d %H:%M')
        return value.strftime('%Y-%m-%d')
    if isinstance(value, datetime.date):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return ('%.10f' % value).rstrip('0').rstrip('.')
    return str(value).strip()


def _xlsx_sheets(raw):
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        return [(sheet.title, [list(row) for row in sheet.iter_rows(values_only=True)])
                for sheet in workbook.worksheets]
    finally:
        workbook.close()


def _xls_sheets(raw):
    """Read BIFF first; fall back to HTML / SpreadsheetML / delimited text."""
    try:
        return _xlrd_sheets(raw)
    except Exception:
        pass
    text = _decode_text(raw)
    if text:
        stripped = text.lstrip('\ufeff \t\r\n')
        if stripped.startswith('<?xml') or '<Workbook' in stripped[:4000]:
            try:
                return _xml_sheets(stripped)
            except Exception:
                pass
        if '<table' in text[:50000].lower():
            tables = _html_tables(text)
            if tables:
                return [(f'表格 {index}', rows) for index, rows in enumerate(tables, 1)]
        rows = _delimited_rows(text)
        if rows:
            return [('XLS', rows)]
    raise ValueError('无法读取该 Excel 文件：不是有效的工作簿，也不是可识别的 HTML/CSV 表格')


def _xlrd_sheets(raw):
    import xlrd

    workbook = xlrd.open_workbook(file_contents=raw, on_demand=True)
    try:
        sheets = []
        for sheet in workbook.sheets():
            rows = []
            for row_index in range(sheet.nrows):
                rows.append([xls_cell_value(workbook, sheet.cell(row_index, column_index))
                             for column_index in range(sheet.ncols)])
            sheets.append((sheet.name or 'XLS', rows))
        return sheets
    finally:
        workbook.release_resources()


def xls_cell_value(workbook, cell):
    """One xlrd cell as a typed value (date cells become ``datetime``)."""
    import xlrd

    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK, xlrd.XL_CELL_ERROR):
        return None
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate.xldate_as_datetime(cell.value, workbook.datemode)
        except Exception:
            return cell.value
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        value = cell.value
        return int(value) if float(value).is_integer() else value
    return cell.value


def _decode_text(raw):
    """Decode plain-text disguised .xls; return None for binary data."""
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        encodings = ('utf-16', 'utf-8-sig')
    else:
        if b'\x00' in raw[:4096]:
            return None
        encodings = ('utf-8-sig', 'gb18030')
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if '\x00' not in text:
            return text
    return None


def _xml_sheets(text):
    """Read Excel 2003 SpreadsheetML (``.xls`` saved as XML)."""
    if len(text) > 20_000_000:
        raise ValueError('表格文本过大')
    root = ElementTree.fromstring(text)
    ns = {'ss': _SS_NS}
    sheets = []
    for index, worksheet in enumerate(root.findall('.//ss:Worksheet', ns), 1):
        name = worksheet.get(f'{{{_SS_NS}}}Name') or f'工作表 {index}'
        table = worksheet.find('ss:Table', ns)
        rows = []
        if table is not None:
            for row in table.findall('ss:Row', ns):
                values = []
                for cell in row.findall('ss:Cell', ns):
                    cell_index = cell.get(f'{{{_SS_NS}}}Index')
                    if cell_index and cell_index.isdigit():
                        while len(values) < int(cell_index) - 1:
                            values.append(None)
                    data = cell.find('ss:Data', ns)
                    value = ''.join(data.itertext()).strip() if data is not None else ''
                    values.append(value or None)
                if values:
                    rows.append(values)
        if rows:
            sheets.append((name, rows))
    return sheets


class _TableCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._rows = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == 'table' and self._rows is None:
            self._rows = []
        elif tag == 'tr' and self._rows is not None:
            self._row = []
        elif tag in ('td', 'th') and self._row is not None:
            self._cell = []
        elif tag == 'br' and self._cell is not None:
            self._cell.append(' ')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ('td', 'th') and self._row is not None and self._cell is not None:
            self._row.append(' '.join(''.join(self._cell).split()))
            self._cell = None
        elif tag == 'tr' and self._rows is not None and self._row is not None:
            if any(cell for cell in self._row):
                self._rows.append(self._row)
            self._row = None
        elif tag == 'table' and self._rows is not None:
            if self._rows:
                self.tables.append(self._rows)
            self._rows = None
            self._row = None
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _html_tables(text):
    parser = _TableCollector()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        return []
    return parser.tables


def _delimited_rows(text):
    """Parse tab/comma/semicolon separated text (often exported with .xls)."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    first = lines[0]
    delimiter = next((candidate for candidate in ('\t', ',', ';') if candidate in first), None)
    if delimiter is None:
        return []
    rows = [[cell.strip() for cell in row]
            for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    return [row for row in rows if any(row)]
