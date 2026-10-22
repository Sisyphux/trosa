"""Search-only country aliases: one country, every spelling a person types.

A customer stores its country however it was entered ("Sweden", "瑞典",
"USA" ...).  Customer search uses this table to widen a query that *is* a
country name into all known spellings of that country, so "瑞典" finds a
customer stored as "Sweden" and the other way round.

Boundaries:

* Search only.  Nothing here changes a stored value, the save-time
  ``normalize_country`` mapping or any response field.
* A query expands only when the whole query, or one whitespace-separated
  word of it, *equals* a known spelling (case-insensitive, surrounding
  whitespace and a leading "the" ignored).  A partial input such as "瑞"
  never expands and keeps plain substring matching.
* The groups mirror the display table ``COUNTRY_DISPLAY_NAMES`` in
  ``app/static/app.js`` (138 countries/regions, 178 English spellings), plus
  the Chinese name of each group.  Keep the two tables in sync until they are
  served from one source.
"""

from __future__ import annotations

import re
import unicodedata

# Chinese display name -> English spellings (lower case, without "the").
COUNTRY_SEARCH_GROUPS = {
    '美国': ('us', 'usa', 'u.s.', 'u.s.a.', 'u.s.a', 'united states', 'united states of america', 'america'),
    '阿联酋': ('uae', 'u.a.e.', 'u.a.e', 'united arab emirates', 'emirates', 'dubai'),
    '沙特阿拉伯': ('saudi arabia', 'ksa', 'saudi'),
    '英国': ('uk', 'u.k.', 'united kingdom', 'great britain', 'britain', 'england'),
    '德国': ('germany', 'deutschland'),
    '法国': ('france',),
    '意大利': ('italy',),
    '西班牙': ('spain', 'españa'),
    '葡萄牙': ('portugal',),
    '荷兰': ('netherlands', 'holland'),
    '比利时': ('belgium',),
    '卢森堡': ('luxembourg',),
    '瑞士': ('switzerland',),
    '奥地利': ('austria',),
    '爱尔兰': ('ireland',),
    '瑞典': ('sweden',),
    '挪威': ('norway',),
    '丹麦': ('denmark',),
    '芬兰': ('finland',),
    '冰岛': ('iceland',),
    '波兰': ('poland',),
    '捷克': ('czech republic', 'czechia'),
    '斯洛伐克': ('slovakia',),
    '匈牙利': ('hungary',),
    '罗马尼亚': ('romania',),
    '保加利亚': ('bulgaria',),
    '希腊': ('greece',),
    '克罗地亚': ('croatia',),
    '斯洛文尼亚': ('slovenia',),
    '塞尔维亚': ('serbia',),
    '立陶宛': ('lithuania',),
    '拉脱维亚': ('latvia',),
    '爱沙尼亚': ('estonia',),
    '乌克兰': ('ukraine',),
    '俄罗斯': ('russia', 'russian federation'),
    '白俄罗斯': ('belarus',),
    '塞浦路斯': ('cyprus',),
    '马耳他': ('malta',),
    '土耳其': ('turkey', 'türkiye', 'turkiye'),
    '加拿大': ('canada',),
    '墨西哥': ('mexico', 'méxico'),
    '巴西': ('brazil', 'brasil'),
    '阿根廷': ('argentina',),
    '智利': ('chile',),
    '秘鲁': ('peru', 'perú'),
    '哥伦比亚': ('colombia',),
    '厄瓜多尔': ('ecuador',),
    '委内瑞拉': ('venezuela',),
    '乌拉圭': ('uruguay',),
    '巴拉圭': ('paraguay',),
    '玻利维亚': ('bolivia',),
    '巴拿马': ('panama', 'panamá'),
    '哥斯达黎加': ('costa rica',),
    '危地马拉': ('guatemala',),
    '洪都拉斯': ('honduras',),
    '萨尔瓦多': ('el salvador',),
    '尼加拉瓜': ('nicaragua',),
    '伯利兹': ('belize',),
    '多米尼加共和国': ('dominican republic', 'dominicana'),
    '波多黎各': ('puerto rico',),
    '牙买加': ('jamaica',),
    '特立尼达和多巴哥': ('trinidad and tobago', 'trinidad'),
    '巴巴多斯': ('barbados',),
    '巴哈马': ('bahamas',),
    '阿鲁巴': ('aruba',),
    '格林纳达': ('grenada',),
    '圭亚那': ('guyana',),
    '苏里南': ('suriname',),
    '圣卢西亚': ('saint lucia',),
    '安提瓜和巴布达': ('antigua and barbuda',),
    '古巴': ('cuba',),
    '海地': ('haiti',),
    '澳大利亚': ('australia',),
    '新西兰': ('new zealand', 'aotearoa'),
    '斐济': ('fiji',),
    '巴布亚新几内亚': ('papua new guinea',),
    '中国': ('china',),
    '中国香港': ('hong kong',),
    '中国澳门': ('macau', 'macao'),
    '中国台湾': ('taiwan',),
    '日本': ('japan',),
    '韩国': ('south korea', 'korea', 'republic of korea'),
    '蒙古': ('mongolia',),
    '印度': ('india',),
    '巴基斯坦': ('pakistan',),
    '孟加拉国': ('bangladesh',),
    '斯里兰卡': ('sri lanka',),
    '尼泊尔': ('nepal',),
    '新加坡': ('singapore',),
    '马来西亚': ('malaysia',),
    '印度尼西亚': ('indonesia',),
    '泰国': ('thailand',),
    '越南': ('vietnam', 'viet nam'),
    '菲律宾': ('philippines',),
    '缅甸': ('myanmar',),
    '柬埔寨': ('cambodia',),
    '老挝': ('laos',),
    '文莱': ('brunei',),
    '哈萨克斯坦': ('kazakhstan',),
    '乌兹别克斯坦': ('uzbekistan',),
    '吉尔吉斯斯坦': ('kyrgyzstan',),
    '阿塞拜疆': ('azerbaijan',),
    '格鲁吉亚': ('georgia',),
    '亚美尼亚': ('armenia',),
    '阿富汗': ('afghanistan',),
    '卡塔尔': ('qatar',),
    '科威特': ('kuwait',),
    '阿曼': ('oman',),
    '巴林': ('bahrain',),
    '伊朗': ('iran',),
    '伊拉克': ('iraq',),
    '以色列': ('israel',),
    '约旦': ('jordan',),
    '黎巴嫩': ('lebanon',),
    '叙利亚': ('syria',),
    '也门': ('yemen',),
    '埃及': ('egypt',),
    '摩洛哥': ('morocco',),
    '阿尔及利亚': ('algeria',),
    '突尼斯': ('tunisia',),
    '利比亚': ('libya',),
    '苏丹': ('sudan',),
    '尼日利亚': ('nigeria',),
    '加纳': ('ghana',),
    '肯尼亚': ('kenya',),
    '坦桑尼亚': ('tanzania',),
    '乌干达': ('uganda',),
    '埃塞俄比亚': ('ethiopia',),
    '南非': ('south africa',),
    '博茨瓦纳': ('botswana',),
    '纳米比亚': ('namibia',),
    '赞比亚': ('zambia',),
    '津巴布韦': ('zimbabwe',),
    '莫桑比克': ('mozambique',),
    '安哥拉': ('angola',),
    '塞内加尔': ('senegal',),
    '喀麦隆': ('cameroon',),
    '科特迪瓦': ("cote d'ivoire", 'côte d’ivoire', "côte d'ivoire", 'ivory coast',),
}


