"""Deterministic country -> IANA timezone inference for customer profiles.

The inferred value only drives a display question -- "is the counterpart
currently in local daytime?" -- and is never a business fact.  It never
creates tasks, contacts, or commitments, and it never changes any customer
matching logic.

Rules (v2, lenient):

* A lookup table maps a country to one default IANA timezone.
* Multi-timezone countries (United States, Australia, Canada, Brazil,
  Russia, Mexico, Indonesia, ...) take a single documented default zone --
  the most likely place, not an exact address.
* Real-world country values are messy.  The lookup first tries an exact
  (trimmed) match, then falls back to the most likely match inside the text:
  parenthetical notes are dropped, multi-value separators (``/``, newline,
  ``、`` ...) are split, trailing punctuation such as ``？`` is stripped, and
  the longest known country name the text starts with wins.  A city alias
  such as ``迪拜``/``Dubai`` resolves to its country's zone.
* Only a blank value or the explicit "worldwide" marker ``全球`` stays empty.
  ``infer_timezone`` never raises; anything it cannot place returns ``''``.
"""

from __future__ import annotations

import re

try:  # Python 3.9+
    from zoneinfo import available_timezones as _available_timezones
except Exception:  # pragma: no cover - platform without tzdata
    _available_timezones = None


TIMEZONE_SOURCE_INFERRED = 'inferred'
TIMEZONE_SOURCE_MANUAL = 'manual'

# Canonical country (mostly the Chinese name already stored on customers) ->
# default IANA timezone.  Multi-timezone countries use a single default.
_COUNTRY_TIMEZONE = {
    '美国': 'America/New_York',
    '阿联酋': 'Asia/Dubai',
    '澳大利亚': 'Australia/Sydney',
    '印度': 'Asia/Kolkata',
    '加拿大': 'America/Toronto',
    '墨西哥': 'America/Mexico_City',
    '新西兰': 'Pacific/Auckland',
    '沙特阿拉伯': 'Asia/Riyadh',
    '巴西': 'America/Sao_Paulo',
    '哥伦比亚': 'America/Bogota',
    '英国': 'Europe/London',
    '德国': 'Europe/Berlin',
    '马来西亚': 'Asia/Kuala_Lumpur',
    '意大利': 'Europe/Rome',
    '卡塔尔': 'Asia/Qatar',
    '法国': 'Europe/Paris',
    '智利': 'America/Santiago',
    '埃及': 'Africa/Cairo',
    '秘鲁': 'America/Lima',
    '土耳其': 'Europe/Istanbul',
    '西班牙': 'Europe/Madrid',
    '巴林': 'Asia/Bahrain',
    '阿曼': 'Asia/Muscat',
    '荷兰': 'Europe/Amsterdam',
    '波多黎各': 'America/Puerto_Rico',
    '新加坡': 'Asia/Singapore',
    '波兰': 'Europe/Warsaw',
    '比利时': 'Europe/Brussels',
    '科威特': 'Asia/Kuwait',
    '印度尼西亚': 'Asia/Jakarta',
    '泰国': 'Asia/Bangkok',
    '伊朗': 'Asia/Tehran',
    '南非': 'Africa/Johannesburg',
    '罗马尼亚': 'Europe/Bucharest',
    '巴拿马': 'America/Panama',
    '菲律宾': 'Asia/Manila',
    '葡萄牙': 'Europe/Lisbon',
    '厄瓜多尔': 'America/Guayaquil',
    '哥斯达黎加': 'America/Costa_Rica',
    '爱尔兰': 'Europe/Dublin',
    '芬兰': 'Europe/Helsinki',
    '多米尼加共和国': 'America/Santo_Domingo',
    '以色列': 'Asia/Jerusalem',
    '丹麦': 'Europe/Copenhagen',
    '匈牙利': 'Europe/Budapest',
    '危地马拉': 'America/Guatemala',
    '委内瑞拉': 'America/Caracas',
    '尼日利亚': 'Africa/Lagos',
    '希腊': 'Europe/Athens',
    '捷克': 'Europe/Prague',
    '洪都拉斯': 'America/Tegucigalpa',
    '瑞典': 'Europe/Stockholm',
    '瑞士': 'Europe/Zurich',
    '立陶宛': 'Europe/Vilnius',
    '越南': 'Asia/Ho_Chi_Minh',
    '阿根廷': 'America/Argentina/Buenos_Aires',
    '苏里南': 'America/Paramaribo',
    '俄罗斯': 'Europe/Moscow',
    # v2 additions: countries that previously stayed empty.
    '特立尼达和多巴哥': 'America/Port_of_Spain',
    '牙买加': 'America/Jamaica',
    '乌拉圭': 'America/Montevideo',
    '肯尼亚': 'Africa/Nairobi',
    '巴拉圭': 'America/Asuncion',
    '玻利维亚': 'America/La_Paz',
    '尼加拉瓜': 'America/Managua',
    '奥地利': 'Europe/Vienna',
    '巴巴多斯': 'America/Barbados',
    '伯利兹': 'America/Belize',
    '萨尔瓦多': 'America/El_Salvador',
    '加纳': 'Africa/Accra',
    '格林纳达': 'America/Grenada',
    '圭亚那': 'America/Guyana',
    '巴哈马': 'America/Nassau',
    '阿鲁巴': 'America/Aruba',
    '安提瓜和巴布达': 'America/Antigua',
    '孟加拉国': 'Asia/Dhaka',
    '博茨瓦纳': 'Africa/Gaborone',
    '中国': 'Asia/Shanghai',
    '挪威': 'Europe/Oslo',
    '圣卢西亚': 'America/St_Lucia',
    '斯里兰卡': 'Asia/Colombo',
    '坦桑尼亚': 'Africa/Dar_es_Salaam',
    '乌干达': 'Africa/Kampala',
}

