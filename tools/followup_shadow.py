#!/usr/bin/env python3
"""Shadow run of the follow-up candidate engine on real data (read-only, local only).

Reads every member's accounts, timeline and open tasks through the maintainer
read-only channel (``tools/trosa_maint_read.py``: one SELECT per call, date
windows / id chunks so no single query is large), runs
``followup_candidates.scan`` once per member and writes one plain-text list per
member *outside the repository* (default ``~/Desktop/Trosa-影子名单/<date>/``).
The lists contain customer names so the boss and the member can judge them;
nothing about customers is printed to the terminal - only counts.

Nothing is written to any database and nothing is published.

Usage::

    /Users/luoxin/Desktop/Trosa/.venv/bin/python tools/followup_shadow.py
    ... tools/followup_shadow.py --date 2026-10-28 --members hamid amy
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / 'tools'):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import followup_candidates as fc  # noqa: E402

TZ = ZoneInfo('Asia/Shanghai')
MEMBERS = ('hamid', 'amy', 'kelley', 'emma')
MEMBER_LABEL = {'hamid': 'Hamid', 'amy': 'Amy', 'kelley': 'Kelley', 'emma': 'Emma'}
# What the 2026-10-27 hand count found (docs/research/notes/改造落地路线/follow_up_rhythm_data.md, 第三节).
HAND_COUNT = {'hamid': 35, 'kelley': 23, 'amy': 21, 'emma': 0}
BOUNCE_TYPES = {'outreach_bounced', '外联退信'}
DEFAULT_OUT = Path.home() / 'Desktop' / 'Trosa-影子名单'
EVENT_WINDOW_DAYS = 120
ROW_LIMIT = 400                       # rows asked per query; the channel allows 500

# query(sql, params, limit) -> (list of row dicts, truncated)
Query = Callable[[str, tuple, int], tuple]


# ---------------------------------------------------------------------------
# Reading (thin layer over the read-only channel)
# ---------------------------------------------------------------------------

def build_query() -> Query:
    """The real query function: ``trosa_maint_read.run_query`` with retries for dropped tunnels."""
    import trosa_maint_read as maint

    def query(sql: str, params: tuple = (), limit: int = ROW_LIMIT):
        last: Exception | None = None
        for attempt in range(4):
            try:
                maint._audit('followup_shadow', ' '.join(sql.split())[:300])
                columns, rows, truncated = maint.run_query(sql, params, limit=limit)
                return [dict(zip(columns, row)) for row in rows], truncated
            except Exception as exc:                      # noqa: BLE001 - only transient ones are retried
                transient = type(exc).__name__ in ('OperationalError', 'InterfaceError') \
                    or (isinstance(exc, maint.MaintError) and '隧道' in str(exc))
                if not transient:
                    raise
                last = exc
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f'只读通道重试 4 次仍失败：{last}')

    return query


def fetch_window(query: Query, sql: str, params: tuple, lo: datetime, hi: datetime) -> list[dict]:
    """Rows whose timestamp is in ``[lo, hi)``; a window that comes back full is split in two.

    ``sql`` must end with ``... >= %s AND ... < %s ORDER BY ...`` (the two bounds are appended to ``params``).
    """
    rows, truncated = query(sql, params + (lo, hi), ROW_LIMIT)
    if not truncated:
        return rows
    if hi - lo <= timedelta(days=1):
        raise RuntimeError(f'{lo.date()} 当天记录超过 {ROW_LIMIT} 条，无法再拆分')
    mid = lo + (hi - lo) / 2
    return fetch_window(query, sql, params, lo, mid) + fetch_window(query, sql, params, mid, hi)


def fetch_by_ids(query: Query, sql: str, ids: list[str], chunk: int = 100) -> list[dict]:
    """Run ``sql`` (one ``%s::uuid[]`` placeholder) over id chunks; a full chunk is split in two."""
    out: list[dict] = []
    pending = [ids[i:i + chunk] for i in range(0, len(ids), chunk)]
    while pending:
        part = pending.pop(0)
        rows, truncated = query(sql, (part,), ROW_LIMIT)
        if truncated and len(part) > 1:
            half = len(part) // 2
            pending[:0] = [part[:half], part[half:]]
        elif truncated:
            raise RuntimeError('单个客户的行数超过单次上限')
        else:
            out.extend(rows)
    return out


_EVENT_SQL = '''
SELECT e.id, e.account_id, e.event_type, e.direction, e.content, e.result, e.next_plan,
       e.source_module, e.occurred_at, e.payload
  FROM trosa.timeline_events e
  JOIN trosa.accounts a ON a.id = e.account_id
  JOIN identity.users u ON u.id = a.owner_user_id
 WHERE u.legacy_user_id = %s
   AND lower(coalesce(e.payload->>'is_deleted', '0')) NOT IN ('1', 'true')
   AND e.occurred_at >= %s AND e.occurred_at < %s
 ORDER BY e.occurred_at, e.id'''

_FIRST_EVENT_SQL = '''
SELECT min(e.occurred_at) AS first_at
  FROM trosa.timeline_events e
  JOIN trosa.accounts a ON a.id = e.account_id
  JOIN identity.users u ON u.id = a.owner_user_id
 WHERE u.legacy_user_id = %s'''

_ACCOUNT_SQL = '''
SELECT a.id, a.display_name, a.deleted_at, d.notes, d.system_notes, d.import_source
  FROM trosa.accounts a
  LEFT JOIN trosa.customer_details d ON d.account_id = a.id
 WHERE a.id = ANY(%s::uuid[])'''

_MAIL_SQL = '''
SELECT account_id, reply_status, subject, body, reply_content
  FROM trosa.outreach_messages WHERE account_id = ANY(%s::uuid[])'''

_INBOX_SQL = '''
SELECT account_id, title, content FROM trosa.inbox_items WHERE account_id = ANY(%s::uuid[])'''

_TOTALS_SQL = '''
SELECT u.legacy_user_id AS member, count(*) AS accounts,
       count(*) FILTER (WHERE a.deleted_at IS NOT NULL) AS archived
  FROM trosa.accounts a JOIN identity.users u ON u.id = a.owner_user_id
 GROUP BY 1'''

_TASK_SQL = '''
SELECT t.id, t.account_id, t.task_type, t.status
  FROM trosa.tasks t
 WHERE t.status = 'open' AND t.id > %s::uuid
 ORDER BY t.id LIMIT 300'''


def load_totals(query: Query) -> dict[str, dict]:
    rows, _ = query(_TOTALS_SQL, (), ROW_LIMIT)
    return {r['member']: {'accounts': int(r['accounts']), 'archived': int(r['archived'])} for r in rows}


def load_open_tasks(query: Query) -> list[dict]:
    """Every open task row (all kinds); ``fc.open_task_account_ids`` skips the legacy outreach ones."""
    out: list[dict] = []
    after = '00000000-0000-0000-0000-000000000000'
    while True:
        rows, _ = query(_TASK_SQL, (after,), ROW_LIMIT)
        out.extend({'account_id': str(r['account_id']), 'reminder_type': r['task_type'], 'status': r['status']}
                   for r in rows)
        if len(rows) < 300:
            return out
        after = str(rows[-1]['id'])


def load_member(query: Query, member: str, today: date) -> dict:
    """Everything the engine needs for one member, as plain dicts (no customer data leaves this process)."""
    rows, _ = query(_FIRST_EVENT_SQL, (member,), 1)
    first = rows[0]['first_at'] if rows and rows[0]['first_at'] else None
    events: list[dict] = []
    if first is not None:
        lo = first.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        end = datetime.combine(today + timedelta(days=1), datetime.min.time(), TZ)
        while lo < end:
            hi = min(lo + timedelta(days=EVENT_WINDOW_DAYS), end)
            events.extend(fetch_window(query, _EVENT_SQL, (member,), lo, hi))
            lo = hi
    ids = sorted({str(e['account_id']) for e in events})
    accounts = fetch_by_ids(query, _ACCOUNT_SQL, ids)
    mails: dict[str, list] = {}
    for row in fetch_by_ids(query, _MAIL_SQL, ids, chunk=30):
        mails.setdefault(str(row['account_id']), []).append(row)
    inbox: dict[str, list] = {}
    for row in fetch_by_ids(query, _INBOX_SQL, ids):
        inbox.setdefault(str(row['account_id']), []).append(row)
    return {
        'member': member,
        'events': [{**e, 'event_id': str(e['id']), 'account_id': str(e['account_id']), 'owner': member}
                   for e in events],
        'accounts': [{'account_id': str(a['id']), 'name': a['display_name'], 'deleted_at': a['deleted_at'],
                      'notes': a['notes'], 'system_notes': a['system_notes'], 'import_source': a['import_source']}
                     for a in accounts],
        'mails': mails,
        'inbox': inbox,
    }


# ---------------------------------------------------------------------------
# Pure logic (covered by tests/test_followup_shadow.py with synthetic data)
# ---------------------------------------------------------------------------

def last_record_days(events: Iterable[Mapping], today: date, params: Mapping | None = None) -> dict[Any, date]:
    """account id -> day of the newest usable record (the same filtering the engine applies)."""
    cfg = fc.merge_params(params)
    grouped = fc._normalise_events(events, cfg, today)
    return {account_id: rows[-1]['_day'] for account_id, rows in grouped.items() if rows}


def peer_index(member_data: Mapping[str, Mapping], today: date, params: Mapping | None = None) -> dict[str, dict[str, date]]:
    """member -> {customer name: newest record day} over that member's accounts."""
    index: dict[str, dict[str, date]] = {}
    for member, data in member_data.items():
        days = last_record_days(data['events'], today, params)
        names: dict[str, date] = {}
        for account in data['accounts']:
            day = days.get(account['account_id'])
            if day is not None and account.get('name'):
                if account['name'] not in names or day > names[account['name']]:
                    names[account['name']] = day
        index[member] = names
    return index