def country_search_key(value):
    """Comparison key for a country spelling; the stored value is untouched."""
    text = unicodedata.normalize('NFKC', str(value or '')).casefold()
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'^the ', '', text)
    return re.sub(r'[\s,;。]+$', '', text)


def _build_lookup():
    lookup = {}
    for chinese, spellings in COUNTRY_SEARCH_GROUPS.items():
        keys = {country_search_key(chinese)}
        keys.update(country_search_key(spelling) for spelling in spellings)
        keys.discard('')
        group = frozenset(keys)
        for key in group:
            lookup[key] = group
    return lookup


_LOOKUP = _build_lookup()


def country_alias_keys(term):
    """All spelling keys of the country ``term`` names exactly, else empty."""
    key = country_search_key(term)
    return _LOOKUP.get(key) or _LOOKUP.get(key.rstrip('.')) or frozenset()


def country_alias_sql_values(keys):
    """Lower-cased values to compare with ``lower(trim(country))`` in SQL.

    SQL cannot strip a leading "the", so each Latin spelling is listed with
    and without it.  Sorted for stable bind parameters.
    """
    values = set(keys)
    values.update('the ' + key for key in keys if key.isascii())
    return sorted(values)


def country_aliases_for_tokens(query, tokens):
    """Map each search token to the country spelling keys it may also match.

    A token expands when it equals a country spelling by itself.  When the
    whole query names a country ("United States"), every token of it may be
    satisfied by that country.  Tokens that name no country are absent.
    """
    whole = country_alias_keys(query) if len(tokens) > 1 else frozenset()
    result = {}
    for token in tokens:
        keys = country_alias_keys(token) | whole
        if keys:
            result[token] = keys
    return result


def country_matches(value, keys):
    """True when the stored country ``value`` is one of ``keys``."""
    return bool(keys) and country_search_key(value) in keys
