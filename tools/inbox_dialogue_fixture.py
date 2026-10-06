"""Deterministic sela-dialogue fixture for the local Inbox browser rehearsal.

Rehearsal-only (``CRM_ENV=rehearsal``). It loads the standard rehearsal fixture
for a real Customer, then replays the frozen contract samples
``tests/fixtures/inbox_contract/examples/dialogue-01..07`` into isolated
``inbox_threads`` through the canonical ``inbox_dialogue`` writer — no HTTP test
hook and no production behaviour is introduced.

For the browser walk each sample stops at a deliberate, documented point so all
three list groups are visible and every sample can be finished by the human in
the real UI:

    dialogue-01  waiting for you (sela asked, suggestions shown)
    dialogue-02  waiting for you (has a customer ref, so 附件 is enabled)
    dialogue-03  waiting for you (first ask; the confirmation ask happens in the UI)
    dialogue-04  waiting for you + a system message (30-day timeout)
    dialogue-05  sela 处理中 (human already replied)
    dialogue-06  已完成 (human replied, sela closed)
    dialogue-07  sela 处理中 + a system message (run failed, then the user replied)

Usage::

    eval "$(python3 tools/postgres_rehearsal.py env)"
    CRM_ENV=rehearsal python3 tools/inbox_dialogue_fixture.py seed
    CRM_ENV=rehearsal python3 tools/inbox_dialogue_fixture.py list
    # while a thread is open in the UI, force a conflict on the next reply:
    CRM_ENV=rehearsal python3 tools/inbox_dialogue_fixture.py bump <thread_id>
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if os.environ.get('CRM_ENV') != 'rehearsal':
    raise SystemExit('Inbox dialogue fixture is rehearsal-only')

from tools.postgres_rehearsal import ensure_database, load_fixture  # noqa: E402
import db  # noqa: E402
import inbox_dialogue  # noqa: E402

FIXTURE_USER = 'hamid'
EXAMPLES = ROOT / 'tests' / 'fixtures' / 'inbox_contract' / 'examples'

# How far to replay each sample; see the module docstring.
PLAN = {
    1: 'awaiting',        # stop before the human reply
    2: 'awaiting',        # stop before the human reply (+ customer ref for 附件)
    3: 'awaiting',        # stop before the human reply
    4: 'awaiting_system',  # stop before the human reply, keep the system message
    5: 'reply',           # apply the recorded human reply -> sela 处理中
    6: 'closed',          # apply the human reply, then sela closes -> 已完成
    7: 'reply_system',    # apply the human reply, then the system failure message
}

# dialogue-02 is the attachment walk: give it the seeded customer so the reply
# box can upload a real file through POST /api/customers/<id>/files.
CUSTOMER_REF_SLUG = 'dialogue-02'


def _example(slug_prefix):
    for path in sorted(EXAMPLES.glob('dialogue-*.json')):
        value = json.loads(path.read_text(encoding='utf-8'))['value']
        if value['slug'].startswith(slug_prefix):
            return value
    raise SystemExit(f'sample not found: {slug_prefix}')


def _turns(value):
    return [(index, event['turn']) for index, event in enumerate(value['events'])
            if event.get('op') == 'turn']


def _first_human_index(value):
    for index, event in enumerate(value['events']):
        if event.get('op') == 'human_reply':
            return index
    return len(value['events'])


def _system_texts_before(value, stop_index):
    return [event['turn']['text'] for index, event in enumerate(value['events'])
            if index < stop_index and event.get('op') == 'turn'
            and event['turn'].get('role') == 'system']


def _system_texts_after(value, start_index):
    return [event['turn']['text'] for index, event in enumerate(value['events'])
            if index >= start_index and event.get('op') == 'turn'
            and event['turn'].get('role') == 'system']


def _human_reply_text(value, start_index):
    for index, event in enumerate(value['events']):
        if index >= start_index and event.get('op') == 'human_reply':
            return event['human_reply']
    return None


def _append_system(conn, thread_id, text):
    row = inbox_dialogue._thread_row(conn, thread_id)
    seq = inbox_dialogue._next_seq(conn, thread_id)
    now = inbox_dialogue._now()
    inbox_dialogue._insert_message(
        conn, thread_id=thread_id, seq=seq, role='system', text=text,
        actor='runtime', awaiting_after=None, created_at=now,
    )
    inbox_dialogue._touch_thread(
        conn, thread_id, revision=seq, awaiting=row['awaiting'], updated_at=now,
    )


def _create_payload(value, customer_id=None, slug=None):
    first = _turns(value)[0][1]
    refs = list(first.get('refs') or [])
    if customer_id is not None:
        refs.append({'type': 'customer', 'id': str(customer_id)})
    return {
        'subject': f"{value['subject']}#{slug}",
        'title': value['title'],
        'text': first['text'],
        'suggested_replies': first.get('suggested_replies') or [],
        'refs': refs,
        'awaiting': 'human',
    }


def _wipe(conn):
    where, params = inbox_dialogue._scope()
    for table in ('inbox_action_receipts', 'inbox_messages', 'inbox_threads'):
        conn.execute(f"DELETE FROM {inbox_dialogue._t(table)} WHERE {where}", params)


def _thread_revision(conn, thread_id):
    return int(inbox_dialogue.get_thread(conn, thread_id=thread_id)['thread']['revision'])


def seed():
    ids = load_fixture()
    db.set_db_user(FIXTURE_USER)
    conn = db.get_db()
    try:
        _wipe(conn)
        seeded = []
        labels = {}
        for number in sorted(PLAN):
            value = _example(f'dialogue-{number:02d}')
            slug = value['slug']
            plan = PLAN[number]
            stop = _first_human_index(value)
            customer_id = ids['customer_id'] if slug.startswith(CUSTOMER_REF_SLUG) else None
            # Only the opening sela ask plus any system messages that precede the
            # human reply are seeded; the human finishes the rest in the UI.
            body = inbox_dialogue.create_thread(
                conn, payload=_create_payload(value, customer_id=customer_id, slug=slug),
            )
            thread_id = body['thread']['id']
            for text in _system_texts_before(value, stop):
                _append_system(conn, thread_id, text)
            if plan in ('reply', 'reply_system', 'closed'):
                reply = _human_reply_text(value, stop)
                if reply:
                    inbox_dialogue.reply_human(
                        conn, thread_id=thread_id,
                        payload={'text': reply, 'seen_revision': _thread_revision(conn, thread_id)},
                        actor=FIXTURE_USER,
                    )
                for text in _system_texts_after(value, stop):
                    _append_system(conn, thread_id, text)
                if plan == 'closed':
                    inbox_dialogue.close_thread(
                        conn, thread_id=thread_id,
                        payload={'summary': value.get('outcome', {}).get('closed_summary') or 'sela 对话已完成'},
                        closed_by='sela',
                    )
            seeded.append(thread_id)
            labels[thread_id] = {'slug': slug, 'plan': plan,
                                 'awaiting': inbox_dialogue.get_thread(conn, thread_id=thread_id)['thread']['awaiting']}
        conn.commit()
        return {'customer_id': ids['customer_id'], 'threads': seeded, 'labels': labels}
    finally:
        conn.close()


def list_threads():
    db.set_db_user(FIXTURE_USER)
    conn = db.get_db()
    try:
        body = inbox_dialogue.list_threads(conn, status='all', limit=200)
        return {
            'counts': body['counts'],
            'threads': [
                {'id': t['id'], 'title': t['title'], 'subject': t['subject'],
                 'status': t['status'], 'awaiting': t['awaiting']}
                for t in body['threads']
            ],
        }
    finally:
        conn.close()


def bump(thread_id):
    """Append a sela message so the next human reply hits ``thread_changed``."""
    db.set_db_user(FIXTURE_USER)
    conn = db.get_db()
    try:
        body = inbox_dialogue.append_message(
            conn, thread_id=thread_id,
            payload={'text': '（rehearsal）sela 刚刚补充了一条新消息，请先看。', 'awaiting': 'human'},
        )
        conn.commit()
        return {'thread_id': thread_id, 'revision': body['thread']['revision']}
    finally:
        conn.close()


def main(argv):
    command = argv[0] if argv else 'seed'
    if command == 'seed':
        ensure_database()
        print(json.dumps(seed(), ensure_ascii=False, indent=2))
    elif command == 'list':
        print(json.dumps(list_threads(), ensure_ascii=False, indent=2))
    elif command == 'bump' and len(argv) > 1:
        print(json.dumps(bump(argv[1]), ensure_ascii=False, indent=2))
    else:
        raise SystemExit('usage: inbox_dialogue_fixture.py seed|list|bump <thread_id>')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