def peers_of(member: str, index: Mapping[str, Mapping[str, date]]) -> dict[str, date]:
    """Newest record day per customer name held by the *other* members."""
    merged: dict[str, date] = {}
    for other, names in index.items():
        if other == member:
            continue
        for name, day in names.items():
            if name not in merged or day > merged[name]:
                merged[name] = day
    return merged


def batch_day_stats(events: Iterable[Mapping], accounts: Iterable[Mapping], today: date,
                    params: Mapping | None = None) -> dict[str, int]:
    """How many bulk back-fill days (and records on them) the member has, among live customers."""
    cfg = fc.merge_params(params)
    live = {a['account_id'] for a in accounts if not a.get('deleted_at')}
    grouped = fc._normalise_events(events, cfg, today)
    days: set = set()
    rows_total = 0
    for account_id, rows in grouped.items():
        if account_id not in live:
            continue
        for row in rows:
            if row['_batch']:
                rows_total += 1
                days.add(row['_day'])
    return {'days': len(days), 'records': rows_total}


def _is_bounce(raw: Mapping) -> bool:
    """A delivery-failure record: typed as a bounce, or a customer-side row that is really a bounce notice."""
    text = ' '.join(str(raw.get(f) or '') for f in ('content', 'result', 'next_plan'))
    customer_typed = str(raw.get('direction') or '').lower() == 'inbound' \
        or str(raw.get('event_type') or '') in ('customer_reply', '客户回复')
    return raw.get('event_type') in BOUNCE_TYPES or (customer_typed and fc._bounce_text(text))


