#!/usr/bin/env python3
"""Seed one de-identified Customer for the 「导出全部沟通」 browser check.

The record keeps the shape of a real quotation / drawing / sample
conversation (including a plan we already told the customer cannot be made)
without any real company, person, address or price sheet.

    .venv/bin/python tools/repro_seed_context_export.py --app-root . --db-dir <scratch>

It is meant to run against an isolated dev database created by
``tools/repro_dev_server.py``.  Run it before the server starts, or while the
server is idle, then point the repro script at that server.
"""
import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

COMPANY = '脱敏亚克力客户（导出验收）'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app-root', required=True)
    parser.add_argument('--db-dir', required=True)
    parser.add_argument('--user', default='hamid')
    args = parser.parse_args()
    root = Path(args.app_root).resolve()
    db_dir = Path(args.db_dir).expanduser().resolve()
    if root / 'data' == db_dir or (root / 'data') in db_dir.parents:
        raise SystemExit('refusing to seed a repository data folder')
    os.environ.update({
        'CRM_ENV': 'development',
        'TRADE_OS_DEV_SQLITE': '1',
        'TRADE_OS_DATA_BACKEND': 'sqlite',
        'CRM_DB_PATH': str(db_dir),
    })
    os.environ.pop('TRADE_OS_DATABASE_URL', None)
    sys.path.insert(0, str(root))
    import db

    db.DB_DIR = str(db_dir)
    db.init_all_dbs()
    db.set_db_user(args.user)
    conn = db.get_db()
    today = date.today()
    try:
        existing = conn.execute('SELECT id FROM customers WHERE company=?', (COMPANY,)).fetchone()
        if existing:
            conn.execute('DELETE FROM customers WHERE id=?', (existing[0],))
        cursor = conn.execute(
            """INSERT INTO customers (name, company, country, profile, notes, customer_judgment, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            ('采购联系人（脱敏）', COMPANY, '美国', '亚克力板与定制膜采购',
             '客户要求报价含包装，并指定到港前提供装箱图。', '等客户确认寄样地址',
             (today - timedelta(days=120)).isoformat(), (today - timedelta(days=120)).isoformat()))
        customer_id = cursor.lastrowid
        for index in range(60):
            day = (today - timedelta(days=index % 20)).isoformat()
            conn.execute(
                """INSERT INTO follow_up_logs
                   (customer_id, content, follow_date, activity_type, direction, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (customer_id, f'历史沟通 [H{index + 1:03d}]：讨论报价与图纸', day,
                 'whatsapp', 'two_way', f'{day} 00:{index:02d}:00'))
        for offset, direction, content, next_plan in (
            (9, 'inbound', '客户说可以的话寄样，先看板子质量', ''),
            (5, 'inbound', '请按图纸 B-204 报价，原弯管方案可以吗', ''),
            (4, 'outbound', '图纸已收到；原弯管方案做不了，需要改直管或另开模具', ''),
            (3, 'inbound', '旧记录：客户说下月再谈', '30 天后再问一次'),
        ):
            day = (today - timedelta(days=offset)).isoformat()
            conn.execute(
                """INSERT INTO follow_up_logs
                   (customer_id, content, follow_date, activity_type, direction, next_plan, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (customer_id, content, day, 'email', direction, next_plan, day + ' 09:00:00'))
        for subject, body, offset, status, reply, reply_offset in (
            ('Acrylic quotation - 3mm', '附上 3mm 透明亚克力板报价：USD 12.5/张，含包装。',
             10, 'replied', '可以的话寄样', 7),
            ('Follow up - drawings', '', 6, 'pending', '', None),
            ('Sample policy', '我们的样品政策是首次寄样免费，运费由客户承担。',
             12, 'bounced', '550 5.1.1 user unknown', 12),
        ):
            sent = (today - timedelta(days=offset)).isoformat()
            reply_day = '' if reply_offset is None else (today - timedelta(days=reply_offset)).isoformat()
            conn.execute(
                """INSERT INTO outreach_emails
                   (customer_id, subject, content, sent_date, reply_status, reply_content, reply_date, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (customer_id, subject, body, sent, status, reply, reply_day, sent + ' 08:00:00'))
        conn.execute(
            """INSERT INTO reminders (customer_id, title, content, remind_date, is_done, reminder_type, created_at)
               VALUES (?,?,?,?,0,'follow_up',?)""",
            (customer_id, '寄样并确认运费', '先确认收件地址，再报运费',
             (today + timedelta(days=8)).isoformat(), today.isoformat()))
        research = {
            'qualification_status': 'qualified',
            'confidence': 'high',
            'reason': '官网显示生产亚克力展示架，采购需求与我们的板材匹配。',
            'source_urls': ['https://example.invalid/about'],
            'evidence': [{'text': 'Acrylic display manufacturer', 'source_url': 'https://example.invalid/about'}],
        }
        conn.execute(
            """INSERT INTO agent_prospect_profiles
               (legacy_user_id, source, source_id, customer_id, research_json, contact_permission)
               VALUES (?,?,?,?,?,'allowed')""",
            (args.user, 'sela', f'context-export-{customer_id}', customer_id,
             json.dumps(research, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()
        db.set_db_user(None)
    print(json.dumps({'customer_id': customer_id, 'company': COMPANY}, ensure_ascii=False))


if __name__ == '__main__':
    main()
