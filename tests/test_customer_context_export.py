"""The single-customer 「导出全部沟通」 copy path.

Pins what a person actually gets on their clipboard: every stored
communication (no silent 50-row cut), stored email bodies kept apart from
replies, open tasks separated from historical next-step notes, manual notes
separated from machine research, and no invented stage or recommendation.

The sample is deliberately de-identified: it keeps the shape of a real
quotation/drawing/sample conversation without using any real customer's text.
"""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db  # noqa: E402


class CustomerContextExportTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        self.original_demo = os.environ.get('CRM_SEED_DEMO_DATA')
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.init_all_dbs()
        spec = importlib.util.spec_from_file_location('crm_app_context_export_test', ROOT / 'app.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.today = self.module._calendar_today()
        self.client = self.module.app.test_client()
        self.assertEqual(self.client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)

    def tearDown(self):
        db.cancel_safety_backup()
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    # -- fixtures ---------------------------------------------------------
    def _connection(self):
        return sqlite3.connect(db.get_user_db_path('hamid'))

    def _seed_customer(self, *, with_tasks=True, with_research=True):
        conn = self._connection()
        try:
            cursor = conn.execute(
                """INSERT INTO customers (name, company, country, profile, notes, customer_judgment, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                ('北美采购联系人', '北美亚克力客户（脱敏样本）', '美国',
                 '亚克力板与定制膜采购', '客户要求报价含包装，并指定到港前提供装箱图。',
                 '等客户确认寄样地址', self._day(120), self._day(120)))
            customer_id = cursor.lastrowid
            # 60 records over 20 days: three per day, so same-day ordering is covered.
            for index in range(60):
                day = (self.today - timedelta(days=index % 20)).isoformat()
                conn.execute(
                    """INSERT INTO follow_up_logs
                       (customer_id, content, follow_date, activity_type, direction, created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (customer_id, f'历史沟通 [G{index + 1:03d}]：讨论报价与图纸', day,
                     'whatsapp', 'two_way', f'{day} 00:{index:02d}:00'))
            sample = (
                ('2026-11-18', 'inbound', '请按图纸 B-204 报价，原弯管方案可以吗', None),
                ('2026-11-19', 'outbound', '图纸已收到；原弯管方案做不了，需要改直管或另开模具', None),
                ('2026-11-10', 'inbound', '客户说可以的话寄样，先看板子质量', None),
                ('2026-11-05', 'inbound', '旧记录：客户说下月再谈', '2026-12-30 再问一次'),
            )
            for day, direction, content, next_plan in sample:
                conn.execute(
                    """INSERT INTO follow_up_logs
                       (customer_id, content, follow_date, activity_type, direction, next_plan, created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (customer_id, content, day, 'email', direction, next_plan or '', day + ' 09:00:00'))
            emails = (
                ('Acrylic quotation - 3mm', '附上 3mm 透明亚克力板报价：USD 12.5/张，含包装。',
                 '2026-11-01', 'replied', '可以的话寄样', '2026-11-04'),
                ('Follow up - drawings', '', '2026-11-06', 'pending', '', ''),
                ('Sample policy', '我们的样品政策是首次寄样免费，运费由客户承担。',
                 '2026-11-08', 'bounced', '550 5.1.1 user unknown', '2026-11-08'),
            )
            for subject, body, sent_date, status, reply, reply_date in emails:
                conn.execute(
                    """INSERT INTO outreach_emails
                       (customer_id, subject, content, sent_date, reply_status, reply_content, reply_date, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (customer_id, subject, body, sent_date, status, reply, reply_date,
                     sent_date + ' 08:00:00'))
            if with_tasks:
                due = (self.today + timedelta(days=8)).isoformat()
                conn.execute(
                    """INSERT INTO reminders (customer_id, title, content, remind_date, is_done, reminder_type, created_at)
                       VALUES (?,?,?,?,0,'follow_up',?)""",
                    (customer_id, '寄样并确认运费', '先确认收件地址，再报运费', due, self._day(1)))
            if with_research:
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
                       VALUES ('hamid','sela',?,?,?,'allowed')""",
                    (f'context-export-{customer_id}', customer_id, json.dumps(research, ensure_ascii=False)))
            conn.commit()
            return customer_id
        finally:
            conn.close()

    def _day(self, offset):
        return (self.today - timedelta(days=offset)).isoformat()

    def _context(self, customer_id, mode='timeline'):
        response = self.client.get(f'/api/customers/{customer_id}/context?mode={mode}')
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload['mode'], mode)
        return payload['content']

    @staticmethod
    def _section(content, title):
        marker = '## ' + title
        start = content.index(marker)
        rest = content[start:]
        following = rest.find('\n## ', len(marker))
        return rest if following < 0 else rest[:following]

    # -- tests ------------------------------------------------------------
    def test_requires_login(self):
        anonymous = self.module.app.test_client()
        self.assertEqual(anonymous.get('/api/customers/1/context?mode=timeline').status_code, 401)

    def test_timeline_copies_every_record_instead_of_stopping_at_fifty(self):
        customer_id = self._seed_customer()
        content = self._context(customer_id)
        for marker in ('[G001]', '[G030]', '[G060]'):
            self.assertIn(f'历史沟通 {marker}', content)
        self.assertEqual(content.count('历史沟通 [G'), 60)
        # 60 generated records + 4 sample records + 3 outbound mails + 1 stored reply.
        self.assertIn('## 沟通时间线（共 68 条，从新到旧）', content)

    def test_same_day_records_keep_the_saved_newest_first_order(self):
        customer_id = self._seed_customer()
        content = self._section(self._context(customer_id), '沟通时间线')
        self.assertLess(content.index('历史沟通 [G041]'), content.index('历史沟通 [G021]'))
        self.assertLess(content.index('历史沟通 [G021]'), content.index('历史沟通 [G001]'))

    def test_email_body_and_reply_are_kept_and_told_apart(self):
        customer_id = self._seed_customer()
        content = self._context(customer_id)
        self.assertIn('主题：Acrylic quotation - 3mm', content)
        self.assertIn('正文：附上 3mm 透明亚克力板报价：USD 12.5/张，含包装。', content)
        self.assertIn('· 客户回信 · 客户发来', content)
        self.assertIn('正文：可以的话寄样', content)
        # A subject-only row must say so instead of letting the subject look like the body.
        self.assertIn('主题：Follow up - drawings', content)
        self.assertIn('正文：Trosa 未保存正文，只保存了主题。', content)
        # A bounce is a delivery fact, never a customer reply.
        self.assertIn('送达：退信', content)
        self.assertNotIn('正文：550 5.1.1 user unknown', content)

    def test_current_tasks_do_not_borrow_a_historical_next_step(self):
        customer_id = self._seed_customer()
        content = self._context(customer_id)
        todo = self._section(content, '当前未完成待办')
        self.assertIn('寄样并确认运费', todo)
        self.assertIn('（先确认收件地址，再报运费）', todo)
        self.assertNotIn('再问一次', todo)
        timeline = self._section(content, '沟通时间线')
        self.assertIn('记录时写的下一步：2026-12-30 再问一次', timeline)

    def test_manual_notes_and_automatic_research_are_separate(self):
        customer_id = self._seed_customer()
        content = self._context(customer_id)
        notes = self._section(content, '人工备注')
        research = self._section(content, '自动背调')
        self.assertIn('客户要求报价含包装', notes)
        self.assertIn('当前等待：等客户确认寄样地址', notes)
        self.assertNotIn('qualified', notes)
        self.assertIn('机器抓取或生成，未经人工核实', research)
        self.assertIn('qualified', research)
        self.assertIn('https://example.invalid/about', research)
        for invented in ('## 当前阶段', '## 评分', '## 跟进建议'):
            self.assertNotIn(invented, content)

    def test_the_deidentified_sample_keeps_its_own_words(self):
        customer_id = self._seed_customer()
        content = self._context(customer_id)
        self.assertIn('请按图纸 B-204 报价', content)
        self.assertIn('原弯管方案做不了', content)
        self.assertIn('可以的话寄样', content)
        self.assertNotIn('已寄样', content)

    def test_empty_customer_states_are_explicit(self):
        conn = self._connection()
        try:
            cursor = conn.execute("INSERT INTO customers (name, company) VALUES ('空客户', '暂无记录公司')")
            customer_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()
        content = self._context(customer_id)
        self.assertIn('- 暂无人工备注', content)
        self.assertIn('- 暂无未完成待办', content)
        self.assertIn('- 暂无自动背调资料', content)
        self.assertIn('- 暂无沟通记录', content)

    def test_other_export_modes_keep_their_existing_shape(self):
        customer_id = self._seed_customer()
        compact = self._context(customer_id, 'compact')
        self.assertIn('## 最近状态', compact)
        self.assertNotIn('## 沟通时间线', compact)
        full = self._context(customer_id, 'full')
        self.assertIn('## 已记录需求与备注', full)
        self.assertIn('## 沟通时间线', full)
        # 完整导出仍沿用原来的 50 条展示上限；这次只改「导出全部沟通」。
        self.assertLess(full.count('历史沟通 [G'), 60)


if __name__ == '__main__':
    unittest.main()