def bounce_event_ids(events: Iterable[Mapping]) -> set:
    return {raw.get('event_id') for raw in events if _is_bounce(raw)}


def bounced_last(events: Iterable[Mapping]) -> dict:
    """account id -> day of the bounce, for accounts whose newest record of any kind is a delivery failure."""
    latest: dict[Any, tuple] = {}
    for index, raw in enumerate(events):
        key = (raw.get('occurred_at') or datetime.min.replace(tzinfo=TZ), index)
        account_id = raw.get('account_id')
        if account_id not in latest or key >= latest[account_id][0]:
            latest[account_id] = (key, raw)
    return {account_id: fc._to_date(raw.get('occurred_at'), TZ)
            for account_id, (_, raw) in latest.items() if _is_bounce(raw)}


def sela_only_accounts(events: Iterable[Mapping]) -> set:
    """Pure Sela development customers: every record is Sela's and the customer never spoke.

    A customer who answered a Sela mail (the Sela hand-off) is a real conversation, not "pure Sela".
    """
    seen: dict[Any, bool] = {}
    for raw in events:
        source = str(raw.get('source_module') or '').lower()
        from_sela = 'sela' in source or source.startswith('outreach')
        customer_spoke = str(raw.get('direction') or '').lower() == 'inbound' \
            or str(raw.get('event_type') or '') in ('customer_reply', '客户回复')
        seen[raw.get('account_id')] = seen.get(raw.get('account_id'), True) and from_sela and not customer_spoke
    return {account_id for account_id, only in seen.items() if only}