# Accepted English names, abbreviations, and common city names -> canonical
# country above.  Keys are matched case-insensitively; a Chinese key is
# unchanged by lower().
_ALIASES = {
    'us': '美国', 'usa': '美国', 'u.s.a.': '美国', 'u.s.a': '美国',
    'united states': '美国', 'united states of america': '美国', 'america': '美国',
    'uae': '阿联酋', 'u.a.e.': '阿联酋', 'u.a.e': '阿联酋', 'united arab emirates': '阿联酋',
    'dubai': '阿联酋', '迪拜': '阿联酋',
    'saudi arabia': '沙特阿拉伯', 'ksa': '沙特阿拉伯', '沙特': '沙特阿拉伯',
    'brazil': '巴西', 'ecuador': '厄瓜多尔',
    'uk': '英国', 'united kingdom': '英国', 'great britain': '英国',
    'germany': '德国', 'deutschland': '德国',
    'france': '法国', 'australia': '澳大利亚', 'canada': '加拿大', 'mexico': '墨西哥',
    'india': '印度', 'italy': '意大利', 'egypt': '埃及', 'turkey': '土耳其',
    'denmark': '丹麦', 'new zealand': '新西兰', 'colombia': '哥伦比亚', 'iran': '伊朗',
    'oman': '阿曼', 'kuwait': '科威特', 'qatar': '卡塔尔', 'netherlands': '荷兰',
    'spain': '西班牙', 'portugal': '葡萄牙', 'poland': '波兰', 'belgium': '比利时',
    'romania': '罗马尼亚', 'israel': '以色列', 'singapore': '新加坡', 'malaysia': '马来西亚',
    'indonesia': '印度尼西亚', 'thailand': '泰国', 'vietnam': '越南', 'philippines': '菲律宾',
    'chile': '智利', 'peru': '秘鲁', 'panama': '巴拿马', 'suriname': '苏里南',
    'costa rica': '哥斯达黎加', 'guatemala': '危地马拉', 'honduras': '洪都拉斯',
    'venezuela': '委内瑞拉', 'argentina': '阿根廷', 'dominican republic': '多米尼加共和国',
    'dominicana': '多米尼加共和国', 'puerto rico': '波多黎各', 'nigeria': '尼日利亚',
    'south africa': '南非', 'greece': '希腊', 'czech republic': '捷克', 'czechia': '捷克',
    'sweden': '瑞典', 'switzerland': '瑞士', 'finland': '芬兰', 'ireland': '爱尔兰',
    'lithuania': '立陶宛', 'hungary': '匈牙利', 'bahrain': '巴林', 'russia': '俄罗斯',
    '多米尼加': '多米尼加共和国',
    # v2 additions.
    'trinidad and tobago': '特立尼达和多巴哥', 'trinidad': '特立尼达和多巴哥',
    'jamaica': '牙买加', 'uruguay': '乌拉圭', 'kenya': '肯尼亚',
    'paraguay': '巴拉圭', 'bolivia': '玻利维亚', 'nicaragua': '尼加拉瓜',
    'austria': '奥地利', 'barbados': '巴巴多斯', 'belize': '伯利兹',
    'el salvador': '萨尔瓦多', 'ghana': '加纳', 'grenada': '格林纳达',
    'guyana': '圭亚那', 'bahamas': '巴哈马', 'aruba': '阿鲁巴',
    'antigua and barbuda': '安提瓜和巴布达', 'bangladesh': '孟加拉国',
    'botswana': '博茨瓦纳', 'china': '中国', 'norway': '挪威',
    'saint lucia': '圣卢西亚', 'sri lanka': '斯里兰卡', 'tanzania': '坦桑尼亚',
    'uganda': '乌干达',
}

