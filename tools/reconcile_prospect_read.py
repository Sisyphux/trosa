#!/usr/bin/env python3
"""Read-only reconciliation for the S9 prospect read surface.

Run this with the Sela service token against a Trosa instance to reconcile the
read surface after the S9 read-side work (T1/T2/T3/T4/T9).  It prints one
explicit PASS/FAIL line per invariant plus the census numbers behind each
check, so nobody has to eyeball the data.

It only issues ``GET /api/integrations/sela/prospects`` requests (single reads,
``ids`` batches, the full feed and the ``needs`` filters).  It never writes,
never deletes and never sends mail.

    export TROSA_BASE_URL=https://app.trosa.space
    export TROSA_SERVICE_TOKEN=<sela service token>
    python3 tools/reconcile_prospect_read.py

Targeted checks (repeatable) assert the reconciliation the S9 work was about:

    --source-id 586 --expect-first-sent 2026-08-13
    --source-id <imported-draft> --expect-zero-sends

Exit status is 0 when every check passes and 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

PROSPECTS_PATH = '/api/integrations/sela/prospects'
CALLER_TZ = timezone(timedelta(hours=8))
FOLLOW_UP_DAYS = (7, 14, 30, 60, 90)
FOLLOW_UP_MIN_GAP_DAYS = 7


class HttpTransport:
    """Minimal read-only HTTP transport for the Sela service token."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.timeout = timeout

    def get(self, path: str, params: dict, headers: dict | None = None) -> tuple[int, dict, dict]:
        query = urllib.parse.urlencode(sorted((k, v) for k, v in params.items() if v not in (None, '')))
        url = self.base_url + path + ('?' + query if query else '')
        request_headers = {
            'Authorization': f'Bearer {self.token}',
            'Accept': 'application/json',
            'User-Agent': 'sela-reconcile/1.0',
        }
        if headers:
            request_headers.update({str(k): str(v) for k, v in headers.items()})
        request = urllib.request.Request(url, method='GET', headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode('utf-8')
                return response.status, dict(response.headers), json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode('utf-8')
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {'raw': raw}
            return exc.code, dict(exc.headers or {}), body


def _parse_day(value: str):
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def follow_up_due(first_sent_at: str, last_sent_at: str, today: date) -> bool:
    """Mirror of the server-side follow-up cadence used by needs=follow_up."""
    first, last = _parse_day(first_sent_at), _parse_day(last_sent_at)
    if first is None or last is None or last < first:
        return False
    days_since_first = (today - first).days
    milestone = 0
    for day in FOLLOW_UP_DAYS:
        if day <= days_since_first:
            milestone = day
    while milestone and milestone + 90 <= days_since_first:
        milestone += 90
    return bool(milestone) and last < first + timedelta(days=milestone) and (today - last).days >= FOLLOW_UP_MIN_GAP_DAYS


def needs_match(row: dict, need: str, today: date) -> bool:
    """Mirror of the server-side needs predicate, computed from one full row."""
    if str(row.get('lifecycle_stage') or '') != 'cold_prospect' or row.get('do_not_contact'):
        return False
    email = str(row.get('email') or '')
    verified = str(row.get('email_route_status') or '').upper() == 'VERIFIED'
    outreach = row.get('outreach') if isinstance(row.get('outreach'), dict) else {}
    sent = int(outreach.get('sent_count') or 0) > 0
    status = str(row.get('outreach_status') or '')
    if need == 'email':
        return not email
    if need == 'verify':
        return bool(email) and not verified
    if need == 'first_touch':
        return bool(email) and verified and not sent and status not in ('DRAFT_READY', 'GMAIL_DRAFTED')
    if need == 'follow_up':
        return bool(email) and verified and sent and not row.get('bounced') and follow_up_due(
            outreach.get('first_sent_at') or '', outreach.get('last_sent_at') or '', today)
    return False


def fetch_all(transport: HttpTransport, params: dict) -> tuple[list[dict], int | None]:
    """Every page of a prospects query, following next_after."""
    rows: list[dict] = []
    after = 0
    while True:
        query = dict(params)
        query['after'] = after
        query.setdefault('limit', 100)
        status, _headers, body = transport.get(PROSPECTS_PATH, query)
        if status != 200:
            raise RuntimeError(f'GET prospects failed: HTTP {status} {body}')
        page = body.get('prospects') or []
        rows.extend(page)
        next_after = body.get('next_after')
        if next_after in (None, ''):
            return rows, body.get('total')
        next_after = int(next_after)
        if next_after <= after:
            raise RuntimeError('paging cursor did not advance')
        after = next_after


def census(rows: list[dict], today: date) -> dict:
    stages: dict[str, int] = {}
    for row in rows:
        stage = str(row.get('lifecycle_stage') or '')
        stages[stage] = stages.get(stage, 0) + 1
    names: dict[str, int] = {}
    for row in rows:
        key = str(row.get('normalized_name') or '')
        if key:
            names[key] = names.get(key, 0) + 1
    return {
        'total': len(rows),
        'by_stage': stages,
        'do_not_contact': sum(1 for row in rows if row.get('do_not_contact')),
        'by_needs': {need: sum(1 for row in rows if needs_match(row, need, today))
                     for need in ('email', 'verify', 'first_touch', 'follow_up')},
        'duplicate_name_groups': sum(1 for count in names.values() if count > 1),
        'duplicate_name_rows': sum(count for count in names.values() if count > 1),
    }


def targeted_check(transport: HttpTransport, check: dict) -> tuple[bool, str]:
    source_id = str(check['source_id'])
    status, _headers, body = transport.get(f'{PROSPECTS_PATH}/{urllib.parse.quote(source_id, safe="")}', {})
    if status != 200:
        return False, f'{source_id}: read failed (HTTP {status})'
    row = body.get('prospect') or {}
    outreach = row.get('outreach') if isinstance(row.get('outreach'), dict) else {}
    expected = []
    ok = True
    if check.get('expect_first_sent'):
        actual = str(outreach.get('first_sent_at') or '')
        expected.append(f"first_sent_at={check['expect_first_sent']}")
        ok = ok and actual == check['expect_first_sent']
    if check.get('expect_count') is not None:
        actual = int(outreach.get('sent_count') or 0)
        expected.append(f"sent_count={check['expect_count']}")
        ok = ok and actual == int(check['expect_count'])
    if check.get('expect_zero_sends'):
        actual = int(outreach.get('sent_count') or 0)
        expected.append('sent_count=0')
        ok = ok and actual == 0
    detail = ', '.join(expected) or 'no expectation'
    return ok, (f'{source_id}: outreach={json.dumps(outreach, ensure_ascii=False)} '
                f'expected[{detail}] -> {"PASS" if ok else "FAIL"}')


def reconcile(transport: HttpTransport, targeted: list[dict] | None = None) -> tuple[bool, list[str]]:
    """Return (all_ok, lines).  Only reads; nothing here mutates Trosa."""
    today = datetime.now(CALLER_TZ).date()
    lines: list[str] = []
    ok = True

    def report(passed: bool, text: str) -> None:
        nonlocal ok
        ok = ok and passed
        lines.append(f'[{"PASS" if passed else "FAIL"}] {text}')

    rows, total = fetch_all(transport, {})
    ids = [str(row.get('id') or '') for row in rows]
    report(total is not None and int(total) == len(rows),
           f'feed total matches rows read ({total} vs {len(rows)})')
    report(len(set(ids)) == len(ids), f'no duplicate prospect ids ({len(ids)} rows)')

    today_census = census(rows, today)
    lines.append('      ' + json.dumps(today_census, ensure_ascii=False, sort_keys=True))
    lines.append(f'      previously-invisible customers now in the pool: {today_census["do_not_contact"]} do_not_contact, '
                 f'{today_census["by_stage"].get("cold_prospect", 0)} cold_prospect, '
                 f'{len(rows) - today_census["by_stage"].get("cold_prospect", 0)} handed off')

    for need in ('email', 'verify', 'first_touch', 'follow_up'):
        server_rows, _server_total = fetch_all(transport, {'needs': need})
        server_ids = {str(row.get('id') or '') for row in server_rows}
        local_ids = {str(row.get('id') or '') for row in rows if needs_match(row, need, today)}
        report(server_ids == local_ids,
               f'needs={need}: server returns {len(server_ids)} rows == local reconciliation {len(local_ids)}')

    _status, headers, body_a = transport.get(PROSPECTS_PATH, {'limit': 1})
    version = str(body_a.get('data_version') or '')
    report(bool(version), f'list read carries data_version ({version[:12] or "missing"})')
    etag = headers.get('ETag') or (f'"{version}"' if version else '')
    report(bool(etag), f'list read carries an ETag ({etag[:16] or "missing"})')
    if etag:
        status_cond, _h, body_b = transport.get(PROSPECTS_PATH, {'limit': 1}, {'If-None-Match': etag})
        report(status_cond == 304 and not body_b, 'If-None-Match returns 304 with no body')
        _status, _h, body_c = transport.get(PROSPECTS_PATH, {'limit': 1})
        report(str(body_c.get('data_version') or '') == version, 'data_version is stable across consecutive reads')
    for check in targeted or []:
        passed, text = targeted_check(transport, check)
        report(passed, text)
    return ok, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Read-only reconciliation for the S9 prospect read surface.')
    parser.add_argument('--base-url', default=os.environ.get('TROSA_BASE_URL', ''))
    parser.add_argument('--token', default=os.environ.get('TROSA_SERVICE_TOKEN', ''))
    parser.add_argument('--source-id', action='append', default=[], help='prospect source_id to check (repeatable)')
    parser.add_argument('--expect-first-sent', default='', help='expected first_sent_at for the last --source-id')
    parser.add_argument('--expect-count', type=int, default=None, help='expected sent_count for the last --source-id')
    parser.add_argument('--expect-zero-sends', action='store_true', help='expect sent_count==0 for the last --source-id')
    parser.add_argument('--json', action='store_true', help='print the lines as JSON')
    args = parser.parse_args(argv)

    if not args.base_url or not args.token:
        print('TROSA_BASE_URL and TROSA_SERVICE_TOKEN (or --base-url/--token) are required', file=sys.stderr)
        return 2

    targeted: list[dict] = []
    if args.source_id:
        last = {'source_id': args.source_id[-1]}
        if args.expect_first_sent:
            last['expect_first_sent'] = args.expect_first_sent
        if args.expect_count is not None:
            last['expect_count'] = args.expect_count
        if args.expect_zero_sends:
            last['expect_zero_sends'] = True
        targeted = [last]

    transport = HttpTransport(args.base_url, args.token)
    ok, lines = reconcile(transport, targeted)
    if args.json:
        print(json.dumps({'ok': ok, 'lines': lines}, ensure_ascii=False, indent=2))
    else:
        for line in lines:
            print(line)
        print('RESULT:', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