def self_check(entries: Iterable[fc.Candidate], accounts: Iterable[Mapping], events: Iterable[Mapping],
               open_task_ids: set, first_day: list, first_day_cap: int, pool: Iterable[fc.Candidate] = ()) -> list[dict]:
    """Prove the list holds no archived / bounced / Sela-only / already-tasked customer."""
    entries = list(entries)
    archived = {a['account_id'] for a in accounts if a.get('deleted_at')}
    events = list(events)
    bounce_ids = bounce_event_ids(events)
    sela = sela_only_accounts(events)
    ids = [c.account_id for c in entries]
    pool = list(pool)

    def check(name: str, offenders: Iterable) -> dict:
        count = len(list(offenders))
        return {'name': name, 'passed': count == 0, 'offenders': count}

    return [
        check('没有已归档客户', [i for i in ids if i in archived]),
        check('没有靠退信记录/退信通知当证据的客户', [c.account_id for c in entries
                                          if bool(set(c.evidence_event_ids) & bounce_ids)]),
        check('没有纯 Sela 开发客户（全部记录来自 Sela）', [i for i in ids if i in sela]),
        check('没有已有未完成待办的客户', [i for i in ids if i in open_task_ids]),
        check('每个客户只出现一次', [i for i, n in Counter(c.account_id for c in pool).items() if n > 1]),
        check(f'首日放出不超过 {first_day_cap} 条', first_day if len(first_day) > first_day_cap else []),
    ]


def run_scans(data: Mapping, today: date, peers: Mapping[str, date], open_task_ids: set,
              params: Mapping | None = None) -> dict:
    """Two scans per member: the first day after switch-on (smaller cap) and a steady day."""
    common = dict(owner=data['member'], mails=data['mails'], inbox=data['inbox'], peer_last_contact=peers)
    args = (data['accounts'], data['events'], open_task_ids, today, params)
    first = fc.scan(*args, launch_date=today, **common)
    steady = fc.scan(*args, **common)
    return {'first_day': first, 'steady': steady}


def summarise_member(data: Mapping, scans: Mapping, totals: Mapping, today: date,
                     open_task_ids: set, params: Mapping | None = None) -> dict:
    """Numbers only - safe to print and to quote in a report."""
    first, steady = scans['first_day'], scans['steady']
    pool = steady.released + steady.held_back
    with_events = len(data['accounts'])
    checks = self_check(first.released + pool, data['accounts'], data['events'], open_task_ids,
                        first.released, int(fc.merge_params(params)['limits']['launch_cap']), pool)
    return {
        'member': data['member'],
        'accounts_total': totals.get('accounts', 0),
        'accounts_archived': totals.get('archived', 0),
        'accounts_with_events': with_events,
        'accounts_without_events': max(0, totals.get('accounts', 0) - with_events),
        'excluded': dict(steady.excluded),
        'fired': dict(steady.fired),
        'pool_by_rule': dict(Counter(c.rule for c in pool)),
        'first_day_released': len(first.released),
        'first_day_by_rule': dict(Counter(c.rule for c in first.released)),
        'steady_released': len(steady.released),
        'held_back': len(steady.held_back),
        'suppressed': len(steady.suppressed),
        'archived_with_contact': len(steady.archived_with_contact),
        'bounced_latest': len({c.account_id for c in pool} & set(bounced_last(data['events']))),
        'candidates_total': len(pool),
        'batch_days': batch_day_stats(data['events'], data['accounts'], today, params),
        'checks': checks,
    }


# ---------------------------------------------------------------------------
# Writing (outside the repository only)
# ---------------------------------------------------------------------------

def safe_out_dir(base: Path, today: date) -> Path:
    """``<base>/<date>``; refuses a place inside the repository (the lists carry customer names)."""
    target = (Path(base).expanduser() / today.isoformat()).resolve()
    root = ROOT.resolve()
    if target == root or root in target.parents:
        raise SystemExit(f'名单含客户名，不能写进仓库：{target}')
    return target


PLAIN_RULE = {'R1': '客户来信没回', 'R2': '报价后没下文', 'R3': '老客户很久没联系', 'R4': '客户说过的日期到了'}
# Members whose mailbox the system cannot read (Netease etc.): "customer wrote, nobody answered" cannot be seen for them.
MAIL_NOT_CONNECTED = {'amy', 'kelley', 'emma'}
SHOWN_DAILY = 2


