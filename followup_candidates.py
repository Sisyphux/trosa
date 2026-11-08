"""Deterministic "who should I follow up with today" candidate engine.

Pure functions over in-memory rows: no database, no HTTP, no clock.  The caller
loads one member's accounts and timeline events and passes them in together with
``today``; the result is a ranked list of *candidates*.  A candidate is only a
suggestion built from timeline facts - it is not a task, it never persists by
itself, and it disappears the day the evidence stops being true.

Rules (see ``docs/research/2026-10-改造落地路线.md``, "规则定义"):

* ``R1`` customer-waiting      - the customer spoke last and nobody answered.
* ``R2`` quote-sample-silent   - a quote / sample / PI went out, then silence.
* ``R3`` dormant-deal-client   - a customer with deal traces went quiet.
* ``R4`` customer-said-date    - a date the customer named has passed.

Everything is decided from timeline events only.  ``contact_state``,
``next_task_date`` and ``last_contact_at`` are never read, and legacy
``outreach_*`` reminder rows are never an input (open tasks arrive solely as a
set of account ids).

Data-quality guards (``guards`` in the parameter table, each can be switched off):
auto-reply wording, bounce wording, bulk back-fill days, same-name duplicate customers,
archived customers that still have contact, and weeks-only wording in reasons.

All thresholds live in :data:`DEFAULT_PARAMS`; per-member overrides are merged
on top with :func:`merge_params`.
"""

from __future__ import annotations

import html
import json
import re
import statistics
import unicodedata
from calendar import monthrange
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

# Reuse the business regexes and the relationship classifier; never copy them.
from trosa_domain import (
    _RE_BOUNCE,
    _RE_CUST,
    _RE_NEG_QUOTE,
    _RE_NEG_SAMPLE,
    _RE_ONEWAY,
    _RE_QUOTE,
    _RE_QUOTE_STRONG,
    _RE_REJECT,
    _RE_SAMPLE_CUST,
    _RE_STRONG_ORDER,
    _relationship_bucket,
)


# ---------------------------------------------------------------------------
# Parameter table - the only place thresholds are written down.
# ---------------------------------------------------------------------------

DEFAULT_PARAMS: dict[str, Any] = {
    # R1: the customer spoke last and we have not answered.
    'r1': {
        'min_days': 2,      # at least this many calendar days since the customer spoke
        'max_days': 30,     # older than this is no longer "waiting", just gone quiet
    },
    # R2: our quote / sample / PI, then nothing.
    'r2': {
        'min_days': 14,     # 7 days would be wrong for most replies (only 29% reply within 7)
        'max_days': 56,     # ~8 weeks: older than this is stale, chasing it is re-prospecting, not a follow-up
    },
    # R3: customer with deal traces, quiet for a long time.
    'r3': {
        'min_days': 45,                # floor of the silence threshold
        'interval_multiplier': 2.5,    # ... or this many times the usual gap between contact days
        'max_days': 150,               # only customers who were in touch recently
        'min_contact_days': 4,         # distinct contact days needed to know the usual gap
        'weekly_cap': 1,               # at most this many R3 per member per ISO week
    },
    # R4: a date the customer named has passed.
    'r4': {
        'grace_days': 3,               # days after the named date with no follow-up record
        'max_days': 30,                # stop suggesting this long after the named date
        'max_future_days': 120,        # ignore stated dates further out than this
        'after_holiday_days': 14,      # "节后 / after the holiday" -> event date + this
        'standalone': True,            # False: R4 may only support another rule's candidate
    },
    # Limits on what a member sees per day.
    'limits': {
        'daily_cap': 2,                # few and sure: a member sees at most this many a day, most days none
        'launch_days': 3,              # the first days after the feature is switched on
        'launch_cap': 2,
        'r1_total_cap': 2,             # R1 may overflow the daily cap, never beyond this total
        'ignore_threshold': 2,         # "不用跟/忽略" this many times ...
        'ignore_window_days': 14,      # ... within this many days silences the customer
    },
    # Display / ordering.
    'priority': ['R1:certain', 'R4', 'R1:hint', 'R2', 'R3'],
    # Which timeline sources can be trusted to know the direction.
    'reliable_sources': ['sela', 'inbox'],
    # Event types that are not a conversation at all.
    'ignored_event_types': ['outreach', '外联', 'task_completed', 'task_done', '待办完成',
                            'weekly_summary', 'weekly', '周内汇总', 'bounce'],
    # Data-quality guards; each one can be switched off by overriding it with False.
    'guards': {
        'auto_reply': True,         # out-of-office / auto-reply wording is not the customer speaking
        'bounce_text': True,        # delivery-failure wording is not the customer speaking
        'batch_day': True,          # a day with a bulk back-fill is a weaker date: R2/R3 wait longer
        'duplicate_names': True,    # same normalised name with a newer record elsewhere: hold back
        'archived_contact': True,   # report archived customers that were contacted after archiving
        'weeks_display': True,      # reasons say "about N weeks", never a falsely exact day count
        'complete_reason': True,    # a suggestion without last topic / last speaker (/ deal detail) is not shown
    },
    # Bulk back-fill days: one creator entering this many records on one day.
    'batch_day': {
        'threshold': 25,
        'extra_days': 7,            # added to the R2 minimum / R3 silence threshold for such a date
    },
    # Same-name duplicate customers.
    'duplicates': {
        'min_name_length': 3,       # shorter normalised names are too generic to compare
    },
    # Wording taken from the timeline for the reason card.
    'context': {
        'snippet_chars': 70,
    },
    'timezone': 'Asia/Shanghai',
}