_KNOWN_TIMEZONES = set(_available_timezones()) if _available_timezones else set()

# A country value may carry a parenthetical share/note, several candidates
# separated by slashes or newlines, or trailing punctuation.  These helpers
# strip that noise before the most-likely lookup.
_PARENTHETICALS = re.compile(r'[（(【\[][^）)】\]]*[）)】\]]')
_SEPARATORS = re.compile(r'[\n\r/／\\、,，;；|&+·]+')
_TRAILING = '？?！!。.·、　 \t\r\n'


def _lookup_exact(key):
    """Exact (case-insensitive, alias-aware) lookup, or ``''``."""
    if not key:
        return ''
    canonical = _ALIASES.get(key.lower(), key)
    return _COUNTRY_TIMEZONE.get(canonical, '')


def _lookup_segment(segment):
    """Best-effort lookup for one cleaned segment.

    Exact match first, then the longest known country name (canonical or
    alias) the segment starts with.  Short ASCII aliases (``us``, ``uk``)
    are skipped in the prefix pass so they cannot swallow longer words.
    """
    text = segment.strip().strip(_TRAILING).strip()
    if not text:
        return ''
    exact = _lookup_exact(text)
    if exact:
        return exact

    lower = text.lower()
    best_len = 0
    best_tz = ''
    for key, tz in _COUNTRY_TIMEZONE.items():
        if len(key) < 2:
            continue
        if lower.startswith(key.lower()) and len(key) > best_len:
            best_len = len(key)
            best_tz = tz
    for alias, canonical in _ALIASES.items():
        if len(alias) < 4 and alias.isascii():
            continue
        if lower.startswith(alias) and len(alias) > best_len:
            best_len = len(alias)
            best_tz = _COUNTRY_TIMEZONE.get(canonical, '')
    return best_tz


def infer_timezone(country):
    """Return the most likely IANA timezone for ``country``, or ``''``.

    Lenient: parentheticals and separators are ignored and the longest known
    country the text starts with wins.  Only a blank value or the explicit
    worldwide marker ``全球`` returns ``''``.
    """
    if not country:
        return ''
    key = str(country).strip()
    if not key:
        return ''

    cleaned = _PARENTHETICALS.sub(' ', key)
    for segment in _SEPARATORS.split(cleaned):
        inferred = _lookup_segment(segment)
        if inferred:
            return inferred
    return ''


def is_known_timezone(name):
    """True when ``name`` is a legal IANA zone name (or ``UTC``)."""
    if not name:
        return False
    value = str(name).strip()
    if not value:
        return False
    if value == 'UTC':
        return True
    if _KNOWN_TIMEZONES:
        return value in _KNOWN_TIMEZONES
    # Last-resort shape check when tzdata is unavailable.
    return bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)+', value))


def normalize_timezone(value):
    """Return ``value`` when it is a legal IANA zone name, otherwise ``''``."""
    if not value:
        return ''
    name = str(value).strip()
    return name if is_known_timezone(name) else ''
