#!/usr/bin/env python3
"""Repair already-stored communications that still carry quoted mail history.

The capture pipeline used to persist the whole raw Gmail/Proton body (quoted
replies, forwarded headers, mobile signatures) as timeline content.  The
ingestion and read paths now strip that deterministically, but records written
before the fix keep the old body.

This tool pages the Agent read surface (``/api/agent/messages/search``), flags
timeline communications that still look like quoted mail, and -- only with
``--apply`` -- rewrites each one through the reversible ``update_communication``
gateway action.  Every write returns an undo token, and the raw original stays in
``communication_source_items`` for audit.

It is read-only by default: run it once to review the candidate list, then run
again with ``--apply``.

Usage:
    python3 tools/clean_communication_content.py                 # dry run
    python3 tools/clean_communication_content.py --apply         # repair
    python3 tools/clean_communication_content.py --json          # machine output
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, 'tools')
for path in (ROOT, TOOLS):
    if path not in sys.path:
        sys.path.insert(0, path)

from communication_text import looks_like_quoted_email, strip_quoted_email_text  # noqa: E402
from trosa_cli import CliError, Gateway, load_token  # noqa: E402


def _fetch_page(gateway, params, attempts=4):
    """Fetch one search page, retrying transient TLS/tunnel resets."""
    last = None
    for attempt in range(attempts):
        try:
            return gateway.agent_get('/messages/search', **params)
        except CliError as error:
            last = error
            if attempt < attempts - 1:
                time.sleep(1 + attempt)
    raise last


def iter_events(gateway, *, query='', country='', direction='', from_date='',
                to_date='', customer_id=None, page_size=100, max_pages=0):
    """Yield search items page by page until the result set is exhausted."""
    offset, pages = 0, 0
    while True:
        params = {
            'query': query, 'country': country, 'direction': direction,
            'from_date': from_date, 'to_date': to_date,
            'limit': page_size, 'offset': offset,
        }
        if customer_id:
            params['customer_id'] = customer_id
        page = _fetch_page(gateway, params)
        if not isinstance(page, dict):
            break
        batch = page.get('items') or []
        for item in batch:
            yield item
        pages += 1
        total = page.get('total')
        offset += len(batch)
        if len(batch) < page_size:
            break
        if total is not None and offset >= int(total):
            break
        if max_pages and pages >= max_pages:
            break


def find_candidates(items, min_length=200):
    """Return interactions whose stored content still contains quoted mail."""
    candidates = []
    for item in items:
        if not isinstance(item, dict):
            continue
        event_type = str(item.get('event_type') or '')
        event_id = item.get('event_id')
        if event_type != 'communication' or not isinstance(event_id, int):
            continue
        content = str(item.get('content') or '')
        if len(content) < min_length:
            continue
        if not looks_like_quoted_email(content):
            continue
        cleaned = strip_quoted_email_text(content)
        if not cleaned or cleaned == content:
            continue
        candidates.append({
            'log_id': event_id,
            'customer_id': item.get('customer_id'),
            'customer': item.get('customer_name') or item.get('company') or '',
            'date': str(item.get('event_date') or ''),
            'activity_type': str(item.get('activity_type') or ''),
            'direction': str(item.get('direction') or ''),
            'before_length': len(content),
            'after_length': len(cleaned),
            'before': content,
            'after': cleaned,
        })
    return candidates


def _snippet(text, width=180):
    flat = ' '.join(str(text or '').split())
    return flat if len(flat) <= width else flat[:width] + '…'


def _action_ref(payload):
    data = payload.get('data') if isinstance(payload, dict) else None
    action = data.get('action') if isinstance(data, dict) else None
    if isinstance(action, dict):
        return action.get('id'), action.get('undo_token'), action.get('undo_description') or ''
    return None, None, ''


def apply_candidates(gateway, candidates):
    """Rewrite each candidate through the reversible update action."""
    results = []
    for candidate in candidates:
        log_id = candidate['log_id']
        try:
            payload = gateway.act('update_communication', None, {'log_id': log_id, 'strip_quotes': True})
        except CliError as error:
            results.append({**candidate, 'status': 'error', 'error': str(error)})
            continue
        action_id, undo_token, undo_description = _action_ref(payload)
        results.append({
            **candidate, 'status': 'updated',
            'action_id': action_id, 'undo_token': undo_token,
            'undo_description': undo_description or '撤销修改沟通记录',
        })
    return results


def _print_report(candidates, results, as_json):
    if as_json:
        print(json.dumps({'candidates': candidates, 'results': results}, ensure_ascii=False, indent=2))
        return
    if not candidates:
        print('没有发现仍带引用历史的沟通记录。')
        return
    print(f'发现 {len(candidates)} 条仍带引用历史的沟通记录：')
    for candidate in candidates:
        print(f"\n- #{candidate['log_id']} · {candidate['customer'] or '(未知客户)'}"
              f" · {candidate['date']} · {candidate['activity_type']}/{candidate['direction']}"
              f" · {candidate['before_length']} -> {candidate['after_length']} 字符")
        print(f"  清洗前: {_snippet(candidate['before'])}")
        print(f"  清洗后: {_snippet(candidate['after'])}")
    if results:
        updated = sum(1 for item in results if item.get('status') == 'updated')
        failed = [item for item in results if item.get('status') == 'error']
        print(f'\n已处理 {updated} 条，失败 {len(failed)} 条。')
        for item in results:
            if item.get('status') == 'updated':
                print(f"  #{item['log_id']} 撤销: python3 tools/trosa_cli.py undo {item.get('action_id')}")
            else:
                print(f"  #{item['log_id']} 失败: {item.get('error')}")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--apply', action='store_true', help='真正执行清洗（默认只列出候选）')
    parser.add_argument('--query', default='', help='只扫描内容命中该关键字的记录（推荐用引用历史标记缩小范围）')
    parser.add_argument('--min-length', dest='min_length', type=int, default=200,
                        help='只处理内容长度不低于该值的记录（默认 200，设 0 不过滤）')
    parser.add_argument('--customer', type=int, default=0, help='只处理某个客户 ID')
    parser.add_argument('--from', dest='from_date', default='', help='起始日期 YYYY-MM-DD')
    parser.add_argument('--to', dest='to_date', default='', help='结束日期 YYYY-MM-DD')
    parser.add_argument('--limit', type=int, default=100, help='每页条数（服务端上限 100）')
    parser.add_argument('--max-pages', dest='max_pages', type=int, default=0, help='最多翻页数（0 表示全部）')
    parser.add_argument('--json', action='store_true', help='输出 JSON')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        gateway = Gateway(load_token())
        items = list(iter_events(
            gateway, query=args.query,
            from_date=args.from_date, to_date=args.to_date,
            customer_id=args.customer or None,
            page_size=max(1, min(args.limit, 100)), max_pages=max(0, args.max_pages),
        ))
        candidates = find_candidates(items, min_length=max(0, args.min_length))
        results = apply_candidates(gateway, candidates) if args.apply else []
    except CliError as error:
        print(str(error), file=sys.stderr)
        return error.code
    _print_report(candidates, results, args.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