def short_name(name: Any) -> str:
    """Customer name for a list: first line only, no URL tails."""
    lines = [ln.strip() for ln in str(name or '').splitlines() if ln.strip()]
    first = lines[0] if lines else ''
    return (first[:40] + '…') if len(first) > 40 else first


def _entry_lines(index: int, candidate: fc.Candidate, names: Mapping, last_days: Mapping,
                 bounced: Mapping = {}) -> list[str]:
    name = short_name(names.get(candidate.account_id)) or '（无名称）'
    ctx = dict(candidate.facts.get('context') or {})
    lines = [f'{index}. {name}　［{PLAIN_RULE[candidate.rule]}］',
             f'   为什么现在提：{candidate.reason}']
    label = candidate.facts.get('deal_label') or '成交相关'
    if candidate.rule in ('R2', 'R3'):
        if ctx.get('deal_detail'):
            lines.append(f'   当时的{label}：{ctx["deal_detail"]}')
        elif ctx.get('deal_text') and ctx['deal_text'] != ctx.get('last_words'):
            lines.append(f'   当时的{label}：{ctx["deal_text"]}')
    words = ctx.get('last_words') or ctx.get('last_topic') or ''
    who = ctx.get('last_speaker')
    if who and words:
        head = '上次聊到（电话/面谈类的双方记录）' if who == '双方' else f'上次聊到 / 最后一句话（{who}说的）'
        lines.append(f'   {head}：{words}')
    if candidate.account_id in bounced:
        lines.append(f'   ⚠ 最新一条是退信（{bounced[candidate.account_id]}），这个邮箱可能已失效')
    lines.append('   判断：对 / 不对 / 已经聊过（请圈一个）')
    return lines


def render_member_file(data: Mapping, scans: Mapping, summary: Mapping, today: date,
                      last_days: Mapping, params: Mapping | None = None) -> str:
    names = {a['account_id']: a['name'] for a in data['accounts']}
    bounced = bounced_last(data['events'])
    steady = scans['steady']
    member = MEMBER_LABEL.get(data['member'], data['member'])
    out = ['Trosa 跟进建议 · 影子名单（试跑，不进系统，只供判断准不准）',
           f'成员：{member}　日期：{today.isoformat()}',
           f'每天最多只提 {SHOWN_DAILY} 条，宁少勿多；多数日子可以是 0 条。每条请圈：对 / 不对 / 已经聊过。', '']
    out.append(f'【今天会提的（共 {len(steady.released)} 条）】')
    if not steady.released:
        out.append('（今天没有足够把握的，所以一条都不提）')
    for i, c in enumerate(steady.released, 1):
        out += _entry_lines(i, c, names, last_days, bounced)
    out += ['', f'【条件也成立、但今天名额已满，之后几天才轮到的（共 {len(steady.held_back)} 条，只列名字）】']
    if not steady.held_back:
        out.append('（没有）')
    for i, c in enumerate(steady.held_back, 1):
        out.append(f'{i}. {short_name(names.get(c.account_id)) or "（无名称）"}［{PLAIN_RULE[c.rule]}］')
    out += ['', '【说明】']
    if data['member'] in MAIL_NOT_CONNECTED:
        out.append('这个人的邮件没有接入系统，所以「客户来信没回」基本看不到，名单里只有手工记录能支撑的几类。')
    out += summary_lines(summary)
    return '\n'.join(out) + '\n'


EXCLUDE_LABEL = {
    'archived': '已归档', 'no_real_events': '没有真实沟通记录（只有退信/外联等）', 'has_open_task': '已有未完成待办',
    'sela_only': '纯 Sela 开发客户', 'ignored_twice': '近期被忽略过两次', 'no_rule_matched': '现在没有需要提的理由',
    'possible_duplicate': '可能重复客户（对方有更新记录）',
    'incomplete_reason': '理由讲不完整（缺上次聊到的内容或最后是谁说的），宁可不提',
}


