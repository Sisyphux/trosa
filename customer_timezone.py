"""Deterministic country -> IANA timezone inference for customer profiles.

The inferred value only drives a display question -- "is the counterpart
currently in local daytime?" -- and is never a business fact.  It never
creates tasks, contacts, or commitments, and it never changes any customer
matching logic.

Rules (v1):

* A single lookup table maps a country to one default IANA timezone.
* Multi-timezone countries (United States, Australia, Canada, Brazil,
  Russia, Mexico, Indonesia, ...) take a single documented default zone.
* The lookup is an exact match on the trimmed value (plus a small alias
  table for English names and abbreviations).  Anything the table does not
  recognise stays empty: no guessing from partial text, cities, or regions.
* ``infer_timezone`` never raises; unknown input returns ``''``.
"""

from __future__ import annotations

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
}

# Accepted English names and abbreviations -> canonical country above.
# Keys are matched case-insensitively; a Chinese key is unchanged by lower().
_ALIASES = {
    'us': '美国', 'usa': '美国', 'u.s.a.': '美国', 'u.s.a': '美国',
    'united states': '美国', 'united states of america': '美国', 'america': '美国',
    'uae': '阿联酋', 'u.a.e.': '阿联酋', 'u.a.e': '阿联酋', 'united arab emirates': '阿联酋',
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
}

_KNOWN_TIMEZONES = set(_available_timezones()) if _available_timezones else set()


def infer_timezone(country):
    """Return the default IANA timezone for ``country``, or ``''``.

    Exact (trimmed) match only.  Unknown or ambiguous values -- empty,
    ``全球``, a city (``迪拜``), a region note (``美国\\n加州``), a question
    mark (``马来西亚？``), or several countries at once (``美国/加拿大``) --
    return ``''`` rather than a guess.
    """
    if not country:
        return ''
    key = str(country).strip()
    if not key:
        return ''
    canonical = _ALIASES.get(key.lower(), key)
    return _COUNTRY_TIMEZONE.get(canonical, '')


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
    import re

    return bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)+', value))


def normalize_timezone(value):
    """Return ``value`` when it is a legal IANA zone name, otherwise ``''``."""
    if not value:
        return ''
    name = str(value).strip()
    return name if is_known_timezone(name) else ''
