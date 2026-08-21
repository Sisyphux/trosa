# -*- coding: utf-8 -*-
"""Generate stable, user-scoped RFC 5545 calendar feeds."""

from datetime import datetime, timedelta, timezone
import hashlib
import re


# Kept for compatibility with older callers. Feed freshness now comes from
# persisted reminder timestamps instead of a process-local counter.
_calendar_seq = 0
_SHANGHAI_TZ = timezone(timedelta(hours=8))


def bump_calendar_seq():
    global _calendar_seq
    _calendar_seq += 1
    return _calendar_seq


def get_calendar_seq():
    return _calendar_seq


def _parse_local_timestamp(value):
    text = str(value or '').strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=_SHANGHAI_TZ)
        except ValueError:
            continue
    return datetime(2000, 1, 1, tzinfo=_SHANGHAI_TZ)


def _utc_stamp(value):
    return _parse_local_timestamp(value).astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def _make_uid(owner_id, source, reminder_id):
    """A task keeps the same identity when its title or due date changes."""
    raw = f'trade-os:{owner_id}:{source}:{reminder_id}'
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]
    return f'{digest}@trade-os.local'


def _fold_line(line, max_octets=75):
    """Fold content lines by UTF-8 octets as required by RFC 5545."""
    chunks = []
    current = ''
    prefix = ''
    for char in str(line):
        candidate = prefix + current + char
        if current and len(candidate.encode('utf-8')) > max_octets:
            chunks.append(prefix + current)
            prefix = ' '
            current = char
        else:
            current += char
    chunks.append(prefix + current)
    return '\r\n'.join(chunks)


def _escape_text(value):
    text = str(value or '').replace('\r\n', '\n').replace('\r', '\n')
    return (text.replace('\\', '\\\\')
                .replace(';', '\\;')
                .replace(',', '\\,')
                .replace('\n', '\\n'))


def _clean_action(customer, value):
    action = re.sub(r'\s+', ' ', str(value or '').strip())
    if not action:
        return ''
    for prefix in (f'联系 {customer}', f'跟进 {customer}:', f'跟进 {customer}：'):
        if customer and action.casefold().startswith(prefix.casefold()):
            action = action[len(prefix):].strip(' ：:-')
    return action


def _summary(reminder):
    customer = str(reminder.get('customer_name') or '客户').strip()
    action = _clean_action(customer, reminder.get('title') or reminder.get('content'))
    if not action or action.casefold() == customer.casefold():
        return customer
    return f'{customer} · {action}'


def build_icalendar(reminders, owner_id='', calendar_name='客户跟进',
                    timezone_name='Asia/Shanghai', last_modified=''):
    """Convert task dictionaries to a stable personal iCalendar feed."""
    fallback_modified = last_modified or '2000-01-01 00:00:00'
    calendar_stamp = _utc_stamp(fallback_modified)
    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//Trade OS//Customer Follow-up Calendar//ZH',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        _fold_line(f'X-WR-CALNAME:{_escape_text(calendar_name)}'),
        f'X-WR-TIMEZONE:{timezone_name}',
        'REFRESH-INTERVAL;VALUE=DURATION:PT15M',
        'X-PUBLISHED-TTL:PT15M',
        f'LAST-MODIFIED:{calendar_stamp}',
    ]

    for reminder in reminders:
        date_text = str(reminder.get('remind_date') or '')[:10]
        try:
            start_date = datetime.strptime(date_text, '%Y-%m-%d').date()
        except ValueError:
            continue
        end_date = start_date + timedelta(days=1)
        changed_at = reminder.get('changed_at') or reminder.get('completed_at') or reminder.get('created_at') or fallback_modified
        changed_stamp = _utc_stamp(changed_at)
        status = str(reminder.get('status') or 'CONFIRMED').upper()
        sequence = 1 if status == 'CANCELLED' else 0
        description_parts = []
        action = str(reminder.get('title') or reminder.get('content') or '').strip()
        reason = str(reminder.get('reason') or '').strip()
        if action:
            description_parts.append(f'任务：{action}')
        if reason and reason != action:
            description_parts.append(f'原因：{reason}')
        description = '\n'.join(description_parts) or '客户跟进提醒'

        lines.extend([
            'BEGIN:VEVENT',
            f'UID:{_make_uid(owner_id, reminder.get("source", "reminder"), reminder.get("id", "0"))}',
            f'DTSTAMP:{changed_stamp}',
            f'LAST-MODIFIED:{changed_stamp}',
            f'SEQUENCE:{sequence}',
            f'DTSTART;VALUE=DATE:{start_date.strftime("%Y%m%d")}',
            f'DTEND;VALUE=DATE:{end_date.strftime("%Y%m%d")}',
            _fold_line(f'SUMMARY:{_escape_text(_summary(reminder))}'),
            _fold_line(f'DESCRIPTION:{_escape_text(description)}'),
            f'STATUS:{status}',
            'TRANSP:TRANSPARENT',
            'END:VEVENT',
        ])

    lines.append('END:VCALENDAR')
    return '\r\n'.join(lines) + '\r\n'