def summary_lines(s: Mapping) -> list[str]:
    member = MEMBER_LABEL.get(s['member'], s['member'])
    lines = [
        f'{member}：名下客户 {s["accounts_total"]} 个（归档 {s["accounts_archived"]}），其中有沟通记录的 {s["accounts_with_events"]} 个',
        '  满足条件的客户数（收紧之前的候选）：' + ('、'.join(f'{PLAIN_RULE[r]} {s["fired"].get(r, 0)}' for r in PLAIN_RULE)),
        '  没提的原因：' + ('、'.join(f'{EXCLUDE_LABEL.get(k, k)} {v}' for k, v in s['excluded'].items()) or '无'),
        f'  最后：今天提 {s["steady_released"]} 条，排队待提 {s["held_back"]} 条，因重复客户压住 {s["suppressed"]} 条',
        '  邮件：' + ('这个人的邮件没接入，「客户来信没回」读不到，所以缺' if s['member'] in MAIL_NOT_CONNECTED
                      else '已接入（Sela 读取）'),
    ]
    if s['bounced_latest']:
        lines.append(f'  提示：{s["bounced_latest"]} 个建议客户最新一条是退信')
    failed = [c for c in s['checks'] if not c['passed']]
    lines.append('  自检：' + ('全部通过' if not failed else '；'.join(f'{c["name"]} 失败({c["offenders"]})' for c in failed)))
    return lines


def write_lists(out_dir: Path, today: date, per_member: Mapping[str, str], summaries: list[dict]) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)
    written = []
    for member, text in per_member.items():
        path = out_dir / f'{MEMBER_LABEL.get(member, member)}.txt'
        path.write_text(text, encoding='utf-8')
        path.chmod(0o600)
        written.append(path)
    total = out_dir / '汇总（只有数字）.txt'
    body = [f'Trosa 跟进建议 · 影子名单汇总　{today.isoformat()}', '']
    for s in summaries:
        body += summary_lines(s) + ['']
    total.write_text('\n'.join(body), encoding='utf-8')
    total.chmod(0o600)
    return written + [total]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(query: Query, today: date, members: Iterable[str], out_base: Path, params: Mapping | None = None) -> dict:
    members = list(members)
    out_dir = safe_out_dir(out_base, today)
    totals = load_totals(query)
    tasks = load_open_tasks(query)
    open_ids = fc.open_task_account_ids(tasks)
    data = {m: load_member(query, m, today) for m in members}
    index = peer_index(data, today, params)
    summaries: list[dict] = []
    files: dict[str, str] = {}
    for member in members:
        d = data[member]
        scans = run_scans(d, today, peers_of(member, index), open_ids, params)
        summary = summarise_member(d, scans, totals.get(member, {}), today, open_ids, params)
        summaries.append(summary)
        files[member] = render_member_file(d, scans, summary, today, last_record_days(d['events'], today, params), params)
    paths = write_lists(out_dir, today, files, summaries)
    return {'summaries': summaries, 'paths': paths, 'out_dir': out_dir,
            'open_task_rows': len(tasks), 'open_task_accounts': len(open_ids),
            'outreach_open_rows_skipped': sum(1 for t in tasks if str(t['reminder_type']).startswith('outreach'))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--date', help='运行日期 YYYY-MM-DD，默认今天（北京时间）')
    parser.add_argument('--members', nargs='+', default=list(MEMBERS), choices=MEMBERS)
    parser.add_argument('--out', default=str(DEFAULT_OUT), help='名单目录（必须在仓库之外）')
    args = parser.parse_args(argv)
    today = date.fromisoformat(args.date) if args.date else datetime.now(TZ).date()
    import trosa_maint_read as maint
    try:
        config = maint._load_config()
        with maint._tunnel(config):                       # one tunnel for the whole run
            result = run(build_query(), today, args.members, Path(args.out))
    except maint.MaintError as exc:
        print(f'只读通道不可用：{exc}', file=sys.stderr)
        return 2
    print(f'影子名单（{today.isoformat()}）已写入：{result["out_dir"]}')
    print(f'待办表：未完成 {result["open_task_rows"]} 行，其中旧外联类 {result["outreach_open_rows_skipped"]} 行已跳过，'
          f'真正占用客户的 {result["open_task_accounts"]} 个')
    failed = 0
    for summary in result['summaries']:
        print()
        print('\n'.join(summary_lines(summary)))
        failed += sum(1 for c in summary['checks'] if not c['passed'])
    print()
    print('自检全部通过' if not failed else f'自检有 {failed} 项失败，请先查原因再看名单')
    return 0 if not failed else 3


if __name__ == '__main__':
    raise SystemExit(main())
