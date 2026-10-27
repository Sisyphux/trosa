"""Search-only synonyms and pinyin: one idea, every way a person writes it.

Customer records mix Chinese and English ("亚克力分销", "acrylic sheet
distributor").  Search uses this module to let a query that *is* a known term
find the same idea written in the other language or spelling, and to let
letters typed as pinyin find Chinese text.

Boundaries (same spirit as ``customer_country_search``):

* Search only.  Nothing here changes a stored value or any response field.
* A query expands only when the whole word *equals* a known term (a trailing
  plural "s"/"es" is ignored).  A partial input keeps plain substring matching.
* Latin terms match whole words in the text ("sheet" never hits "sheeting"
  by accident); Chinese terms match as plain substrings.
* Groups come from the words that actually appear in the Customer industry,
  field and type columns.  Add a group when a new recurring term shows up.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from pypinyin import Style, lazy_pinyin

SYNONYM_GROUPS = (
    ('亚克力', '压克力', '有机玻璃', 'acrylic', 'pmma', 'plexiglas', 'plexiglass', 'perspex'),
    ('分销', '分销商', '经销', '经销商', '批发', '批发商', 'distributor', 'wholesaler', 'wholesale',
     'dealer', 'reseller'),
    ('贸易', '贸易商', 'trader', 'trading'),
    ('加工', '加工商', '加工厂', 'fabricator', 'fabrication', 'processor'),
    ('制造', '制造商', '生产商', '厂家', '工厂', 'manufacturer', 'maker', 'factory', 'producer'),
    ('标牌', '标识', '招牌', '广告牌', 'signage', 'signboard'),
    ('广告', 'advertising', 'advertiser', 'advertisement'),
    ('印刷', '打印', 'printing', 'printer'),
    ('激光', 'laser'),
    ('数控', 'cnc'),
    ('进口', '进口商', 'importer', 'import'),
    ('出口', '出口商', 'exporter', 'export'),
    ('零售', '零售商', 'retailer', 'retail'),
    ('展示', '展柜', 'display'),
    ('展会', '展览', 'exhibition', 'expo', 'trade show'),
    ('塑料', 'plastic', 'plastics'),
    ('玻璃', 'glass'),
    ('板材', 'sheet'),
    ('供应商', '供货商', 'supplier', 'vendor'),
    ('终端', '终端客户', 'end user', 'end-user'),
    ('中间商', 'intermediary', 'middleman'),
    ('家具', 'furniture'),
    ('建材', '建筑建材', 'building material', 'construction'),
)

_CJK = re.compile(r'[㐀-鿿]')
_CJK_RUN = re.compile(r'[㐀-鿿]+')


def _fold(value):
    text = unicodedata.normalize('NFKD', str(value or '')).casefold().strip()
    return ''.join(ch for ch in text if not unicodedata.combining(ch))


def _build_lookup():
    lookup = {}
    for group in SYNONYM_GROUPS:
        folded = tuple(dict.fromkeys(_fold(term) for term in group))
        for term in folded:
            lookup.setdefault(term, folded)
    return lookup


_LOOKUP = _build_lookup()


def synonym_terms(token):
    """Every term meaning the same as ``token`` (itself included), or ()."""
    key = _fold(token)
    for candidate in (key, re.sub(r'(?:es|s)$', '', key)):
        if candidate and candidate in _LOOKUP:
            return _LOOKUP[candidate]
    return ()


def _term_in_text(term, folded_text):
    if _CJK.search(term):
        return term in folded_text
    return re.search(r'(?<![a-z0-9])' + re.escape(term) + r'(?:es|s)?(?![a-z0-9])', folded_text) is not None


def synonym_hit(token, folded_text):
    """True when some spelling of the idea ``token`` names is in ``folded_text``."""
    return any(_term_in_text(term, folded_text) for term in synonym_terms(token))


@lru_cache(maxsize=4096)
def _pinyin_forms(run):
    return ''.join(lazy_pinyin(run)), ''.join(lazy_pinyin(run, style=Style.FIRST_LETTER))


def pinyin_hit(token, text):
    """True when letters typed as pinyin read as part of the Chinese in ``text``.

    Three or more letters may sit anywhere in the full pinyin ("yakeli");
    two or more may be the initials from the start of a Chinese run ("zw"
    for 张伟).  Letters only: a token with anything else never matches.
    """
    key = _fold(token)
    if len(key) < 2 or not key.isascii() or not key.isalpha():
        return False
    for run in _CJK_RUN.findall(str(text or '')):
        full, initials = _pinyin_forms(run)
        if (len(key) >= 3 and key in full) or initials.startswith(key):
            return True
    return False
