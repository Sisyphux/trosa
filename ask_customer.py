"""Ask the Inbox assistant "is X due for a follow-up?" -- rule-based, no model.

Pure functions only: the caller (app.py) reads the customer's real timeline and
passes plain data in.  The verdict reuses the daily scan in ``followup_candidates``
(customer waiting, quote/sample silence, dormant deal client, customer-stated
date), so the answer here and the quiet line on the customer page never disagree.

Answer shape (fixed): verdict, 2-3 dated basis lines, what the system cannot see.
Nothing here writes; postponing goes through the existing dated-task feature
after the member confirms in the conversation.
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, timedelta

import followup_candidates as fc

CAN, WAIT, NO = '可以跟', '建议再等', '不建议'
ANSWER_MARK = '结论：'            # every answer starts with this; postponing keys off it
CONFIRM_MARK = '请回复「确认」'    # a pending task proposal carries this
GENERIC_NAMES = {'', '那他', '那她', '那', '那么', '他们', '客户', '他', '她', '它', '这个客户', '那个客户', '这位', '那位', '这家', '那家', '这个', '那个'}
QUIET_DAYS = 30                    # no rule fires, but a month of silence is worth a nudge

_MARKERS = re.compile(r'是不是|是否|该不该|要不要|需不需要|能不能|可不可以|吗|嘛|呢|吧|[?？]|怎么样|怎样|咋样|如何')
_TOPIC = re.compile(r'跟进|跟了|跟一下|该跟|可以跟|要跟|跟吗|联系|催|怎么样|怎样|咋样|如何|最近')
_CUT = re.compile(r'是不是|是否|该不该|要不要|需不需要|能不能|可不可以|可以|适合|应该|该|也|还要|最近|现在|怎么样|怎样|咋样|如何|跟|联系|催')
_LEAD = re.compile(r'^(请问|问一下|问下|我想问|想问|帮我看看|帮我看下|看看|看下|查一下|查下|对了|还有)+')
_CONFIRM = re.compile(r'^(确认|好的?|可以|行|记下|记吧|ok|OK|是的?)[。.!！\s]*$')
_CHOICE = re.compile(r'^第?\s*([1-3])\s*(?:个|位|家)?[。.]?$')
_POSTPONE_CUE = re.compile(r'推迟|延后|往后|晚点|改到|等到|再跟|再联系|再说|跟进|记下|提醒|过')
_WEEKDAY = '一二三四五六日天'


def _clean(text) -> str:
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def parse_question(text: str):
    """What the member said: ``('ask', name)``, ``('postpone', text)``, ``('confirm', '')`` or ``None``.

    Deliberately strict: a note such as "今天和 Carlos 通了电话" must go on to the
    normal assistant, so only short questions that name a follow-up topic match.
    """
    t = _clean(text)
    if not t or len(t) > 60 or '\n' in str(text).strip():
        return None
    if _CONFIRM.match(t):
        return 'confirm', ''
    m = _CHOICE.match(t)
    if m:
        return 'choose', int(m.group(1))
    if _MARKERS.search(t) and _TOPIC.search(t):
        cut = _CUT.search(t)
        name = t[:cut.start()] if cut else t
        name = _LEAD.sub('', name.strip())
        name = re.sub(r'[\s，,。.?？!！、:：的]+$', '', name).strip()
        name = name.strip('「」"\'“” ')
        return 'ask', ('' if name in GENERIC_NAMES else name)
    if _POSTPONE_CUE.search(t):
        return 'postpone', t
    return None


def parse_when(text: str, today: date):
    """A future day named in ``text`` ("推迟到11月5日", "下周三", "两周后"), or ``None``."""
    t = _clean(text)
    for day, _ in fc._explicit_dates(t, today):
        if day >= today:
            return day
    m = re.search(r'(\d{1,3}|[一二两三四五六七八九十])\s*(天|日|周|个星期|星期|个月|月)后', t)
    if m:
        n = _number(m.group(1))
        unit = m.group(2)
        if unit in ('天', '日'):
            return today + timedelta(days=n)
        if unit in ('周', '个星期', '星期'):
            return today + timedelta(days=7 * n)
        return _add_months(today, n)
    m = re.search(r'(下|下个)(?:周|星期|礼拜)([一二三四五六日天])?', t)
    if m:
        monday = today + timedelta(days=7 - today.weekday())
        return monday + timedelta(days=_WEEKDAY.index(m.group(2)) % 7) if m.group(2) else monday
    m = re.search(r'(?:这|本)(?:周|星期|礼拜)([一二三四五六日天])', t)
    if m:
        day = today - timedelta(days=today.weekday()) + timedelta(days=_WEEKDAY.index(m.group(1)) % 7)
        return day if day >= today else None
    if re.search(r'后天', t):
        return today + timedelta(days=2)
    if re.search(r'明天', t):
        return today + timedelta(days=1)
    if re.search(r'下个月|下月', t):
        return _add_months(today, 1).replace(day=1)
    if re.search(r'月底|月末', t):
        return today.replace(day=monthrange(today.year, today.month)[1])
    return None


def _number(raw: str) -> int:
    if raw.isdigit():
        return int(raw)
    return {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}[raw]


def _add_months(day: date, months: int) -> date:
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def full_date(day: date) -> str:
    return f'{day.year}年{day.month}月{day.day}日'


def parse_proposal(text: str):
    """The day in a pending proposal message ("记下：2026年11月5日 跟进 X"), or ``None``."""
    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', text or '')
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def proposal_text(name: str, day: date) -> str:
    return f'记下：{full_date(day)} 跟进 {name}。{CONFIRM_MARK}，我就加进待办。'


def pick(results, query: str):
    """Choose from search results: ``(customer, [])`` when clear, ``(None, candidates)`` otherwise."""
    items = [r for r in results if r]
    if not items:
        return None, []
    wanted = _norm(query)
    exact = [r for r in items if wanted and wanted in (_norm(r.get('company')), _norm(r.get('name')))]
    if len(exact) == 1:
        return exact[0], []
    if len(items) == 1:
        return items[0], []
    return None, (exact or items)[:3]


def _norm(value) -> str:
    return re.sub(r'[\s\-_.,，。()（）]+', '', str(value or '')).casefold()


def display_name(customer) -> str:
    company = _clean(customer.get('company'))
    person = _clean(customer.get('name'))
    return company or person or '这位客户'


def _snippet(row) -> str:
    text = _clean(row.get('content') or row.get('result') or row.get('next_plan'))
    return text[:36] + ('…' if len(text) > 36 else '')


def _who(row) -> str:
    side = fc._side(row)
    return {'customer': '客户来信', 'customer_text': '客户说', 'ours': '我们跟进', 'both': '双方往来'}.get(side, '记录')


def _line(row) -> str:
    snippet = _snippet(row)
    return f'· {fc._md(row["_day"])} {_who(row)}' + (f'：{snippet}' if snippet else '')


def assess(account: dict, events, tasks, today: date) -> dict:
    """Verdict for one customer from real records.

    ``account``: ``{'account_id', 'name', 'deleted_at'}``; ``events``: timeline rows as the
    daily scan reads them; ``tasks``: open tasks (``remind_date``, ``title``, ``reminder_type``).
    Returns ``verdict`` (可以跟 / 建议再等 / 不建议), ``why`` (one clause), ``basis`` (dated lines) and
    ``last_day`` (newest record, or ``None``).
    """
    aid = account['account_id']
    params = fc.merge_params()
    rows = fc._normalise_events(events, params, today).get(aid, [])
    last = rows[-1]['_day'] if rows else None
    recent = [_line(r) for r in rows[-3:]][::-1]
    out = {'verdict': WAIT, 'why': '', 'basis': [], 'last_day': last, 'rule': ''}

    def done(verdict, why, basis=None, rule=''):
        out.update(verdict=verdict, why=why, basis=(basis or recent)[:3], rule=rule)
        return out

    if account.get('deleted_at'):
        return done(NO, '这位客户已经归档了')
    if not rows:
        return done(NO, '系统里没有这位客户的沟通记录，我没有依据判断', [])
    open_tasks = [t for t in tasks if not str(t.get('reminder_type') or '').startswith('outreach')]
    dated = sorted((str(t.get('remind_date') or '')[:10], t.get('title') or t.get('content') or '待办')
                   for t in open_tasks if str(t.get('remind_date') or '')[:10])
    if dated:
        when = date.fromisoformat(dated[0][0])
        line = f'· {fc._md(when)} 待办：{_clean(dated[0][1])[:30]}'
        if when >= today:
            return done(WAIT, f'已经安排了 {fc._md(when)} 的下一步', [line] + recent)
        return done(CAN, f'安排的 {fc._md(when)} 待办已经过期，还没处理', [line] + recent)

    scan = fc.scan([account], events, fc.open_task_account_ids(tasks), today, params, '',
                   human_owned_ids=[aid])
    if scan.released:
        cand = scan.released[0]
        by_id = {str(r['_id']): r for r in rows}
        evidence = [_line(by_id[str(i)]) for i in cand.evidence_event_ids if str(i) in by_id][-1:]
        lines = []
        for item in evidence + recent:
            if item not in lines:
                lines.append(item)
        return done(CAN, cand.reason, lines, cand.rule)

    last_rows = [r for r in rows if r['_day'] == last]
    if any(fc._RE_REJECT.search(fc._text(r, 'content', 'result')) for r in last_rows if fc._side(r) != 'ours'):
        return done(NO, '客户最近一次明确表示不需要')
    age = (today - last).days
    sides = {fc._side(r) for r in last_rows}
    if sides & {'ours', 'both'} and age < QUIET_DAYS:
        return done(WAIT, f'我们 {fc._md(last)} 刚联系过，球在客户那边' if age < 14 else f'我们 {fc._md(last)} 联系过，还没到常规的跟进间隔')
    if age >= QUIET_DAYS:
        return done(CAN, f'已经约 {fc._weeks(age)} 周没有联系了，虽然没有触发具体的提醒规则')
    return done(WAIT, f'最近一次记录是 {fc._md(last)}，时间还不长')


def compose(name: str, result: dict, today: date) -> str:
    """The fixed-format answer text."""
    verdict = result['verdict']
    head = f'{ANSWER_MARK}{verdict}。{result["why"]}。' if result['why'] else f'{ANSWER_MARK}{verdict}。'
    parts = [head]
    if result['basis']:
        parts.append('依据：\n' + '\n'.join(result['basis']))
    last = result['last_day']
    if last is None:
        parts.append('这位客户的沟通我这边一条都看不到。')
    else:
        parts.append(f'系统里最后一条记录是 {fc._md(last)}，之后的情况我看不到。')
    if verdict != NO:
        parts.append('要推迟，回我一句「推迟到某月某日」就行。')
    return '\n\n'.join(parts)


def not_found(query: str) -> str:
    return f'没有找到「{query}」这位客户。换个名字、拼音或者邮箱再试试？'


def ask_who() -> str:
    return '你想问的是哪位客户？说一下名字就行。'


CHOOSE_MARK = '有几位对得上'


def choose_text(query: str, labels: list[str]) -> str:
    lines = '\n'.join(f'{n}. {label}' for n, label in enumerate(labels, 1))
    return f'「{query}」{CHOOSE_MARK}，你指的是哪一位？回复序号就行。\n{lines}'


def choose_query(text: str):
    m = re.match(r'「(.+?)」' + CHOOSE_MARK, text or '')
    return m.group(1) if m else None