RULE_NAMES = {
    'R1': 'customer-waiting',
    'R2': 'quote-sample-silent',
    'R3': 'dormant-deal-client',
    'R4': 'customer-said-date',
}


def merge_params(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return DEFAULT_PARAMS with ``overrides`` merged on top (one level deep)."""
    merged = json.loads(json.dumps(DEFAULT_PARAMS))
    for key, value in (overrides or {}).items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One suggested follow-up.  Facts are what the reason sentence is built from."""

    account_id: Any
    rule: str                       # 'R1' .. 'R4'
    certainty: str                  # 'certain' | 'hint'
    evidence_event_id: Any          # the one event the suggestion hangs on
    evidence_event_ids: tuple       # all events that carry the facts
    facts: Mapping[str, Any]
    reason: str
    owner: str = ''
    priority: int = 0               # lower is more important
    supporting: tuple = ()          # other rules that also fired for this account
    dedupe_key: str = ''

    @property
    def rule_name(self) -> str:
        return RULE_NAMES[self.rule]

    def as_dict(self) -> dict:
        return {
            'account_id': self.account_id, 'rule': self.rule, 'rule_name': self.rule_name,
            'certainty': self.certainty, 'evidence_event_id': self.evidence_event_id,
            'evidence_event_ids': list(self.evidence_event_ids), 'facts': dict(self.facts),
            'reason': self.reason, 'owner': self.owner, 'priority': self.priority,
            'supporting': list(self.supporting), 'dedupe_key': self.dedupe_key,
        }


@dataclass
class ScanResult:
    """Everything one scan decided, for the daily job and the shadow list."""

    released: list = field(default_factory=list)      # what the member sees today
    held_back: list = field(default_factory=list)     # valid but cut by the daily cap
    excluded: dict = field(default_factory=dict)      # reason -> number of accounts
    fired: dict = field(default_factory=dict)         # rule -> accounts that matched it
    suppressed: list = field(default_factory=list)    # valid candidates held back as "possible duplicate customer"
    archived_with_contact: list = field(default_factory=list)  # archived accounts with records after archiving


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

_RE_PI = re.compile(r'(?<![A-Za-z])PI(?![A-Za-z])|proforma|形式发票', re.I)
_RE_NEG_ORDER = re.compile(r'(没有|没|无|暂无|未|尚未|还没|不)[^，。,.;；\s]{0,3}(订单|下单|合同|付款|定金|预付款)')
# The customer said no: not a conversation worth chasing.
_RE_DECLINED = re.compile(
    r"(婉拒|拒绝|谢绝|不感兴趣|没有兴趣|无兴趣|不考虑|不需要|没有需求|暂无需求|无需求|退订|取消订阅|别再发|不要再发"
    r"|not interested|no interest|no need|unsubscribe|remove me|stop (?:e-?mailing|sending|contacting)|do not contact"
    r"|(?:we|i) (?:do not|don't) (?:need|require|import|buy)|not (?:looking|in the market)|no longer (?:need|interested))", re.I)
_RE_LEAD_LABEL = re.compile(r'^\s*客户(?:回复|来信|回信)了?(?:邮件)?[：:]\s*')
_BOUNCE_NUMERIC = re.compile(r'^\d+$')
# Typical automatic-reply sentences only.  A bare "holiday", "vacation" or "ticket" is an ordinary
# word in a real customer mail ("our factory closes for the holiday"), so single words never match.
_RE_AUTO_REPLY = re.compile(
    r"(自动回复|自动回信|自动答复|休假中|休假期间|度假中|不在办公室|外出期间"
    r"|out[ -]of[ -](?:the[ -])?office|\bauto[- ]?(?:reply|replied|response|responder|generated)\b"
    r"|\bautomatic(?:ally)?[ -](?:reply|response|generated)\b|\bautomated (?:reply|response|message)\b"
    r"|this is an automated|do not reply to this (?:e-?mail|message)"
    r"|\bI(?:'m| am) currently (?:out|away)\b|\bI(?:'m| am) (?:currently )?on (?:annual )?(?:vacation|leave)\b"
    r"|\b(?:your|the) (?:support )?(?:ticket|request) (?:has been|was|is) (?:received|created|logged|opened)\b"
    r"|abwesenheitsnotiz|abwesenheitsmeldung|automatische antwort|fuera de (?:la )?oficina"
    r"|respuesta autom[aá]tica|r[ée]ponse automatique|absent du bureau|risposta automatica"
    r"|resposta autom[aá]tica|fora do escrit[óo]rio)", re.I)

_MONTHS = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6, 'jul': 7, 'aug': 8,
           'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}
_RE_DATE_ISO = re.compile(r'(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)')
_RE_DATE_CN = re.compile(r'(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]')
_RE_DATE_EN = re.compile(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b'
                         r'|\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b', re.I)
_RE_REL_TOMORROW = re.compile(r'明天|tomorrow', re.I)
_RE_REL_DAY_AFTER = re.compile(r'后天')
_RE_REL_NEXT_WEEK = re.compile(r'下周|下星期|下礼拜|next\s+week', re.I)
_RE_REL_NEXT_MONTH = re.compile(r'下个月|下月|next\s+month', re.I)
_RE_REL_MONTH_END = re.compile(r'月底|月末|end\s+of\s+(?:the\s+|this\s+)?month', re.I)
_RE_REL_AFTER_HOLIDAY = re.compile(r'节后|after\s+the\s+(?:holiday|festival|spring\s+festival|new\s+year)', re.I)


def _as_dict(row: Any) -> dict:
    return dict(row) if not isinstance(row, dict) else row


def _payload(event: Mapping) -> dict:
    raw = event.get('payload')
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _to_date(value: Any, tz: ZoneInfo) -> date | None:
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(tz)
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        if len(text) > 10:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(tz)
            return parsed.date()
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _text(event: Mapping, *fields: str) -> str:
    return ' '.join(str(event.get(f) or '') for f in fields if event.get(f)).strip()


def _weeks(days: int) -> int:
    """Whole weeks for display; never shows a falsely exact day count."""
    return max(1, round(days / 7))


def _span(days: int, params: Mapping) -> str:
    """How long ago, for a reason sentence: weeks when the guard is on, never a fake-exact day count."""
    if not params['guards']['weeks_display']:
        return f'{days} 天'
    return '不到一周' if days < 7 else f'约 {_weeks(days)} 周'


def normalise_name(name: Any) -> str:
    """Customer name reduced for comparison: width/case folded, punctuation and spaces removed."""
    folded = unicodedata.normalize('NFKC', str(name or '')).casefold()
    return re.sub(r'[\W_]+', '', folded)


def open_task_account_ids(reminders: Iterable[Any]) -> set:
    """Accounts with an unfinished task, ignoring the legacy ``outreach_%`` reminder rows.

    ``reminders``: rows with ``customer_id`` (or ``account_id``), ``reminder_type`` and
    ``is_done`` (or ``status``).  Convenience for callers; the engine itself only ever
    receives the resulting set of ids.
    """
    ids = set()
    for raw in reminders:
        row = _as_dict(raw)
        if str(row.get('reminder_type') or 'follow_up').lower().startswith('outreach'):
            continue
        if str(row.get('is_done') or '0').lower() in ('1', 'true') \
                or str(row.get('status') or 'open').lower() in ('done', 'completed', 'closed'):
            continue
        account_id = row.get('customer_id', row.get('account_id'))
        if account_id is not None:
            ids.add(account_id)
    return ids


def _bounce_text(text: str) -> bool:
    """A delivery failure notice.  Bare numeric hits (``550`` in a price) do not count."""
    return any(not _BOUNCE_NUMERIC.match(m.group(0)) for m in _RE_BOUNCE.finditer(text))


def _md(day: date) -> str:
    """Date as shown to people: 10月20日."""
    return f'{day.month}月{day.day}日'


def _source_label(event: Mapping, reliable: bool) -> str:
    source = str(event.get('source_module') or '').lower()
    if 'sela' in source:
        return 'Sela 交接'
    if 'inbox' in source:
        return 'Inbox 捕获'
    return '记录' if reliable else '手工记录'


def _is_reliable(event: Mapping, params: Mapping) -> bool:
    source = str(event.get('source_module') or '').lower()
    return any(tag in source for tag in params['reliable_sources'])


# ---------------------------------------------------------------------------
# Event normalisation
# ---------------------------------------------------------------------------

def _creator(event: Mapping) -> str:
    """Who entered the record, for counting bulk back-fill days (events of one member usually share it)."""
    return str(event.get('created_by') or event.get('user') or event.get('owner') or '')


def _normalise_events(events: Iterable[Any], params: Mapping, today: date) -> dict[Any, list[dict]]:
    """Group usable conversation events by account, oldest first.

    Dropped here: deleted rows, outreach / bounce / task / weekly-summary rows,
    auto-replies and delivery failures, rows without a date and rows from the future.
    Surviving rows get ``_batch`` when their day is a bulk back-fill day.
    """
    tz = ZoneInfo(params['timezone'])
    ignored_types = [t.lower() for t in params['ignored_event_types']]
    by_account: dict[Any, list[dict]] = {}
    for index, raw in enumerate(events):
        event = _as_dict(raw)
        account_id = event.get('account_id')
        if account_id is None:
            continue
        payload = _payload(event)
        if str(event.get('is_deleted') or '0').lower() in ('1', 'true') \
                or str(payload.get('is_deleted') or '0').lower() in ('1', 'true'):
            continue
        event_type = str(event.get('event_type') or event.get('activity_type') or '')
        if any(event_type.lower().startswith(t) or event_type.lower() == t for t in ignored_types):
            continue
        if any(str(v or '').lower().startswith('outreach') for v in (event.get('source_module'), payload.get('source'))):
            continue                                    # legacy outreach machinery, never a conversation
        day = _to_date(event.get('occurred_at'), tz)
        if day is None or day > today:
            continue
        text = _text(event, 'content', 'result', 'next_plan')
        direction = str(event.get('direction') or 'unknown').lower()
        customer_typed = direction == 'inbound' or event_type.lower() in ('customer_reply', '客户回复')
        if params['guards']['bounce_text'] and customer_typed and _bounce_text(text):
            continue
        if params['guards']['auto_reply'] and customer_typed and _RE_AUTO_REPLY.search(text):
            continue
        by_account.setdefault(account_id, []).append({
            **event,
            '_id': event.get('event_id', event.get('id', f'{account_id}:{day.isoformat()}:{index}')),
            '_day': day,
            '_type': event_type,
            '_direction': direction,
            '_payload': payload,
            '_order': index,
        })
    batch_cfg = params['batch_day']
    per_day: dict[tuple, int] = {}
    for rows in by_account.values():
        for e in rows:
            key = (_creator(e), e['_day'])
            per_day[key] = per_day.get(key, 0) + 1
    for rows in by_account.values():
        rows.sort(key=lambda e: (e['_day'], e['_order']))
        for e in rows:
            e['_batch'] = bool(params['guards']['batch_day']
                               and per_day[(_creator(e), e['_day'])] >= int(batch_cfg['threshold']))
    return by_account


def _side(event: Mapping) -> str:
    """Who spoke: 'customer', 'customer_text', 'ours', 'both' or 'unknown'."""
    direction = event['_direction']
    if direction == 'inbound' or event['_type'].lower() in ('customer_reply', '客户回复'):
        return 'customer'
    if direction == 'outbound':
        return 'ours'
    last_speaker = str(event['_payload'].get('last_speaker') or '').lower()
    if direction == 'two_way':
        if last_speaker in ('customer', 'client', 'inbound'):
            return 'customer_text'
        return 'both'
    if last_speaker in ('customer', 'client', 'inbound'):
        return 'customer_text'
    if _RE_CUST.search(_text(event, 'content', 'result')):
        return 'customer_text'
    return 'unknown'


# ---------------------------------------------------------------------------
# Deal detection (R2 / R3)
# ---------------------------------------------------------------------------

def _deal_kinds(event: Mapping) -> list[str]:
    """Which of quote / sample / pi this record is about."""
    kinds: list[str] = []

    def add(kind: str) -> None:
        if kind not in kinds:
            kinds.append(kind)

    for item in event['_payload'].get('deal_events') or []:
        value = str(item).lower()
        if value in ('quote', 'sample', 'pi'):
            add(value)
        elif value == 'proforma':
            add('pi')
    label = event['_type'].lower()
    if 'quote' in label or '报价' in label:
        add('quote')
    if 'sample' in label or '样' in label:
        add('sample')
    text = _text(event, 'content', 'result')
    if _RE_QUOTE.search(text) and not (_RE_NEG_QUOTE.search(text) and not _RE_QUOTE_STRONG.search(text)):
        add('quote')
    if _RE_SAMPLE_CUST.search(text) and not (
            _RE_NEG_SAMPLE.search(text)
            and not re.search(r'(样品(寄|到|收|测|反馈|已|确认)|已寄样|拿了.{0,6}样品|寄送.{0,4}样|寄出|送达)', text)):
        add('sample')
    if _RE_PI.search(text):
        add('pi')
    return kinds


def _deal_trace(event: Mapping) -> bool:
    """The record shows an actual deal (order, contract, payment ...), not a negated one."""
    text = _text(event, 'content', 'result')
    match = _RE_STRONG_ORDER.search(text)
    if not match or _RE_NEG_ORDER.search(text):
        return False
    if _RE_ONEWAY.search(text) and not _RE_CUST.search(text):
        return False
    return True


# ---------------------------------------------------------------------------
# R4: dates the customer named
# ---------------------------------------------------------------------------

def _clamp_future(candidate: date | None, base: date, max_future: int) -> date | None:
    if candidate is None or candidate <= base or (candidate - base).days > max_future:
        return None
    return candidate


def _explicit_dates(text: str, base: date) -> list[tuple[date, str]]:
    found: list[tuple[date, str]] = []

    def build(year: int, month: int, day: int) -> date | None:
        try:
            return date(year, month, day)
        except ValueError:
            return None

    for m in _RE_DATE_ISO.finditer(text):
        d = build(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            found.append((d, m.group(0)))
    month_days = [(int(m.group(1)), int(m.group(2)), m.group(0)) for m in _RE_DATE_CN.finditer(text)]
    for m in _RE_DATE_EN.finditer(text):
        if m.group(1):
            month_days.append((_MONTHS[m.group(1).lower()[:3]], int(m.group(2)), m.group(0)))
        else:
            month_days.append((_MONTHS[m.group(4).lower()[:3]], int(m.group(3)), m.group(0)))
    for month, day, snippet in month_days:
        d = build(base.year, month, day)
        if d and d < base:                      # "1月5日" said in December means next year
            d = build(base.year + 1, month, day)
        if d:
            found.append((d, snippet))
    return found


def _stated_due_dates(event: Mapping, params: Mapping) -> list[tuple[date, str]]:
    """Future dates the record names, with the wording they came from."""
    r4 = params['r4']
    base = event['_day']
    text = _text(event, 'content', 'result')    # next_plan is our own plan, not the customer's word
    found = [(d, s) for d, s in _explicit_dates(text, base)]
    for item in event['_payload'].get('stated_dates') or []:
        value = item.get('date') if isinstance(item, Mapping) else item
        d = _to_date(value, ZoneInfo(params['timezone']))
        if d:
            found.append((d, str(item.get('quote') if isinstance(item, Mapping) and item.get('quote') else value)))

    def wording(match: re.Match) -> str:
        start = max(0, match.start() - 10)
        return text[start:match.end() + 10].strip()

    for regex, resolve in (
        (_RE_REL_TOMORROW, lambda: base + timedelta(days=1)),
        (_RE_REL_DAY_AFTER, lambda: base + timedelta(days=2)),
        (_RE_REL_NEXT_WEEK, lambda: base + timedelta(days=7 - base.weekday() + 6)),   # Sunday of next week
        (_RE_REL_NEXT_MONTH, lambda: date(base.year + (base.month == 12), base.month % 12 + 1, 1).replace(
            day=monthrange(base.year + (base.month == 12), base.month % 12 + 1)[1])),
        (_RE_REL_MONTH_END, lambda: base.replace(day=monthrange(base.year, base.month)[1])),
        (_RE_REL_AFTER_HOLIDAY, lambda: base + timedelta(days=int(r4['after_holiday_days']))),
    ):
        match = regex.search(text)
        if match:
            found.append((resolve(), wording(match)))
    return [(d, s) for d, s in found if _clamp_future(d, base, int(r4['max_future_days']))]


# ---------------------------------------------------------------------------
# The four rules.  Each returns a Candidate (without priority/owner) or None.
# ---------------------------------------------------------------------------

def _last_day_events(rows: list[dict]) -> tuple[date, list[dict]]:
    last = rows[-1]['_day']
    return last, [e for e in rows if e['_day'] == last]


def _rule_r1(account_id: Any, rows: list[dict], today: date, params: Mapping) -> Candidate | None:
    cfg = params['r1']
    last, day_rows = _last_day_events(rows)
    age = (today - last).days
    if age < cfg['min_days'] or age > cfg['max_days']:
        return None
    sides = [_side(e) for e in day_rows]
    if any(s in ('ours', 'both') for s in sides):
        return None                                   # we answered the same day
    speakers = [e for e, s in zip(day_rows, sides) if s in ('customer', 'customer_text')]
    if not speakers:
        return None
    if any(_RE_DECLINED.search(_text(e, 'content', 'result')) for e in speakers):
        return None                                   # the customer said no: not worth chasing
    reliable_speakers = [e for e, s in zip(day_rows, sides) if s == 'customer' and _is_reliable(e, params)]
    certain = bool(reliable_speakers) and all(s == 'customer' for s in sides)
    evidence = (reliable_speakers or speakers)[-1]
    source = _source_label(evidence, bool(reliable_speakers))
    if certain:
        reason = f'客户 {_md(last)}来信，之后还没有回复'
    else:
        reason = f'客户最后一次来信在{_span(age, params)}前（{_md(last)}），之后没有回复记录'
    return Candidate(
        account_id=account_id, rule='R1', certainty='certain' if certain else 'hint',
        evidence_event_id=evidence['_id'], evidence_event_ids=tuple(e['_id'] for e in speakers),
        facts={'customer_spoke_on': last.isoformat(), 'days_ago': age, 'source': source,
               'reliable_source': bool(reliable_speakers)},
        reason=reason)


def _rule_r2(account_id: Any, rows: list[dict], today: date, params: Mapping) -> Candidate | None:
    cfg = params['r2']
    last, day_rows = _last_day_events(rows)
    age = (today - last).days
    batch = any(e['_batch'] for e in day_rows)
    if age < cfg['min_days'] + (int(params['batch_day']['extra_days']) if batch else 0) or age > cfg['max_days']:
        return None
    if any(_side(e) in ('customer', 'customer_text') for e in day_rows):
        return None                                   # the customer already answered
    if any(_RE_REJECT.search(_text(e, 'content', 'result')) for e in day_rows):
        return None
    deal_rows = [(e, _deal_kinds(e)) for e in day_rows if _side(e) != 'customer']
    deal_rows = [(e, kinds) for e, kinds in deal_rows if kinds]
    if not deal_rows:
        return None
    evidence, kinds = deal_rows[-1]
    kind = kinds[0]
    label = {'quote': '报价', 'sample': '寄样', 'pi': 'PI'}[kind]
    return Candidate(
        account_id=account_id, rule='R2', certainty='certain',
        evidence_event_id=evidence['_id'], evidence_event_ids=tuple(e['_id'] for e, _ in deal_rows),
        facts={'deal_kind': kind, 'deal_label': label, 'sent_on': last.isoformat(),
               'days_silent': age, 'weeks_silent': _weeks(age), 'batch_dated': batch},
        reason=f'{_md(last)}发了{label}，已约 {_weeks(age)} 周没有后续记录')


def _rule_r3(account_id: Any, rows: list[dict], today: date, params: Mapping) -> tuple[Candidate, int] | None:
    cfg = params['r3']
    contact_days = sorted({e['_day'] for e in rows})
    if len(contact_days) < cfg['min_contact_days']:
        return None
    deal_rows = [e for e in rows if _deal_trace(e)]
    if not deal_rows:
        return None
    gaps = [(b - a).days for a, b in zip(contact_days, contact_days[1:])]
    usual = statistics.median(gaps) if gaps else 0
    batch = any(e['_batch'] for e in rows if e['_day'] == contact_days[-1])
    threshold = max(cfg['min_days'], cfg['interval_multiplier'] * usual) \
        + (int(params['batch_day']['extra_days']) if batch else 0)
    silent = (today - contact_days[-1]).days
    if silent < threshold or silent > cfg['max_days']:
        return None
    evidence = deal_rows[-1]
    usual_days = int(round(usual))
    return Candidate(
        account_id=account_id, rule='R3', certainty='hint',
        evidence_event_id=evidence['_id'], evidence_event_ids=(evidence['_id'],),
        facts={'last_deal_on': evidence['_day'].isoformat(), 'usual_gap_days': usual_days,
               'usual_gap_weeks': _weeks(usual_days) if usual_days else 0,
               'days_silent': silent, 'weeks_silent': _weeks(silent),
               'contact_days': len(contact_days), 'batch_dated': batch},
        reason=(f'{_md(evidence["_day"])}前后有成交相关记录，平时约每 {_weeks(usual_days) if usual_days else 1} 周联系一次，'
                f'现在已约 {_weeks(silent)} 周没联系')), evidence['_day'].toordinal()


def _rule_r4(account_id: Any, rows: list[dict], today: date, params: Mapping) -> Candidate | None:
    cfg = params['r4']
    last, day_rows = _last_day_events(rows)            # a later record means someone followed up
    best: tuple[date, str, Mapping] | None = None
    for event in day_rows:
        if _side(event) == 'ours':
            continue
        for due, snippet in _stated_due_dates(event, params):
            if best is None or due > best[0]:
                best = (due, snippet, event)
    if best is None:
        return None
    due, snippet, event = best
    overdue = (today - due).days
    if overdue < cfg['grace_days'] or overdue > cfg['max_days']:
        return None
    return Candidate(
        account_id=account_id, rule='R4', certainty='hint',
        evidence_event_id=event['_id'], evidence_event_ids=(event['_id'],),
        facts={'said_on': last.isoformat(), 'quote': snippet, 'stated_date': due.isoformat(),
               'days_since_date': overdue},
        reason=f'客户 {_md(last)}提到「{snippet}」（约 {_md(due)}），之后没有记录')


# ---------------------------------------------------------------------------
# Reason context: what the member needs to open the conversation.
# Everything is quoted from existing timeline fields; nothing is guessed.
# ---------------------------------------------------------------------------

_RE_PRICE = re.compile(
    r'(?:US\$|USD|RMB|CNY|IDR|EUR|\$|¥|￥|€)\s?\d[\d,]*(?:\.\d+)?(?:\s*/\s*[A-Za-z\u4e00-\u9fff²㎡]{1,6})?'
    r'|\d[\d,]*(?:\.\d+)?\s*(?:美金|美元|USD|RMB|元|刀)(?:\s*/\s*[A-Za-z\u4e00-\u9fff²㎡]{1,6})?', re.I)


def _snippet(text: Any, limit: int) -> str:
    """One short quoted line: whitespace collapsed, cut at a sentence end when possible."""
    flat = re.sub(r'\s+', ' ', _RE_LEAD_LABEL.sub('', html.unescape(str(text or '')))).strip()
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    for mark in ('。', '；', '. ', '! ', '? ', '！', '？'):
        pos = cut.rfind(mark)
        if pos >= limit // 2:
            return cut[:pos + 1].strip()
    return cut.rstrip() + '…'


def _speaker_label(side: str) -> str:
    return {'customer': '客户', 'customer_text': '客户', 'ours': '我们', 'both': '双方'}.get(side, '')


def _reason_context(rows: list[dict], deal_event: Mapping | None, params: Mapping) -> dict[str, str]:
    """``last_topic`` / ``last_speaker`` / ``last_words`` / ``deal_detail`` for a reason card (empty when unknown)."""
    limit = int(params['context']['snippet_chars'])
    last_day = rows[-1]['_day']
    day_rows = [e for e in rows if e['_day'] == last_day]
    ctx: dict[str, str] = {}
    spoken = [e for e in day_rows if _text(e, 'content') or _text(e, 'result')]
    if spoken:
        final = spoken[-1]
        words = _snippet(_text(final, 'content') or _text(final, 'result'), limit)
        speaker = _speaker_label(_side(final))
        ctx['last_topic'] = words
        if speaker:
            ctx['last_speaker'] = speaker
            ctx['last_words'] = words
    if deal_event is not None:
        text = _text(deal_event, 'content', 'result')
        prices = []
        for m in _RE_PRICE.finditer(text):
            if m.group(0).strip() not in prices:
                prices.append(m.group(0).strip())
        if prices:
            ctx['deal_detail'] = '、'.join(prices[:3])
        if text:
            ctx['deal_text'] = _snippet(text, limit)
    return ctx


def _with_context(candidate: Candidate, rows: list[dict], deal_event: Mapping | None, params: Mapping) -> Candidate:
    ctx = _reason_context(rows, deal_event, params)
    return replace(candidate, facts={**candidate.facts, 'context': ctx})


def reason_is_complete(candidate: Candidate) -> bool:
    """Show a suggestion only when the member can see where the talk stood and who spoke last."""
    ctx = candidate.facts.get('context') or {}
    if not ctx.get('last_topic') or not ctx.get('last_speaker'):
        return False
    if candidate.rule in ('R2', 'R3') and not (ctx.get('deal_text') or ctx.get('deal_detail')):
        return False
    return True


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def _priority_rank(candidate: Candidate, params: Mapping) -> int:
    order = list(params['priority'])
    for key in (f'{candidate.rule}:{candidate.certainty}', candidate.rule):
        if key in order:
            return order.index(key)
    return len(order)


def _sort_key(candidate: Candidate) -> tuple:
    # Same tier: the longest wait first, then a stable id order.
    waited = (candidate.facts.get('days_ago') or candidate.facts.get('days_silent')
              or candidate.facts.get('days_since_date') or 0)
    return (candidate.priority, -waited, str(candidate.account_id))


def compute_candidates(accounts: Iterable[Any], events: Iterable[Any], open_task_account_ids: Iterable[Any],
                       today: date, params: Mapping | None = None, owner: str = '', **options: Any) -> list[Candidate]:
    """Today's follow-up candidates for one member, best first.

    ``accounts``: dicts with ``account_id`` and optionally ``deleted_at`` (archived),
    ``notes`` / ``system_notes`` / ``import_source`` (used to tell Sela-only
    prospects from human customers).
    ``events``: timeline rows with ``account_id``, ``event_type``, ``direction``,
    ``content`` / ``result`` / ``next_plan``, ``source_module``, ``occurred_at``,
    ``payload`` and optionally ``event_id`` / ``is_deleted``.
    ``open_task_account_ids``: accounts that already have an open (non-``outreach_%``)
    task; they are on the Today page already and get no suggestion.
    ``today``: the member's local date.  ``params``: overrides for
    :data:`DEFAULT_PARAMS`.

    Keyword options: ``muted`` (set of ``(account_id, event_id)`` or
    ``(account_id, rule, event_id)`` the member said "不用跟" to), ``ignored``
    (``{account_id: [dates]}`` of recent ignores), ``launch_date`` (day the
    feature was switched on; the first days use the smaller cap), ``r3_released_this_week``
    (R3 already shown this ISO week), ``mails`` / ``inbox`` (``{account_id: [rows]}``
    used only to decide who is Sela-only), ``human_owned_ids`` (override).

    Optional account field ``name`` enables the same-name duplicate guard (skipped for accounts
    without one).  ``peer_last_contact`` maps a customer name to the day of its newest record
    held by *other* members, so duplicates split across members are noticed too.
    """
    return scan(accounts, events, open_task_account_ids, today, params, owner, **options).released


def scan(accounts: Iterable[Any], events: Iterable[Any], open_task_account_ids: Iterable[Any],
         today: date, params: Mapping | None = None, owner: str = '', *,
         muted: Iterable[tuple] = (), ignored: Mapping[Any, Iterable[Any]] | None = None,
         launch_date: date | None = None, r3_released_this_week: int = 0,
         mails: Mapping[Any, list] | None = None, inbox: Mapping[Any, list] | None = None,
         human_owned_ids: Iterable[Any] | None = None,
         peer_last_contact: Mapping[str, Any] | None = None) -> ScanResult:
    """Same as :func:`compute_candidates` but also reports what was held back and why."""
    cfg = merge_params(params) if params is not None else merge_params()
    tz = ZoneInfo(cfg['timezone'])
    today = _to_date(today, tz) or today
    result = ScanResult()
    excluded = result.excluded

    def exclude(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    open_tasks = set(open_task_account_ids)
    muted_keys = {tuple(k) for k in muted}
    human_override = set(human_owned_ids) if human_owned_ids is not None else None
    ignore_window = timedelta(days=int(cfg['limits']['ignore_window_days']))
    by_account = _normalise_events(events, cfg, today)

    account_rows = [_as_dict(raw) for raw in accounts]
    names: dict[str, list] = {}                       # normalised name -> account ids
    if cfg['guards']['duplicate_names']:
        for account in account_rows:
            norm = normalise_name(account.get('name'))
            if len(norm) >= int(cfg['duplicates']['min_name_length']):
                names.setdefault(norm, []).append(account.get('account_id', account.get('id')))
    peer_days = {normalise_name(k): _to_date(v, tz) for k, v in (peer_last_contact or {}).items()}

    def newer_duplicate(account: Mapping, account_id: Any, last_day: date) -> list:
        """Other accounts with the same name that have a newer record than ``last_day``."""
        norm = normalise_name(account.get('name'))
        if norm not in names:
            return []
        newer = [other for other in names[norm]
                 if other != account_id and by_account.get(other) and by_account[other][-1]['_day'] > last_day]
        peer = peer_days.get(norm)
        if peer is not None and peer > last_day:
            newer.append('other-member')
        return newer

    pool: list[Candidate] = []
    r3_pool: list[tuple[Candidate, int]] = []
    for account in account_rows:
        account_id = account.get('account_id', account.get('id'))
        rows = by_account.get(account_id)
        if account.get('deleted_at'):
            exclude('archived')
            if cfg['guards']['archived_contact'] and rows:
                archived_on = _to_date(account.get('deleted_at'), tz)
                if archived_on is None or rows[-1]['_day'] > archived_on:
                    result.archived_with_contact.append({
                        'account_id': account_id, 'last_record_on': rows[-1]['_day'].isoformat(),
                        'archived_on': archived_on.isoformat() if archived_on else None})
            continue
        if not rows:
            exclude('no_real_events')
            continue
        if account_id in open_tasks:
            exclude('has_open_task')
            continue
        if human_override is not None:
            human = account_id in human_override
        else:
            bucket = _relationship_bucket(
                [{**e, 'activity_type': e['_type']} for e in rows],
                list((mails or {}).get(account_id, [])), list((inbox or {}).get(account_id, [])),
                str(account.get('notes') or ''), str(account.get('system_notes') or ''),
                str(account.get('import_source') or ''))
            human = bucket != 'auto_follow'
        if not human:
            exclude('sela_only')
            continue
        recent_ignores = [d for d in ((_to_date(x, tz) for x in (ignored or {}).get(account_id, ())))
                          if d and today - ignore_window <= d <= today]
        if len(recent_ignores) >= int(cfg['limits']['ignore_threshold']):
            exclude('ignored_twice')
            continue

        fired: list[Candidate] = []
        for rule_fn in (_rule_r1, _rule_r2, _rule_r4):
            candidate = rule_fn(account_id, rows, today, cfg)
            if candidate is not None:
                fired.append(candidate)
        r3 = _rule_r3(account_id, rows, today, cfg)
        if r3 is not None:
            fired.append(r3[0])
        fired = [c for c in fired
                 if (account_id, c.evidence_event_id) not in muted_keys
                 and (account_id, c.rule, c.evidence_event_id) not in muted_keys]
        if not cfg['r4']['standalone']:
            main = [c for c in fired if c.rule != 'R4']
            side = [c for c in fired if c.rule == 'R4']
            fired = main + (side if main else [])      # R4 only rides along with another rule
            r4_support_only = True
        else:
            r4_support_only = False
        if not fired:
            exclude('no_rule_matched')
            continue
        for c in fired:
            result.fired[c.rule] = result.fired.get(c.rule, 0) + 1
        ranked = sorted(((_priority_rank(c, cfg), c) for c in fired), key=lambda t: t[0])
        if r4_support_only:
            ranked = [t for t in ranked if t[1].rule != 'R4'] + [t for t in ranked if t[1].rule == 'R4']
        best = ranked[0][1]
        extras = tuple(c.rule for _, c in ranked[1:])
        final = Candidate(
            account_id=best.account_id, rule=best.rule, certainty=best.certainty,
            evidence_event_id=best.evidence_event_id, evidence_event_ids=best.evidence_event_ids,
            facts=best.facts, reason=best.reason, owner=owner, priority=ranked[0][0], supporting=extras,
            dedupe_key=f'{owner}:{account_id}:{best.rule}:{best.evidence_event_id}')
        deal_event = next((e for e in rows if e['_id'] == final.evidence_event_id), None) \
            if final.rule in ('R2', 'R3') else None
        final = _with_context(final, rows, deal_event, cfg)
        twins = newer_duplicate(account, account_id, rows[-1]['_day']) if names else []
        if twins:
            exclude('possible_duplicate')
            result.suppressed.append(replace(
                final, facts={**final.facts, 'suppressed': 'possible_duplicate', 'duplicate_account_ids': list(twins)},
                reason=final.reason + '；同名客户在别处有更新的记录'))
            continue
        if cfg['guards']['complete_reason'] and not reason_is_complete(final):
            exclude('incomplete_reason')
            continue
        if final.rule == 'R3':
            r3_pool.append((final, r3[1] if r3 else 0))
        else:
            pool.append(final)

    # R3 is rationed per ISO week: newest deal first, stable by account id.
    r3_room = max(0, int(cfg['r3']['weekly_cap']) - int(r3_released_this_week))
    r3_pool.sort(key=lambda t: (-t[1], str(t[0].account_id)))
    pool.extend(c for c, _ in r3_pool[:r3_room])
    result.held_back.extend(c for c, _ in r3_pool[r3_room:])

    pool.sort(key=_sort_key)
    limits = cfg['limits']
    launching = launch_date is not None and 0 <= (today - launch_date).days < int(limits['launch_days'])
    cap = int(limits['launch_cap'] if launching else limits['daily_cap'])
    released = pool[:cap]
    rest = pool[cap:]
    if not launching:
        # "Customer wrote, nobody answered" may overflow the daily cap, never past the total cap.
        room = int(limits['r1_total_cap']) - len(released)
        overflow = [c for c in rest if c.rule == 'R1'][:max(0, room)]
        released.extend(overflow)
        rest = [c for c in rest if c not in overflow]
        released.sort(key=_sort_key)
    result.released = released
    result.held_back.extend(rest)
    return result
