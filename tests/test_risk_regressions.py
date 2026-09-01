import importlib.util
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db
import scheduler
from ical_gen import build_icalendar


class IsolatedDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        self.original_demo = os.environ.get('CRM_SEED_DEMO_DATA')
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)

    def tearDown(self):
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    def test_first_initialization_does_not_seed_demo_data(self):
        db.init_all_dbs()
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0], 0)
        finally:
            conn.close()

    def test_demo_data_requires_explicit_switch(self):
        db.init_all_dbs()
        os.environ['CRM_SEED_DEMO_DATA'] = 'true'
        db.seed_demo_data_for_user('hamid')
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertGreater(conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0], 0)
        finally:
            conn.close()

    def test_restore_validates_snapshot_and_removes_stale_wal(self):
        db.ensure_db_dir()
        system_path = os.path.join(db.DB_DIR, 'system.db')
        conn = sqlite3.connect(system_path)
        conn.execute('CREATE TABLE value_store (value TEXT)')
        conn.execute("INSERT INTO value_store VALUES ('before')")
        conn.commit()
        conn.close()
        snapshot = db.backup_database('test')
        relative_snapshot = os.path.relpath(snapshot['path'], os.path.join(db.DB_DIR, 'backups')).replace('\\', '/')
        self.assertIn(relative_snapshot, [item['path'] for item in db.list_backups()])
        conn = sqlite3.connect(system_path)
        conn.execute("UPDATE value_store SET value='after'")
        conn.commit()
        conn.close()
        Path(system_path + '-wal').write_bytes(b'stale')
        result = db.restore_from_backup(relative_snapshot)
        self.assertTrue(result['success'], result)
        self.assertFalse(os.path.exists(system_path + '-wal'))
        conn = sqlite3.connect(system_path)
        try:
            self.assertEqual(conn.execute('SELECT value FROM value_store').fetchone()[0], 'before')
        finally:
            conn.close()

    def test_restore_rejects_path_traversal_and_checksum_mismatch(self):
        self.assertFalse(db.restore_from_backup('../outside')['success'])
        db.ensure_db_dir()
        system_path = os.path.join(db.DB_DIR, 'system.db')
        sqlite3.connect(system_path).close()
        snapshot = db.backup_database('test')
        snapshot_db = os.path.join(snapshot['path'], 'system.db')
        with open(snapshot_db, 'ab') as handle:
            handle.write(b'corrupt')
        relative_snapshot = os.path.relpath(snapshot['path'], os.path.join(db.DB_DIR, 'backups')).replace('\\', '/')
        result = db.restore_from_backup(relative_snapshot)
        self.assertFalse(result['success'])
        self.assertIn('checksum', result['error'].lower())

    def test_scheduled_local_backup_is_a_verified_recovery_point(self):
        db.init_all_dbs()
        result = db.run_scheduled_local_backup()
        self.assertEqual(result['failed'], [])
        relative_snapshot = os.path.relpath(result['path'], os.path.join(db.DB_DIR, 'backups')).replace('\\', '/')
        listed = next(item for item in db.list_backups() if item['path'] == relative_snapshot)
        self.assertEqual(listed['reason'], 'scheduled_local')
        self.assertTrue(listed['created_at'])
        self.assertEqual(set(listed['files']), {'system.db', 'hamid.db', 'amy.db', 'kelley.db'})

    def test_scheduler_registers_daily_local_backup_job(self):
        fake_scheduler = mock.Mock()
        fake_scheduler.running = False
        with mock.patch.object(scheduler, 'BackgroundScheduler', return_value=fake_scheduler):
            scheduler.scheduler = None
            scheduler.start_scheduler()
        job_ids = [call.kwargs['id'] for call in fake_scheduler.add_job.call_args_list]
        self.assertIn('local_backup_daily', job_ids)
        local_call = next(call for call in fake_scheduler.add_job.call_args_list
                          if call.kwargs['id'] == 'local_backup_daily')
        self.assertEqual(local_call.kwargs['misfire_grace_time'], 7 * 24 * 60 * 60)
        scheduler.scheduler = None


class CalendarAndAccessTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        self.original_demo = os.environ.get('CRM_SEED_DEMO_DATA')
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.init_all_dbs()

    def tearDown(self):
        db.DB_DIR = self.original_db_dir
        if self.original_demo is None:
            os.environ.pop('CRM_SEED_DEMO_DATA', None)
        else:
            os.environ['CRM_SEED_DEMO_DATA'] = self.original_demo
        self.tempdir.cleanup()

    def test_shanghai_timestamp_converts_to_utc_without_changing_all_day_date(self):
        feed = build_icalendar([{
            'id': 7, 'source': 'reminder', 'customer_name': '客户', 'title': '跟进',
            'remind_date': '2026-07-27', 'created_at': '2026-07-27 08:00:00',
        }], owner_id='hamid', last_modified='2026-07-27 08:00:00')
        self.assertIn('LAST-MODIFIED:20260727T000000Z', feed)
        self.assertIn('DTSTART;VALUE=DATE:20260727', feed)
        self.assertIn('DTEND;VALUE=DATE:20260728', feed)

    def test_scheduler_uses_shanghai_time(self):
        self.assertEqual(str(scheduler.SCHEDULER_TIMEZONE), 'Asia/Shanghai')

    def test_overview_routes_require_login(self):
        spec = importlib.util.spec_from_file_location('crm_app_for_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        client = module.app.test_client()
        self.assertEqual(client.get('/api/overview/stats').status_code, 401)
        self.assertEqual(client.get('/api/weekly-summary').status_code, 401)
        self.assertEqual(client.get('/api/customers').status_code, 401)

    def test_internal_lan_viewer_is_read_only_and_public_requires_login(self):
        previous_cidrs = os.environ.get('CRM_INTERNAL_VIEWER_CIDRS')
        os.environ['CRM_INTERNAL_VIEWER_CIDRS'] = '192.168.0.0/23'
        try:
            spec = importlib.util.spec_from_file_location('crm_app_internal_viewer_test', ROOT / 'app.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # The public Cloudflare/Tunnel peer remains a normal unauthenticated
            # request. The old public IP share exception must not survive.
            public = module.app.test_client()
            public_entry = public.get('/share/weekly', headers={'CF-Connecting-IP': '122.233.82.41'})
            self.assertEqual(public_entry.status_code, 302)
            self.assertNotIn('weekly=1', public_entry.headers.get('Location', ''))
            public_auth = public.get('/api/auth/me', headers={'CF-Connecting-IP': '122.233.82.41'})
            self.assertFalse(public_auth.get_json()['internal_viewer'])
            self.assertFalse(public_auth.get_json()['weekly_viewer'])
            self.assertEqual(public.get('/api/overview/stats').status_code, 401)

            # A direct office-LAN peer receives the same narrow read-only view.
            lan = module.app.test_client()
            lan_base = {'REMOTE_ADDR': '192.168.0.58'}
            entry = lan.get('/share/weekly', environ_base=lan_base)
            self.assertEqual(entry.status_code, 302)
            self.assertIn('weekly=1', entry.headers.get('Location', ''))
            auth = lan.get('/api/auth/me', environ_base=lan_base)
            self.assertEqual(auth.status_code, 200)
            self.assertTrue(auth.get_json()['internal_viewer'])
            self.assertFalse(auth.get_json()['weekly_viewer'])
            self.assertFalse(auth.get_json()['logged_in'])

            self.assertEqual(lan.get('/api/overview/stats', environ_base=lan_base).status_code, 200)
            self.assertEqual(lan.get('/api/weekly-summary', environ_base=lan_base).status_code, 200)
            self.assertEqual(lan.get('/api/customers?page=1', environ_base=lan_base).status_code, 401)
            self.assertEqual(lan.post('/api/customers', environ_base=lan_base, json={}).status_code, 401)

            # Host/header spoofing and leaving the LAN do not preserve access.
            outsider_base = {'REMOTE_ADDR': '8.8.8.8', 'HTTP_HOST': '192.168.0.58:8080'}
            self.assertEqual(lan.get('/api/overview/stats', environ_base=outsider_base).status_code, 401)
            self.assertEqual(lan.get('/share/weekly', environ_base=outsider_base).status_code, 302)
        finally:
            if previous_cidrs is None:
                os.environ.pop('CRM_INTERNAL_VIEWER_CIDRS', None)
            else:
                os.environ['CRM_INTERNAL_VIEWER_CIDRS'] = previous_cidrs

    def test_mac_weekly_gateway_token_is_loopback_only_and_read_only(self):
        token = 'test-weekly-gateway-token-with-sufficient-entropy'
        previous_digest = os.environ.get('CRM_WEEKLY_GATEWAY_TOKEN_SHA256')
        os.environ['CRM_WEEKLY_GATEWAY_TOKEN_SHA256'] = hashlib.sha256(token.encode('utf-8')).hexdigest()
        try:
            spec = importlib.util.spec_from_file_location('crm_app_weekly_gateway_test', ROOT / 'app.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            client = module.app.test_client()
            header = {'X-TradeOS-Weekly-Gateway': token}

            entry = client.get('/share/weekly', headers=header, environ_base={'REMOTE_ADDR': '127.0.0.1'})
            self.assertEqual(entry.status_code, 302)
            self.assertIn('weekly=1', entry.headers.get('Location', ''))
            auth = client.get('/api/auth/me', headers=header, environ_base={'REMOTE_ADDR': '127.0.0.1'})
            self.assertTrue(auth.get_json()['weekly_viewer'])
            self.assertFalse(auth.get_json()['logged_in'])
            self.assertEqual(client.get(
                '/api/weekly-summary', headers=header, environ_base={'REMOTE_ADDR': '127.0.0.1'}
            ).status_code, 200)
            self.assertEqual(client.post(
                '/api/customers', headers=header, environ_base={'REMOTE_ADDR': '127.0.0.1'}, json={}
            ).status_code, 401)

            external = client.get('/share/weekly', headers=header, environ_base={'REMOTE_ADDR': '8.8.8.8'})
            self.assertNotIn('weekly=1', external.headers.get('Location', ''))
            wrong = client.get(
                '/api/weekly-summary',
                headers={'X-TradeOS-Weekly-Gateway': 'wrong-token'},
                environ_base={'REMOTE_ADDR': '127.0.0.1'},
            )
            self.assertEqual(wrong.status_code, 401)
        finally:
            if previous_digest is None:
                os.environ.pop('CRM_WEEKLY_GATEWAY_TOKEN_SHA256', None)
            else:
                os.environ['CRM_WEEKLY_GATEWAY_TOKEN_SHA256'] = previous_digest

    def test_prospecting_integration_token_is_hashed_and_least_privilege(self):
        spec = importlib.util.spec_from_file_location('crm_app_integration_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        issued = client.post('/api/integrations/prospecting-lab/token')
        self.assertEqual(issued.status_code, 200, issued.get_json())
        token = issued.get_json()['token']
        conn = db.get_system_db()
        try:
            stored = conn.execute('SELECT value FROM app_settings WHERE key=?',
                                  (module._PROSPECTING_INTEGRATION_KEY,)).fetchone()['value']
        finally:
            conn.close()
        self.assertNotIn(token, stored)
        anonymous = module.app.test_client()
        headers = {'Authorization': 'Bearer ' + token}
        self.assertEqual(anonymous.get('/api/customers?page=1&per_page=10', headers=headers).status_code, 200)
        self.assertEqual(anonymous.get('/api/backup/list', headers=headers).status_code, 401)
        self.assertEqual(anonymous.delete('/api/customers/1', headers=headers).status_code, 401)
        self.assertEqual(anonymous.get('/api/customers?page=1', headers={'Authorization': 'Bearer wrong'}).status_code, 401)

    def test_weekly_customer_detail_is_allowlisted_and_paginated(self):
        spec = importlib.util.spec_from_file_location('crm_app_weekly_detail_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('amy'))
        try:
            conn.execute("""INSERT INTO customers
                         (name, company, country, website, field, type, customer_type,
                          profile, notes, system_notes, level, status, created_at)
                         VALUES ('阅读客户', '阅读公司', '中国', 'example.com', '包装', '终端',
                                 'existing', '不应返回的总结', '不应返回的备注', '系统字段', 'A', '成交', '2026-08-04')""")
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("""INSERT INTO contacts (customer_id, name, email, phone)
                         VALUES (?, '联系人隐私', 'private@example.com', '13800000000')""", (customer_id,))
            for day in ('2026-08-03', '2026-08-02'):
                conn.execute("""INSERT INTO follow_up_logs
                             (customer_id, content, follow_date, result, next_plan, is_reported)
                             VALUES (?, '实际记录', ?, '已记录结果', '发送确认', 1)""", (customer_id, day))
            conn.execute("""INSERT INTO reminders (customer_id, title, reason, remind_date)
                         VALUES (?, '确认交期', '等待客户确认', '2026-08-10')""", (customer_id,))
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        response = client.get('/api/overview/customers/amy/%s?week_start=2026-08-03&timeline_per_page=1' % customer_id)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload['recent_timeline']), 1)
        self.assertTrue(payload['timeline_pagination']['has_next'])
        self.assertEqual(payload['customer']['owner'], 'amy')
        serialized = str(payload)
        for forbidden in ('contacts', 'private@example.com', 'research_reports', '不应返回的总结', '系统字段'):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn('profile', payload['customer'])
        self.assertNotIn('notes', payload['customer'])
        summary = client.get('/api/weekly-summary?week_start=2026-08-03').get_json()
        summary_text = str(summary)
        self.assertNotIn('customer_profile', summary_text)
        self.assertNotIn('customer_level', summary_text)
        self.assertNotIn('stats', summary_text)

    def test_weekly_summary_filters_reported_rows_and_returns_customer_facts_only(self):
        spec = importlib.util.spec_from_file_location('crm_app_weekly_summary_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("""INSERT INTO customers
                         (name, company, country, customer_type, created_at)
                         VALUES ('摘要客户', '摘要公司', '德国', 'existing', '2026-08-04')""")
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("""INSERT INTO follow_up_logs
                         (customer_id, content, follow_date, result, next_plan, is_reported)
                         VALUES (?, '<mark class="hl-yellow">已上报工作</mark>', '2026-08-04', '<mark class="hl-green">已上报结果</mark>', '<mark class="hl-pink">确认交期</mark>', 1)""", (customer_id,))
            conn.execute("""INSERT INTO follow_up_logs
                         (customer_id, content, follow_date, result, is_reported)
                         VALUES (?, '未上报工作', '2026-08-05', '不应出现在周报', 0)""", (customer_id,))
            conn.execute("""INSERT INTO outreach_emails
                         (customer_id, subject, content, sent_date, reply_content, is_reported)
                         VALUES (?, '已上报邮件', '已发送邮件', '2026-08-05', '客户已回复', 1)""", (customer_id,))
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        response = client.get('/api/weekly-summary/hamid?week_start=2026-08-03')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload['reported_customers']), 1)
        summary = payload['reported_customers'][0]
        self.assertEqual(summary['activity_count'], 2)
        self.assertIn('已上报工作', summary['actual_work'])
        self.assertIn('<mark class="hl-yellow">已上报工作</mark>', summary['actual_work'])
        self.assertIn('<mark class="hl-green">已上报结果</mark>', summary['result'])
        self.assertEqual(summary['next_step'], '<mark class="hl-pink">确认交期</mark>')
        self.assertNotIn('未上报工作', summary['actual_work'])
        self.assertEqual(summary['customer_country'], '德国')
        self.assertNotIn('week_entries', summary)
        self.assertNotIn('不应出现在周报', str(payload))

    def test_workspace_summary_stays_compact_and_contact_write_returns_contact(self):
        spec = importlib.util.spec_from_file_location('crm_app_workspace_performance_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company, created_at, updated_at) VALUES (?, ?, date('now'), date('now'))",
                         ('轻量客户', 'Compact Workspace Co.'))
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            for day in ('2026-08-03', '2026-08-02'):
                conn.execute("""INSERT INTO reminders (customer_id, title, remind_date, is_done)
                             VALUES (?, ?, ?, 0)""", (customer_id, '安排下一步', day))
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        summary = client.get(f'/api/customers/{customer_id}/summary')
        self.assertEqual(summary.status_code, 200, summary.get_json())
        payload = summary.get_json()
        self.assertIn('next_task', payload)
        self.assertNotIn('reminders', payload)
        self.assertNotIn('automatic_reminders', payload)

        contact = client.post(f'/api/customers/{customer_id}/contacts', json={
            'name': 'Workspace Contact', 'email': 'workspace-contact@example.com',
        })
        self.assertEqual(contact.status_code, 201, contact.get_json())
        self.assertEqual(contact.get_json()['contact']['email'], 'workspace-contact@example.com')

        preferences = client.put('/api/preferences', json={
            'interface_performance': 'performance',
            'performance_probe': {'version': 1, 'frame_count': 42, 'slow_frames': 7,
                                  'slow_ratio': .167, 'long_tasks': 1, 'longest_frame': 96,
                                  'sampled_at': '2026-08-09T12:00:00.000Z', 'slow': True},
        })
        self.assertEqual(preferences.status_code, 200, preferences.get_json())
        self.assertEqual(preferences.get_json()['preferences']['interface_performance'], 'performance')
        self.assertTrue(preferences.get_json()['preferences']['performance_probe']['slow'])

        compact_weekly = module._page_weekly_summary({
            'user_id': 'hamid',
            'reported_customers': [{'customer_id': index} for index in range(12)],
        }, 10, 0)
        self.assertEqual(len(compact_weekly['reported_customers']), 10)
        self.assertEqual(compact_weekly['reported_customer_count'], 12)
        self.assertTrue(compact_weekly['reported_customer_pagination']['has_next'])

    def test_customer_facts_summary_returns_recent_facts_status_and_gaps(self):
        spec = importlib.util.spec_from_file_location('crm_app_customer_facts_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("""INSERT INTO customers
                         (name, company, country, created_at, updated_at, attention_state, attention_reason)
                         VALUES ('事实客户', '重复公司', '', date('now'), date('now'), 'waiting_reply', '等待客户确认规格')""")
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("INSERT INTO customers (name, company) VALUES ('重复记录', '重复公司')")
            for index, day in enumerate(('2026-08-10', '2026-08-09', '2026-08-08', '2026-08-07')):
                conn.execute("""INSERT INTO follow_up_logs
                             (customer_id, content, follow_date, result, activity_type)
                             VALUES (?, ?, ?, ?, 'email')""",
                             (customer_id, '已发生事实 %d' % index, day, '结果 %d' % index))
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        response = client.get('/api/customers/%s/summary' % customer_id)
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(len(payload['recent_facts']), 3)
        self.assertEqual(payload['recent_facts'][0]['source'], '沟通记录')
        self.assertEqual(payload['current_status']['label'], '等待客户确认规格')
        self.assertEqual(payload['current_status']['source'], '用户记录')
        gap_codes = {gap['code'] for gap in payload['information_gaps']}
        self.assertTrue({'missing_contact', 'missing_industry', 'missing_website', 'missing_profile', 'missing_country', 'possible_duplicate'} <= gap_codes)
        self.assertIn('缺少联系人', payload['data_quality_issues'])

        customers = client.get('/api/customers?page=1&per_page=100').get_json()['customers']
        listed = next(item for item in customers if item['id'] == customer_id)
        self.assertIn('缺少联系人', listed['data_quality_issues'])
        self.assertIn('疑似重复资料', listed['data_quality_issues'])

    def test_customer_timeline_returns_weekly_report_state_for_each_record_type(self):
        """The customer workspace needs the same weekly state that the weekly view uses."""
        spec = importlib.util.spec_from_file_location('crm_app_timeline_report_state_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company) VALUES ('星标客户', 'Starred Customer Co.')")
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("""INSERT INTO follow_up_logs
                         (customer_id, content, follow_date, is_reported)
                         VALUES (?, '已加入的沟通', '2026-08-09', 1)""", (customer_id,))
            conn.execute("""INSERT INTO outreach_emails
                         (customer_id, subject, sent_date, is_reported)
                         VALUES (?, '未加入的开发信', '2026-08-08', 0)""", (customer_id,))
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        response = client.get(f'/api/customers/{customer_id}/timeline?per_page=20')
        self.assertEqual(response.status_code, 200, response.get_json())
        items = response.get_json()['items']
        states = {(item['type'], item['id']): item['is_reported'] for item in items}
        self.assertTrue(states[('follow', 1)])
        self.assertFalse(states[('outreach', 1)])

    def test_development_nodes_are_separate_and_categorized(self):
        """15/30/60-day nodes stay available without polluting human Today."""
        spec = importlib.util.spec_from_file_location('crm_app_development_nodes_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        today = module._calendar_today().isoformat()
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company, customer_type) VALUES ('开发节点客户', '开发节点公司', 'new')")
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("INSERT INTO reminders (customer_id, title, remind_date, reminder_type) VALUES (?, '自动开发', ?, 'outreach_15天')", (customer_id, today))
            conn.execute("INSERT INTO reminders (customer_id, title, remind_date, reminder_type) VALUES (?, '人工跟进', ?, 'follow_up')", (customer_id, today))
            conn.commit()
        finally:
            conn.close()
        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        development = client.get('/api/reminders/development').get_json()
        self.assertEqual(len(development), 1)
        self.assertTrue(development[0]['is_automatic_development'])
        self.assertEqual(development[0]['reminder_category_label'], '自动开发节点')
        today_items = client.get('/api/reminders/today').get_json()
        self.assertEqual([item['reminder_type'] for item in today_items], ['follow_up'])

    def test_today_frontend_keeps_customer_focus_without_development_section(self):
        """Today stays focused on explicit follow-ups and the selected customer."""
        html = (ROOT / 'app' / 'static' / 'index.html').read_text(encoding='utf-8')
        javascript = (ROOT / 'app' / 'static' / 'app.js').read_text(encoding='utf-8')
        self.assertIn('id="todayFocus"', html)
        self.assertIn('id="todayWideDetail"', html)
        self.assertNotIn('id="todayDevelopmentSection"', html)
        self.assertNotIn('id="statDevelopment"', html)
        self.assertNotIn("api('/api/reminders/development')", javascript)

    def test_frozen_customer_intelligence_is_not_a_user_module(self):
        spec = importlib.util.spec_from_file_location('crm_app_frozen_modules_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        preferences = client.get('/api/preferences').get_json()['modules']
        self.assertNotIn('ai_research', preferences)
        self.assertNotIn('website_monitor', preferences)
        self.assertIn('email_validation', preferences)
        self.assertIn('weekly_overview', preferences)

    def test_production_pin_setup_and_login_round_trip(self):
        env_keys = ('CRM_ENV', 'CRM_SESSION_SECRET')
        original_env = {key: os.environ.get(key) for key in env_keys}
        try:
            os.environ['CRM_ENV'] = 'production'
            os.environ['CRM_SESSION_SECRET'] = 'test-session-secret-' + ('x' * 40)
            spec = importlib.util.spec_from_file_location('crm_app_production_auth_test', ROOT / 'app.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            client = module.app.test_client()

            setup = client.post('/api/auth/setup-pin', json={'user': 'hamid', 'pin': '654321'})
            self.assertEqual(setup.status_code, 200, setup.get_json())
            client.post('/api/auth/logout')

            login = client.post('/api/auth/login', json={'user': 'hamid', 'pin': '654321'})
            self.assertEqual(login.status_code, 200, login.get_json())
            wrong = client.post('/api/auth/login', json={'user': 'hamid', 'pin': '123456'})
            self.assertEqual(wrong.status_code, 400, wrong.get_json())
        finally:
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_production_cors_allows_only_explicit_origins(self):
        env_keys = ('CRM_ENV', 'CRM_SESSION_SECRET', 'CRM_PUBLIC_URL', 'CRM_CORS_ORIGINS')
        original_env = {key: os.environ.get(key) for key in env_keys}
        try:
            os.environ['CRM_ENV'] = 'production'
            os.environ['CRM_SESSION_SECRET'] = 'test-session-secret-' + ('x' * 40)
            os.environ['CRM_PUBLIC_URL'] = 'https://app.trosa.space'
            os.environ['CRM_CORS_ORIGINS'] = 'chrome-extension://test-extension'
            spec = importlib.util.spec_from_file_location('crm_app_production_cors_test', ROOT / 'app.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            client = module.app.test_client()

            trusted = client.get('/api/network/ping', headers={'Origin': 'https://app.trosa.space'})
            self.assertEqual(trusted.headers.get('Access-Control-Allow-Origin'), 'https://app.trosa.space')
            self.assertEqual(trusted.headers.get('Access-Control-Allow-Credentials'), 'true')

            extension = client.get('/api/network/ping', headers={'Origin': 'chrome-extension://test-extension'})
            self.assertEqual(extension.headers.get('Access-Control-Allow-Origin'), 'chrome-extension://test-extension')

            untrusted = client.get('/api/network/ping', headers={'Origin': 'https://untrusted.example'})
            self.assertIsNone(untrusted.headers.get('Access-Control-Allow-Origin'))
        finally:
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_summary_direction_inference_covers_common_crm_notes(self):
        spec = importlib.util.spec_from_file_location('crm_app_direction_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        infer = module._infer_communication_direction
        aliases = (['hamid'], ['Jimmy'])

        self.assertEqual(infer('客户回复上次报价 very good，并发送了 PO 准备下单', *aliases)[0], 'inbound')
        self.assertEqual(infer('向客户提供了两种尺寸选择，并询问哪种更适合其市场', *aliases)[0], 'outbound')
        self.assertEqual(infer('提供了 FOB 上海报价，并询问是否需要检查 DDP 价格', *aliases)[0], 'outbound')
        self.assertEqual(
            infer('客户表示需要分阶段付款，我方回复首单可接受该方案', *aliases)[0],
            'two_way',
        )
        self.assertEqual(infer('还需要 3D 图纸', *aliases)[0], 'unknown')

    def test_customer_views_require_a_customer_reply_for_contact(self):
        """Sent development emails and outbound logs stay outside “已有联系”."""
        spec = importlib.util.spec_from_file_location('crm_app_contact_view_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, created_at, updated_at) VALUES (?, date('now'), date('now'))",
                         ('仅开发信',))
            outbound_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("INSERT INTO customers (name, created_at, updated_at) VALUES (?, date('now'), date('now'))",
                         ('客户已回复',))
            replied_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("""INSERT INTO outreach_emails
                         (customer_id, subject, sent_date, reply_status, created_at)
                         VALUES (?, '开发信', date('now'), 'pending', datetime('now'))""", (outbound_id,))
            conn.execute("""INSERT INTO follow_up_logs
                         (customer_id, content, follow_date, activity_type, direction, created_at)
                         VALUES (?, '客户回复了询盘', date('now'), 'customer_reply', 'inbound', datetime('now'))""",
                         (replied_id,))
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        communicated = client.get('/api/customers?view=communicated').get_json()['customers']
        uncontacted = client.get('/api/customers?view=uncontacted').get_json()['customers']
        communicated_ids = {customer['id'] for customer in communicated}
        uncontacted_ids = {customer['id'] for customer in uncontacted}

        self.assertIn(replied_id, communicated_ids)
        self.assertNotIn(outbound_id, communicated_ids)
        self.assertIn(outbound_id, uncontacted_ids)

    def test_create_customer_rejects_empty_identity_without_writing(self):
        spec = importlib.util.spec_from_file_location('crm_app_empty_customer_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        before = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            before_count = before.execute('SELECT COUNT(*) FROM customers').fetchone()[0]
        finally:
            before.close()

        response = client.post('/api/customers', json={})
        self.assertEqual(response.status_code, 400, response.get_json())

        after = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            after_count = after.execute('SELECT COUNT(*) FROM customers').fetchone()[0]
        finally:
            after.close()
        self.assertEqual(after_count, before_count)

    def test_customer_create_contact_delete_restore_round_trip(self):
        spec = importlib.util.spec_from_file_location('crm_app_customer_round_trip_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})

        created = client.post('/api/customers', json={
            'name': 'Round Trip Customer',
            'company': 'Round Trip Co.',
            'country': '中国',
            'customer_type': 'existing',
        })
        self.assertEqual(created.status_code, 201, created.get_json())
        customer_id = created.get_json()['id']

        contact = client.post(f'/api/customers/{customer_id}/contacts', json={
            'name': 'Round Trip Contact',
            'email': 'round-trip@example.com',
        })
        self.assertEqual(contact.status_code, 201, contact.get_json())
        self.assertEqual(client.get(f'/api/customers/{customer_id}').status_code, 200)

        self.assertEqual(client.delete(f'/api/customers/{customer_id}').status_code, 200)
        self.assertEqual(client.post(f'/api/customers/{customer_id}/restore').status_code, 200)
        self.assertEqual(client.delete(f'/api/customers/{customer_id}').status_code, 200)
        self.assertEqual(client.delete(f'/api/customers/{customer_id}/permanent').status_code, 200)
        self.assertEqual(client.get(f'/api/customers/{customer_id}').status_code, 404)

    def test_contact_delete_checks_history_and_supports_undo(self):
        spec = importlib.util.spec_from_file_location('crm_app_contact_delete_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company) VALUES ('联系人删除客户', 'Contact Delete Co.')")
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("INSERT INTO contacts (customer_id, name, email, is_primary) VALUES (?, '可删除联系人', 'remove@example.com', 0)", (customer_id,))
            deletable_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("INSERT INTO contacts (customer_id, name, email, is_primary) VALUES (?, '有历史联系人', 'history@example.com', 0)", (customer_id,))
            referenced_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("INSERT INTO follow_up_logs (customer_id, contact_id, content, follow_date) VALUES (?, ?, '历史沟通', '2026-08-05')",
                         (customer_id, referenced_id))
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        with mock.patch.object(module, 'schedule_safety_backup'):
            blocked = client.delete(f'/api/contacts/{referenced_id}')
            self.assertEqual(blocked.status_code, 409, blocked.get_json())
            self.assertEqual(blocked.get_json()['references'][0]['label'], '沟通记录')

            deleted = client.delete(f'/api/contacts/{deletable_id}')
            self.assertEqual(deleted.status_code, 200, deleted.get_json())
            undo_token = deleted.get_json()['undo_token']
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertIsNone(conn.execute('SELECT id FROM contacts WHERE id=?', (deletable_id,)).fetchone())
        finally:
            conn.close()
        with mock.patch.object(module, 'schedule_safety_backup'):
            restored = client.post(f'/api/undo/{undo_token}')
            self.assertEqual(restored.status_code, 200, restored.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute('SELECT email FROM contacts WHERE id=?', (deletable_id,)).fetchone()[0], 'remove@example.com')
        finally:
            conn.close()

    def test_scheduling_uncontacted_inbox_customer_removes_the_signal(self):
        spec = importlib.util.spec_from_file_location('crm_app_inbox_task_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        today = '2026-07-28'
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, customer_type, created_at, updated_at) VALUES (?, 'new', ?, ?)",
                         ('待联系客户', today, today))
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("""INSERT INTO reminders
                         (customer_id, title, remind_date, is_done, reminder_type, created_at)
                         VALUES (?, ?, ?, 0, 'outreach_15', ?)""",
                         (customer_id, '开发提醒', today, today))
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        with mock.patch.object(module, 'datetime') as mocked_datetime:
            mocked_datetime.now.return_value = __import__('datetime').datetime(2026, 7, 28, 10, 0, 0)
            mocked_datetime.strptime = __import__('datetime').datetime.strptime
            before = client.get('/api/inbox').get_json()['items']
            self.assertTrue(any(item['customer_id'] == customer_id and item['item_type'] == 'uncontacted_follow_up'
                                for item in before))
            response = client.post(f'/api/customers/{customer_id}/tasks',
                                   json={'title': '再次联系客户', 'due_date': '2026-08-04'})
            self.assertEqual(response.status_code, 201, response.get_json())
            after = client.get('/api/inbox').get_json()['items']
        self.assertFalse(any(item['customer_id'] == customer_id and item['item_type'] == 'uncontacted_follow_up'
                             for item in after))

    def test_recording_an_inbox_reply_through_follow_history_resolves_only_that_reply(self):
        """The shared communication form must preserve the Inbox item's resolution boundary."""
        spec = importlib.util.spec_from_file_location('crm_app_shared_inbox_record_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company, created_at, updated_at) VALUES (?, ?, date('now'), date('now'))",
                         ('Inbox 联系人', 'Inbox 客户'))
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("""INSERT INTO inbox_items
                         (item_type, customer_id, title, content, dedupe_key, status, created_at)
                         VALUES ('customer_reply', ?, '客户回复待记录', ?, ?, 'open', datetime('now'))""",
                         (customer_id, '客户确认下周再看报价', 'shared-inbox-reply-1'))
            inbox_item_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        response = client.post(f'/api/customers/{customer_id}/follow_history', json={
            'activity_content': '客户确认下周再看报价', 'activity_type': 'customer_reply',
            'direction': 'inbound', 'follow_date': '2026-08-30',
            'inbox_item_id': inbox_item_id, 'source': 'inbox'
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['resolved_inbox_item_id'], inbox_item_id)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute('SELECT status FROM inbox_items WHERE id=?', (inbox_item_id,)).fetchone()[0], 'resolved')
            row = conn.execute('SELECT source, activity_type, direction FROM follow_up_logs WHERE customer_id=?', (customer_id,)).fetchone()
            self.assertEqual(tuple(row), ('inbox', 'customer_reply', 'inbound'))
        finally:
            conn.close()

    def test_inbox_capture_context_and_failed_confirmation_keep_item_open(self):
        """Browser captures retain reliable context and resolve only after confirmation."""
        spec = importlib.util.spec_from_file_location('crm_app_inbox_capture_context_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        capture_payload = {
            'channel': 'whatsapp', 'platform': 'WhatsApp Web',
            'conversation_identity': '待归属买家',
            'source_url': 'https://web.whatsapp.com/example',
            'direction': 'inbound', 'messages': [
                {'time': '2026-08-29 14:20', 'sender': '待归属买家',
                 'direction': 'inbound', 'text': '请确认透明板材交期'}
            ],
        }
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company, created_at, updated_at) VALUES (?, ?, ?, ?)",
                         ('Capture Buyer', 'Capture Context Co.', '2026-08-29', '2026-08-29'))
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("""INSERT INTO contacts (customer_id, name, email, is_primary, created_at)
                         VALUES (?, 'Capture Contact', 'capture-contact@example.com', 1, '2026-08-29')""", (customer_id,))
            conn.execute("""INSERT INTO inbox_items
                         (item_type, customer_id, title, content, dedupe_key, status, created_at)
                         VALUES ('customer_reply', ?, '客户回复', '已确认数量，请给出交期', ?, 'open', '2026-08-29 12:00:00')""",
                         (customer_id, 'capture-context-reply'))
            reply_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("""INSERT INTO inbox_items
                         (item_type, customer_id, title, content, dedupe_key, status, created_at)
                         VALUES ('browser_capture', ?, '待归属沟通：待归属买家', ?, ?, 'open', '2026-08-29 14:21:00')""",
                         (customer_id, json.dumps(capture_payload, ensure_ascii=False), 'capture-context-browser'))
            capture_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        inbox = client.get('/api/inbox')
        self.assertEqual(inbox.status_code, 200, inbox.get_json())
        items = {item['id']: item for item in inbox.get_json()['items'] if item.get('id')}
        self.assertEqual(items[reply_id]['contact_name'], 'Capture Contact')
        self.assertEqual(items[reply_id]['source_label'], 'Inbox 客户回复')
        self.assertEqual(items[capture_id]['capture_content'], '2026-08-29 14:20 · 待归属买家\n请确认透明板材交期')
        self.assertEqual(items[capture_id]['capture_direction'], 'inbound')
        self.assertEqual(items[capture_id]['capture_activity_type'], 'whatsapp')
        self.assertEqual(items[capture_id]['capture_date'], '2026-08-29')
        self.assertEqual(items[capture_id]['source_label'], 'WhatsApp Web')
        self.assertEqual(items[capture_id]['customer_id'], customer_id)
        self.assertEqual(items[capture_id]['contact_id'], 1)
        self.assertEqual(items[capture_id]['contact_name'], 'Capture Contact')

        javascript = (ROOT / 'app' / 'static' / 'app.js').read_text(encoding='utf-8')
        capture_handler = javascript[javascript.index('function recordInboxCapture'):javascript.index('function showInboxRecordUndoToast')]
        self.assertIn("customerId: item.customer_id || ''", capture_handler)
        self.assertIn("contactId: item.contact_id || ''", capture_handler)

        failed = client.post(f'/api/customers/{customer_id}/follow_history', json={
            'activity_content': '这次确认失败，不应写入', 'follow_date': 'not-a-date',
            'inbox_item_id': capture_id, 'source': 'browser_extension',
        })
        self.assertEqual(failed.status_code, 400, failed.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            row = conn.execute('SELECT customer_id, status FROM inbox_items WHERE id=?', (capture_id,)).fetchone()
            self.assertEqual(tuple(row), (customer_id, 'open'))
        finally:
            conn.close()

        confirmed = client.post(f'/api/customers/{customer_id}/follow_history', json={
            'activity_content': '请确认透明板材交期', 'activity_type': 'whatsapp',
            'direction': 'inbound', 'follow_date': '2026-08-29',
            'contact_id': 1, 'inbox_item_id': capture_id, 'source': 'browser_extension',
        })
        self.assertEqual(confirmed.status_code, 200, confirmed.get_json())
        self.assertEqual(confirmed.get_json()['resolved_inbox_item_id'], capture_id)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            row = conn.execute('SELECT customer_id, status FROM inbox_items WHERE id=?', (capture_id,)).fetchone()
            self.assertEqual(tuple(row), (customer_id, 'resolved'))
            activity = conn.execute('''SELECT contact_id, source, activity_type, direction
                                       FROM follow_up_logs WHERE id=?''', (confirmed.get_json()['id'],)).fetchone()
            self.assertEqual(tuple(activity), (1, 'browser_extension', 'whatsapp', 'inbound'))
        finally:
            conn.close()

    def test_customer_search_returns_explainable_inbox_and_communication_context(self):
        """Global search results carry one bounded next action without leaking other customers."""
        spec = importlib.util.spec_from_file_location('crm_app_search_context_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company, created_at, updated_at) VALUES (?, ?, ?, ?)",
                         ('Search Inbox Buyer', 'Search Inbox Co.', '2026-08-29', '2026-08-29'))
            inbox_customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("INSERT INTO contacts (customer_id, name, email, is_primary) VALUES (?, ?, ?, 1)",
                         (inbox_customer_id, 'Search Contact', 'search-contact@example.com'))
            conn.execute("""INSERT INTO inbox_items
                         (item_type, customer_id, title, content, dedupe_key, status, created_at)
                         VALUES ('customer_reply', ?, '需要确认交期', '唯一待确认片段-交期', ?, 'open', '2026-08-29 10:00:00')""",
                         (inbox_customer_id, 'search-context-inbox'))
            inbox_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("INSERT INTO customers (name, company, created_at, updated_at) VALUES (?, ?, ?, ?)",
                         ('Search Timeline Buyer', 'Search Timeline Co.', '2026-08-29', '2026-08-29'))
            communication_customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("""INSERT INTO follow_up_logs
                         (customer_id, content, follow_date, activity_type, direction, contact_id, source, created_at)
                         VALUES (?, '唯一沟通片段-报价', '2026-08-28', 'email', 'inbound', NULL, 'inbox', '2026-08-28 10:00:00')""",
                         (communication_customer_id,))
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        inbox_search = client.get('/api/customers?search=唯一待确认片段&page=1&per_page=30')
        self.assertEqual(inbox_search.status_code, 200, inbox_search.get_json())
        inbox_customer = next(item for item in inbox_search.get_json()['customers'] if item['id'] == inbox_customer_id)
        self.assertEqual(inbox_customer['match_context']['type'], 'inbox')
        self.assertEqual(inbox_customer['match_context']['action'], 'record')
        self.assertEqual(inbox_customer['match_context']['id'], inbox_id)
        self.assertEqual(inbox_customer['match_context']['source'], 'inbox')
        self.assertEqual(inbox_customer['match_context']['date'], '2026-08-29')
        self.assertEqual(inbox_customer['match_context']['contact_name'], 'Search Contact')

        communication_search = client.get('/api/customers?search=唯一沟通片段&page=1&per_page=30')
        self.assertEqual(communication_search.status_code, 200, communication_search.get_json())
        communication_customer = next(item for item in communication_search.get_json()['customers'] if item['id'] == communication_customer_id)
        self.assertEqual(communication_customer['match_context']['type'], 'communication')
        self.assertEqual(communication_customer['match_context']['action'], 'view')
        self.assertEqual(communication_customer['match_context']['activity_type'], 'email')
        self.assertEqual(communication_customer['match_context']['direction'], 'inbound')

    def test_batch_today_follow_up_removes_uncontacted_inbox_signal(self):
        """Clicking "今天跟进" in Inbox must clear the 新客户待跟进 signal immediately.

        Regression: batch_update_next_follow_up only set customers.next_follow_up,
        but the uncontacted_follow_up inbox query ignored that field, so items
        reappeared right after the "操作成功" toast.
        """
        spec = importlib.util.spec_from_file_location('crm_app_inbox_batch_follow_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        today = '2026-07-28'
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, customer_type, created_at, updated_at) VALUES (?, 'new', ?, ?)",
                         ('待跟进新客户', today, today))
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("""INSERT INTO reminders
                         (customer_id, title, remind_date, is_done, reminder_type, created_at)
                         VALUES (?, ?, ?, 0, 'outreach_15', ?)""",
                         (customer_id, '开发提醒', today, today))
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        with mock.patch.object(module, 'datetime') as mocked_datetime:
            mocked_datetime.now.return_value = __import__('datetime').datetime(2026, 7, 28, 10, 0, 0)
            mocked_datetime.strptime = __import__('datetime').datetime.strptime
            before = client.get('/api/inbox').get_json()['items']
            self.assertTrue(
                any(item['customer_id'] == customer_id and item['item_type'] == 'uncontacted_follow_up'
                    for item in before),
                '新客户应先出现在 inbox 的"新客户待跟进"分组中'
            )
            # Simulate clicking "今天跟进" for the whole group.
            response = client.post('/api/customers/batch/next_follow_up',
                                   json={'ids': [customer_id], 'value': today})
            self.assertEqual(response.status_code, 200, response.get_json())
            after = client.get('/api/inbox').get_json()['items']
        self.assertFalse(
            any(item['customer_id'] == customer_id and item['item_type'] == 'uncontacted_follow_up'
                for item in after),
            '"今天跟进"操作后，该客户必须立即从"新客户待跟进"消失'
        )

    def test_agent_brief_workspace_and_confirmed_task_proposal(self):
        spec = importlib.util.spec_from_file_location('crm_app_agent_tools_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company, created_at, updated_at) VALUES (?, ?, date('now'), date('now'))",
                         ('Alice', 'Agent Test Co.'))
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("INSERT INTO reminders (customer_id, title, remind_date, is_done) VALUES (?, ?, date('now'), 0)",
                         (customer_id, '准备客户邮件'))
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        brief = client.get('/api/agent/brief/today').get_json()
        self.assertTrue(any(item['customer_id'] == customer_id for item in brief['due_tasks']))
        self.assertIn('工作简报', client.get('/api/agent/brief/today?format=markdown').get_data(as_text=True))
        workspace = client.get(f'/api/agent/customers/{customer_id}/workspace').get_json()
        self.assertEqual(workspace['customer']['id'], customer_id)
        proposal = client.post('/api/agent/proposals', json={
            'type': 'task', 'customer_id': customer_id,
            'payload': {'title': '确认报价需求', 'due_date': '2026-08-01', 'reason': '客户沟通后续'}
        })
        self.assertEqual(proposal.status_code, 201, proposal.get_json())
        confirmed = client.post(f"/api/agent/proposals/{proposal.get_json()['id']}/confirm")
        self.assertEqual(confirmed.status_code, 200, confirmed.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute("SELECT status FROM agent_proposals WHERE id=?", (proposal.get_json()['id'],)).fetchone()[0], 'confirmed')
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reminders WHERE customer_id=? AND title='确认报价需求'", (customer_id,)).fetchone()[0], 1)
        finally:
            conn.close()

    def test_agent_activity_confirmation_matches_shared_communication_write(self):
        """A confirmed Agent activity must have the same CRM consequences as the shared UI endpoint."""
        spec = importlib.util.spec_from_file_location('crm_app_agent_shared_write_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def seed(user):
            conn = sqlite3.connect(db.get_user_db_path(user))
            try:
                conn.execute("""INSERT INTO customers
                             (name, company, status, customer_type, created_at, updated_at)
                             VALUES (?, ?, '未建联', 'new', '2026-08-28', '2026-08-28')""",
                             ('一致性客户', 'Shared Write Co.'))
                customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                conn.execute("""INSERT INTO reminders
                             (customer_id, title, content, remind_date, is_done, reminder_type, created_at)
                             VALUES (?, '原待办', '原待办', '2026-08-28', 0, 'follow_up', '2026-08-28')""",
                             (customer_id,))
                conn.execute("""INSERT INTO reminders
                             (customer_id, title, content, remind_date, is_done, reminder_type, created_at)
                             VALUES (?, '开发节点', '开发节点', '2026-09-01', 0, 'outreach_15', '2026-08-28')""",
                             (customer_id,))
                conn.execute("""INSERT INTO inbox_items
                             (item_type, customer_id, title, content, dedupe_key, status, created_at)
                             VALUES ('browser_capture', NULL, '待归属沟通', '客户确认样品规格', ?, 'open', '2026-08-29 10:00:00')""",
                             (f'agent-shared-{user}',))
                inbox_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                conn.commit()
                return customer_id, inbox_id
            finally:
                conn.close()

        hamid_customer, hamid_inbox = seed('hamid')
        amy_customer, amy_inbox = seed('amy')
        fields = {
            'activity_content': '客户确认样品规格，9 月中旬上海见。',
            'activity_result': '需要准备样品与会议资料', 'activity_type': 'whatsapp',
            'direction': 'inbound', 'follow_date': '2026-08-29',
            'next_task': '准备上海会面样品', 'next_follow_up': '2026-09-15',
            'source': 'browser_extension',
        }

        ui_client = module.app.test_client()
        ui_client.post('/api/auth/login', json={'user': 'hamid'})
        ui_result = ui_client.post(f'/api/customers/{hamid_customer}/follow_history',
                                   json={**fields, 'inbox_item_id': hamid_inbox})
        self.assertEqual(ui_result.status_code, 200, ui_result.get_json())

        agent_client = module.app.test_client()
        agent_client.post('/api/auth/login', json={'user': 'amy'})
        agent_payload = {
            'content': fields['activity_content'], 'result': fields['activity_result'],
            'activity_type': fields['activity_type'], 'direction': fields['direction'],
            'follow_date': fields['follow_date'], 'next_task': fields['next_task'],
            'next_follow_up': fields['next_follow_up'], 'source': fields['source'],
            'inbox_item_id': amy_inbox,
        }
        proposal = agent_client.post('/api/agent/proposals', json={
            'type': 'activity', 'customer_id': amy_customer, 'payload': agent_payload,
        })
        self.assertEqual(proposal.status_code, 201, proposal.get_json())
        confirmed = agent_client.post(f"/api/agent/proposals/{proposal.get_json()['id']}/confirm")
        self.assertEqual(confirmed.status_code, 200, confirmed.get_json())

        def snapshot(user, customer_id, inbox_id):
            conn = sqlite3.connect(db.get_user_db_path(user))
            try:
                timeline = conn.execute('''SELECT content, follow_date, result, next_plan, activity_type, direction, source
                                           FROM follow_up_logs WHERE customer_id=? ORDER BY id''', (customer_id,)).fetchall()
                reminders = conn.execute('''SELECT title, remind_date, is_done, reminder_type
                                            FROM reminders WHERE customer_id=?
                                            ORDER BY reminder_type, remind_date, title''', (customer_id,)).fetchall()
                inbox = conn.execute('SELECT customer_id, status FROM inbox_items WHERE id=?', (inbox_id,)).fetchone()
                customer = conn.execute('''SELECT last_contact, next_follow_up, manual_next_follow,
                                                   customer_type, status
                                            FROM customers WHERE id=?''', (customer_id,)).fetchone()
                return {'timeline': timeline, 'reminders': reminders, 'inbox': inbox, 'customer': customer}
            finally:
                conn.close()

        self.assertEqual(snapshot('hamid', hamid_customer, hamid_inbox),
                         snapshot('amy', amy_customer, amy_inbox))

    def test_invalid_agent_activity_confirmation_rolls_back_every_business_change(self):
        spec = importlib.util.spec_from_file_location('crm_app_agent_activity_rollback_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company) VALUES ('回滚客户', 'Rollback Co.')")
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("""INSERT INTO reminders
                         (customer_id, title, remind_date, is_done, reminder_type)
                         VALUES (?, '原待办', '2026-08-28', 0, 'follow_up')""", (customer_id,))
            conn.execute("""INSERT INTO inbox_items
                         (item_type, customer_id, title, content, dedupe_key, status, created_at)
                         VALUES ('customer_reply', ?, '客户回复', '请确认交期', 'agent-rollback-inbox', 'open', '2026-08-29')""",
                         (customer_id,))
            inbox_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        proposal = client.post('/api/agent/proposals', json={
            'type': 'activity', 'customer_id': customer_id,
            'payload': {'content': '客户确认交期', 'follow_date': '2026-08-29',
                        'direction': 'invalid', 'inbox_item_id': inbox_id, 'source': 'inbox'},
        })
        self.assertEqual(proposal.status_code, 201, proposal.get_json())
        confirmed = client.post(f"/api/agent/proposals/{proposal.get_json()['id']}/confirm")
        self.assertEqual(confirmed.status_code, 400, confirmed.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM follow_up_logs WHERE customer_id=?', (customer_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT is_done FROM reminders WHERE customer_id=?', (customer_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT status FROM inbox_items WHERE id=?', (inbox_id,)).fetchone()[0], 'open')
            self.assertEqual(conn.execute('SELECT status FROM agent_proposals WHERE id=?',
                                          (proposal.get_json()['id'],)).fetchone()[0], 'pending')
        finally:
            conn.close()

    def test_agent_gateway_tokens_scopes_isolation_and_idempotent_proposals(self):
        spec = importlib.util.spec_from_file_location('crm_app_gateway_security_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ids = {}
        for user, company in (('hamid', 'Gateway Hamid Co.'), ('amy', 'Gateway Amy Co.')):
            conn = sqlite3.connect(db.get_user_db_path(user))
            try:
                if user == 'amy':
                    conn.execute("INSERT INTO customers (name, company) VALUES ('隔离占位', 'Isolation Placeholder')")
                conn.execute("INSERT INTO customers (name, company) VALUES (?, ?)", (user, company))
                ids[user] = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                conn.commit()
            finally:
                conn.close()

        session_client = module.app.test_client()
        session_client.post('/api/auth/login', json={'user': 'hamid'})
        read_token = session_client.post('/api/agent-gateway/tokens', json={'label': 'read', 'scopes': ['crm:read']})
        self.assertEqual(read_token.status_code, 201, read_token.get_json())
        read_token = read_token.get_json()['data']['token']
        proposal_token = session_client.post('/api/agent-gateway/tokens', json={'label': 'propose', 'scopes': ['crm:propose']})
        self.assertEqual(proposal_token.status_code, 201, proposal_token.get_json())
        proposal_token = proposal_token.get_json()['data']['token']
        conn = sqlite3.connect(os.path.join(db.DB_DIR, 'system.db'))
        try:
            stored = conn.execute("SELECT value FROM app_settings WHERE key LIKE 'agent_gateway_token:%' LIMIT 1").fetchone()[0]
            self.assertNotIn(read_token, stored)
            self.assertIn('token_sha256', stored)
        finally:
            conn.close()

        gateway = module.app.test_client()
        self.assertEqual(gateway.get('/api/gateway/customers').status_code, 401)
        self.assertEqual(gateway.get('/api/gateway/customers', headers={'Authorization': 'Bearer invalid'}).status_code, 401)
        customers = gateway.get('/api/gateway/customers?query=Gateway', headers={'Authorization': 'Bearer ' + read_token})
        self.assertEqual(customers.status_code, 200, customers.get_json())
        self.assertEqual([row['company'] for row in customers.get_json()['data']['customers']], ['Gateway Hamid Co.'])
        self.assertEqual(gateway.get(f"/api/gateway/customers/{ids['amy']}", headers={'Authorization': 'Bearer ' + read_token}).status_code, 404)
        self.assertEqual(gateway.post('/api/gateway/proposals', headers={'Authorization': 'Bearer ' + read_token, 'Idempotency-Key': 'no-write'}, json={
            'action': 'create_task', 'customer_id': ids['hamid'], 'payload': {'title': 'x', 'due_date': '2026-09-01'}
        }).status_code, 403)
        request_body = {'action': 'record_communication', 'customer_id': ids['hamid'], 'payload': {
            'content': '客户确认 9 月上海见。', 'direction': 'inbound', 'activity_type': 'whatsapp',
            'follow_date': '2026-08-30', 'source': 'agent_note', 'source_reference': 'chat:42',
            'next_task': '准备样品', 'next_follow_up': '2026-09-15',
        }}
        headers = {'Authorization': 'Bearer ' + proposal_token, 'Idempotency-Key': 'gateway-communication-1'}
        created = gateway.post('/api/gateway/proposals', headers=headers, json=request_body)
        self.assertEqual(created.status_code, 201, created.get_json())
        proposal_id = created.get_json()['data']['proposal']['id']
        replay = gateway.post('/api/gateway/proposals', headers=headers, json=request_body)
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertEqual(replay.get_json()['data']['proposal']['id'], proposal_id)
        conflict = gateway.post('/api/gateway/proposals', headers=headers, json={**request_body, 'payload': {**request_body['payload'], 'content': '不同事实'}})
        self.assertEqual(conflict.status_code, 409, conflict.get_json())
        denied_override = gateway.post('/api/gateway/proposals', headers={'Authorization': 'Bearer ' + proposal_token, 'Idempotency-Key': 'no-user-override'}, json={**request_body, 'user_id': 'amy'})
        self.assertEqual(denied_override.status_code, 400, denied_override.get_json())
        recovered = session_client.get(f'/api/agent/proposals/{proposal_id}')
        self.assertEqual(recovered.status_code, 200, recovered.get_json())
        confirmed = session_client.post(f'/api/agent/proposals/{proposal_id}/confirm')
        self.assertEqual(confirmed.status_code, 200, confirmed.get_json())

    def test_agent_gateway_write_uses_shared_undo_for_grouped_crm_actions(self):
        spec = importlib.util.spec_from_file_location('crm_app_gateway_write_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company, status, customer_type) VALUES ('Jay', 'EXION', '未建联', 'new')")
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("INSERT INTO contacts (customer_id, name, email) VALUES (?, 'Jay', 'jay@example.com')", (customer_id,))
            contact_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("INSERT INTO reminders (customer_id, title, remind_date, is_done, reminder_type) VALUES (?, '原待办', '2026-08-29', 0, 'follow_up')", (customer_id,))
            old_task_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("INSERT INTO inbox_items (item_type, customer_id, title, content, dedupe_key, status) VALUES ('customer_reply', ?, '客户回复', '上海见', 'gateway-write-inbox', 'open')", (customer_id,))
            inbox_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        session_client = module.app.test_client()
        session_client.post('/api/auth/login', json={'user': 'hamid'})
        write_token = session_client.post('/api/agent-gateway/tokens', json={'scopes': ['crm:write']}).get_json()['data']['token']
        propose_token = session_client.post('/api/agent-gateway/tokens', json={'scopes': ['crm:propose']}).get_json()['data']['token']
        read_token = session_client.post('/api/agent-gateway/tokens', json={'scopes': ['crm:read']}).get_json()['data']['token']
        gateway = module.app.test_client()
        body = {'action': 'record_communication', 'customer_id': customer_id, 'payload': {
            'content': 'Jay 今天确认 9 月 15 日上海见。', 'direction': 'inbound', 'activity_type': 'whatsapp',
            'follow_date': '2026-08-30', 'inbox_item_id': inbox_id, 'next_task': '提前确认会面资料',
            'next_follow_up': '2026-09-12', 'source': 'chat_agent',
        }}
        self.assertEqual(gateway.post('/api/gateway/actions', headers={'Authorization': 'Bearer ' + propose_token, 'Idempotency-Key': 'propose-no-write'}, json=body).status_code, 403)
        self.assertEqual(gateway.post('/api/gateway/actions', headers={'Authorization': 'Bearer ' + read_token, 'Idempotency-Key': 'read-no-write'}, json=body).status_code, 403)
        headers = {'Authorization': 'Bearer ' + write_token, 'Idempotency-Key': 'write-communication-1'}
        written = gateway.post('/api/gateway/actions', headers=headers, json=body)
        self.assertEqual(written.status_code, 201, written.get_json())
        action_id = written.get_json()['data']['action']['id']
        replay = gateway.post('/api/gateway/actions', headers=headers, json=body)
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertEqual(replay.get_json()['data']['action']['id'], action_id)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM follow_up_logs WHERE customer_id=?', (customer_id,)).fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT status FROM inbox_items WHERE id=?', (inbox_id,)).fetchone()[0], 'resolved')
            self.assertEqual(conn.execute('SELECT is_done FROM reminders WHERE id=?', (old_task_id,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reminders WHERE customer_id=? AND title='提前确认会面资料' AND is_done=0", (customer_id,)).fetchone()[0], 1)
            action = conn.execute('SELECT token_id, action_type, undo_token, request_json, status FROM agent_actions WHERE action_id=?', (action_id,)).fetchone()
            self.assertEqual(action[1], 'record_communication')
            self.assertTrue(action[0] and action[2] and '上海见' in action[3] and action[4] == 'completed')
        finally:
            conn.close()
        undone = gateway.post('/api/gateway/actions/' + action_id + '/undo', headers={'Authorization': 'Bearer ' + write_token})
        self.assertEqual(undone.status_code, 200, undone.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM follow_up_logs WHERE customer_id=?', (customer_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT status FROM inbox_items WHERE id=?', (inbox_id,)).fetchone()[0], 'open')
            self.assertEqual(conn.execute('SELECT is_done FROM reminders WHERE id=?', (old_task_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reminders WHERE customer_id=? AND title='提前确认会面资料'", (customer_id,)).fetchone()[0], 0)
        finally:
            conn.close()

        def run_write(action, payload, key, customer=customer_id):
            response = gateway.post('/api/gateway/actions', headers={'Authorization': 'Bearer ' + write_token, 'Idempotency-Key': key}, json={
                'action': action, 'customer_id': customer, 'payload': payload})
            self.assertEqual(response.status_code, 201, response.get_json())
            return response.get_json()['data']['action']['id']

        task_action = run_write('create_task', {'title': '新待办', 'due_date': '2026-09-12'}, 'write-task-1')
        self.assertEqual(gateway.post('/api/gateway/actions/' + task_action + '/undo', headers={'Authorization': 'Bearer ' + write_token}).status_code, 200)
        complete_action = run_write('complete_task', {'task_id': old_task_id, 'content': '已完成原待办', 'direction': 'outbound'}, 'write-complete-1')
        self.assertEqual(gateway.post('/api/gateway/actions/' + complete_action + '/undo', headers={'Authorization': 'Bearer ' + write_token}).status_code, 200)
        inbox_action = run_write('resolve_inbox', {'inbox_item_id': inbox_id, 'resolution_note': '已核实'}, 'write-inbox-1')
        self.assertEqual(gateway.post('/api/gateway/actions/' + inbox_action + '/undo', headers={'Authorization': 'Bearer ' + write_token}).status_code, 200)
        profile_action = run_write('update_customer', {'notes': 'Agent 更新资料'}, 'write-profile-1')
        contact_action = run_write('update_contact', {'contact_id': contact_id, 'title': '采购'}, 'write-contact-1')
        self.assertEqual(gateway.post('/api/gateway/actions/' + profile_action + '/undo', headers={'Authorization': 'Bearer ' + write_token}).status_code, 200)
        self.assertEqual(gateway.post('/api/gateway/actions/' + contact_action + '/undo', headers={'Authorization': 'Bearer ' + write_token}).status_code, 200)
        self.assertEqual(gateway.post('/api/gateway/actions', headers={'Authorization': 'Bearer ' + write_token, 'Idempotency-Key': 'no-delete'}, json={
            'action': 'delete_customer', 'customer_id': customer_id, 'payload': {}
        }).status_code, 409)

    def _legacy_chat_agent_handles_real_crm_conversations_through_gateway(self):
        spec = importlib.util.spec_from_file_location('crm_app_chat_agent_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company, attention_reason) VALUES ('Jay', 'EXION', '等待客户确认会议安排')")
            exion_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("INSERT INTO customers (name, company) VALUES ('Hideout', 'Hideout')")
            hideout_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("INSERT INTO customers (name, company) VALUES ('Acme One', 'Acme')")
            conn.execute("INSERT INTO customers (name, company) VALUES ('Acme Two', 'Acme')")
            conn.execute("INSERT INTO reminders (customer_id, title, remind_date, is_done, reminder_type) VALUES (?, '确认会议时间', ?, 0, 'follow_up')",
                         (exion_id, module._calendar_today().isoformat()))
            conn.execute("INSERT INTO follow_up_logs (customer_id, content, follow_date, direction) VALUES (?, '客户此前询问上海会面安排', '2026-08-29', 'inbound')", (exion_id,))
            conn.commit()
        finally:
            conn.close()
        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        today = client.post('/api/chat/agent', json={'message': '我今天有什么要做？'})
        self.assertEqual(today.status_code, 200, today.get_json())
        self.assertIn('EXION', today.get_json()['reply'])
        status = client.post('/api/chat/agent', json={'message': 'EXION 最近怎么样？'})
        self.assertEqual(status.status_code, 200, status.get_json())
        self.assertIn('客户此前询问上海会面安排', status.get_json()['reply'])
        record_request = {'message': '记录一下 Jay 今天确认 9 月 15 日上海见。', 'idempotency_key': 'chat-retry-record'}
        record = client.post('/api/chat/agent', json=record_request)
        self.assertEqual(record.status_code, 200, record.get_json())
        self.assertTrue(record.get_json()['operations'][0]['action_id'])
        replay = client.post('/api/chat/agent', json=record_request)
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertEqual(replay.get_json()['operations'][0]['action_id'], record.get_json()['operations'][0]['action_id'])
        reminder = client.post('/api/chat/agent', json={'message': '下周三提醒我跟进 Hideout。'})
        self.assertEqual(reminder.status_code, 200, reminder.get_json())
        self.assertIn('Hideout', reminder.get_json()['reply'])
        combined = client.post('/api/chat/agent', json={'message': 'Jay 今天确认 9 月 15 日上海见，记一下，提前三天提醒我。'})
        self.assertEqual(combined.status_code, 200, combined.get_json())
        combined_action = combined.get_json()['operations'][0]['action_id']
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reminders WHERE customer_id=? AND remind_date='2026-09-12' AND is_done=0", (exion_id,)).fetchone()[0], 1)
        finally:
            conn.close()
        undone = client.post('/api/chat/agent', json={'message': '撤销刚才的操作'})
        self.assertEqual(undone.status_code, 200, undone.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute('SELECT status FROM agent_actions WHERE action_id=?', (combined_action,)).fetchone()[0], 'undone')
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reminders WHERE customer_id=? AND remind_date='2026-09-12'", (exion_id,)).fetchone()[0], 0)
        finally:
            conn.close()
        ambiguous = client.post('/api/chat/agent', json={'message': '记录一下 Acme 今天确认样品。'})
        self.assertEqual(ambiguous.status_code, 200, ambiguous.get_json())
        self.assertTrue(ambiguous.get_json()['candidates'])
        self.assertIn('没有修改', ambiguous.get_json()['reply'])
        no_date = client.post('/api/chat/agent', json={'message': '提醒我跟进 Hideout。'})
        self.assertEqual(no_date.status_code, 200, no_date.get_json())
        self.assertIn('明确日期', no_date.get_json()['reply'])
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_actions WHERE action_type=?', ('create_task',)).fetchone()[0], 1)
        finally:
            conn.close()
        amy = module.app.test_client()
        amy.post('/api/auth/login', json={'user': 'amy'})
        self.assertEqual(amy.post('/api/chat/agent', json={'message': '我今天有什么要做？'}).status_code, 404)

    def _legacy_chat_can_delegate_to_pi_runtime_without_giving_it_db_access(self):
        spec = importlib.util.spec_from_file_location('crm_app_pi_runtime_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        pi_events = '\n'.join([
            json.dumps({'type': 'tool_execution_end', 'toolName': 'record_communication',
                        'result': {'details': {'action_id': 'agact_test_runtime_1234567890',
                                               'action_type': 'record_communication', 'undo_available': True}}}),
            json.dumps({'type': 'message_end', 'message': {'role': 'assistant',
                                                            'content': [{'type': 'text', 'text': '已记录 EXION 的最新沟通。'}],
                                                            'stopReason': 'stop'}}),
        ])
        fake_completed = mock.Mock(returncode=0, stdout=pi_events, stderr='')
        with mock.patch.dict(os.environ, {
            'TROSA_PI_AGENT_ENABLED': 'true',
            'TROSA_PI_GATEWAY_TOKEN': 'test-token',
            'TROSA_PI_EXECUTABLE': sys.executable,
            'CRM_SESSION_SECRET': 'must-not-reach-pi',
        }, clear=False), mock.patch.object(module.subprocess, 'run', return_value=fake_completed) as run:
            response = client.post('/api/chat/agent', json={'message': '帮我看看 EXION 的最新情况', 'idempotency_key': 'pi-runtime-test'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['reply'], '已记录 EXION 的最新沟通。')
        self.assertEqual(response.get_json()['operations'][0]['action_id'], 'agact_test_runtime_1234567890')
        command = run.call_args.args[0]
        self.assertIn('--mode', command)
        self.assertIn('--no-builtin-tools', command)
        self.assertIn('--no-context-files', command)
        self.assertIn(str(ROOT / 'pi-agent' / 'trosa-tools.ts'), command)
        self.assertNotIn('--api-key', command)
        runtime_env = run.call_args.kwargs['env']
        self.assertEqual(runtime_env['TROSA_GATEWAY_TOKEN'], 'test-token')
        self.assertNotIn('CRM_SESSION_SECRET', runtime_env)

    def _legacy_pi_runtime_failure_is_reported_without_claiming_a_crm_write(self):
        spec = importlib.util.spec_from_file_location('crm_app_pi_runtime_failure_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        with mock.patch.dict(os.environ, {'TROSA_PI_AGENT_ENABLED': 'true', 'TROSA_PI_GATEWAY_TOKEN': ''}, clear=False):
            response = client.post('/api/chat/agent', json={'message': '帮我整理一下客户情况'})
        self.assertEqual(response.status_code, 503)
        self.assertIn('没有执行任何 CRM 修改', response.get_json()['reply'])
        self.assertEqual(response.get_json()['operations'], [])

    def test_agent_timeline_and_message_search_are_composable_and_authenticated(self):
        spec = importlib.util.spec_from_file_location('crm_app_agent_search_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company, country, created_at, updated_at) VALUES (?, ?, ?, date('now'), date('now'))",
                         ('Search User', 'Search Test Co.', 'Australia'))
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("""INSERT INTO follow_up_logs
                         (customer_id, content, follow_date, activity_type, direction, source, created_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?)""",
                         (customer_id, '客户回复：询问 3mm 透明板报价', '2026-07-20', 'customer_reply', 'inbound', 'manual', '2026-07-20 10:00:00'))
            conn.execute("""INSERT INTO outreach_emails
                         (customer_id, subject, content, sent_date, reply_status, created_at)
                         VALUES (?, ?, ?, ?, ?, ?)""",
                         (customer_id, '3mm acrylic pricing', '发送 DDP 报价说明', '2026-07-18', 'replied', '2026-07-18 09:00:00'))
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        self.assertEqual(client.get('/api/agent/messages/search').status_code, 401)
        self.assertEqual(client.get(f'/api/agent/customers/{customer_id}/timeline').status_code, 401)
        client.post('/api/auth/login', json={'user': 'hamid'})

        timeline = client.get(f'/api/agent/customers/{customer_id}/timeline?limit=10')
        self.assertEqual(timeline.status_code, 200, timeline.get_json())
        events = timeline.get_json()['events']
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]['event_date'], '2026-07-20')
        self.assertEqual(events[0]['direction'], 'inbound')

        search = client.get('/api/agent/messages/search?query=报价&country=Australia&limit=10')
        self.assertEqual(search.status_code, 200, search.get_json())
        items = search.get_json()['items']
        self.assertEqual(len(items), 2)
        self.assertTrue(all(item['country'] == 'Australia' for item in items))

        inbound = client.get('/api/agent/messages/search?query=报价&direction=inbound')
        self.assertEqual(inbound.status_code, 200, inbound.get_json())
        self.assertEqual(len(inbound.get_json()['items']), 1)
        self.assertEqual(inbound.get_json()['items'][0]['event_type'], 'communication')
        replied = client.get('/api/agent/messages/search?reply_status=replied')
        self.assertEqual(replied.status_code, 200, replied.get_json())
        self.assertEqual(len(replied.get_json()['items']), 1)
        self.assertEqual(replied.get_json()['items'][0]['event_type'], 'outreach_email')
        self.assertEqual(client.get('/api/agent/messages/search?from_date=2026-08-05&to_date=2026-08-01').status_code, 400)

    def test_task_edit_delete_and_conflict_aware_undo(self):
        spec = importlib.util.spec_from_file_location('crm_app_task_undo_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company, created_at, updated_at) VALUES (?, ?, date('now'), date('now'))",
                         ('Undo User', 'Undo Test Co.'))
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("INSERT INTO reminders (customer_id, title, content, reason, remind_date, is_done) VALUES (?, ?, ?, ?, ?, 0)",
                         (customer_id, '原待办', '原待办', '原原因', '2026-08-04'))
            reminder_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        edited = client.patch(f'/api/reminders/{reminder_id}', json={
            'title': '修改后的待办', 'remind_date': '2026-08-07', 'reason': '延后三天'
        })
        self.assertEqual(edited.status_code, 200, edited.get_json())
        undo_token = edited.get_json()['undo_token']
        self.assertEqual(client.post(f'/api/undo/{undo_token}').status_code, 200)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute('SELECT title, remind_date FROM reminders WHERE id=?', (reminder_id,)).fetchone(),
                             ('原待办', '2026-08-04'))
        finally:
            conn.close()

        rescheduled = client.post(f'/api/reminders/{reminder_id}/reschedule', json={'remind_date': '2026-08-07'})
        self.assertEqual(rescheduled.status_code, 200, rescheduled.get_json())
        reschedule_token = rescheduled.get_json()['undo_token']
        changed_again = client.patch(f'/api/reminders/{reminder_id}', json={'title': '人工再次修改'})
        self.assertEqual(changed_again.status_code, 200, changed_again.get_json())
        conflict = client.post(f'/api/undo/{reschedule_token}')
        self.assertEqual(conflict.status_code, 409, conflict.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute('SELECT title, remind_date FROM reminders WHERE id=?', (reminder_id,)).fetchone(),
                             ('人工再次修改', '2026-08-07'))
        finally:
            conn.close()

        deleted = client.delete(f'/api/reminders/{reminder_id}')
        self.assertEqual(deleted.status_code, 200, deleted.get_json())
        self.assertEqual(client.post(f"/api/undo/{deleted.get_json()['undo_token']}").status_code, 200)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute('SELECT is_done FROM reminders WHERE id=?', (reminder_id,)).fetchone()[0], 0)
        finally:
            conn.close()

    def test_today_reminder_order_requires_full_snapshot_and_is_undoable(self):
        spec = importlib.util.spec_from_file_location('crm_app_today_order_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company) VALUES ('排序客户一', 'Order One')")
            first_customer = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("INSERT INTO customers (name, company) VALUES ('排序客户二', 'Order Two')")
            second_customer = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            for customer_id, title in ((first_customer, '第一条'), (second_customer, '第二条')):
                conn.execute("INSERT INTO reminders (customer_id, title, content, remind_date, is_done) VALUES (?, ?, ?, '2026-08-05', 0)",
                             (customer_id, title, title))
            conn.commit()
            reminder_ids = [row[0] for row in conn.execute("SELECT id FROM reminders ORDER BY id").fetchall()]
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        brief = client.get('/api/agent/brief/today?format=json')
        self.assertEqual(brief.status_code, 200, brief.get_json())
        current_ids = [item['id'] for item in brief.get_json()['due_tasks']]
        self.assertEqual(set(reminder_ids), set(current_ids))

        incomplete = client.post('/api/reminders/today/order', json={
            'ids': [current_ids[0]], 'expected_ids': current_ids,
        })
        self.assertEqual(incomplete.status_code, 400, incomplete.get_json())

        reordered_ids = list(reversed(current_ids))
        reordered = client.post('/api/reminders/today/order', json={
            'ids': reordered_ids, 'expected_ids': current_ids, 'reason': '先处理第二条',
        })
        self.assertEqual(reordered.status_code, 200, reordered.get_json())
        undo_token = reordered.get_json()['undo_token']
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute('SELECT manual_order FROM reminders WHERE id=?', (reordered_ids[0],)).fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT manual_order FROM reminders WHERE id=?', (reordered_ids[1],)).fetchone()[0], 2)
        finally:
            conn.close()

        undone = client.post(f'/api/undo/{undo_token}')
        self.assertEqual(undone.status_code, 200, undone.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            orders = dict(conn.execute('SELECT id, manual_order FROM reminders WHERE id IN (?, ?)', tuple(current_ids)).fetchall())
            self.assertEqual(orders[current_ids[0]], 0)
            self.assertEqual(orders[current_ids[1]], 0)
        finally:
            conn.close()

    def test_agent_command_routes_reads_and_requires_confirmation_for_writes(self):
        spec = importlib.util.spec_from_file_location('crm_app_agent_command_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company, created_at, updated_at) VALUES (?, ?, date('now'), date('now'))",
                         ('Command User', 'Command Test Co.'))
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})

        read = client.post('/api/agent/command', json={'command': '查看 Command Test Co. 的资料和下一步'})
        self.assertEqual(read.status_code, 200, read.get_json())
        self.assertEqual(read.get_json()['mode'], 'read')
        self.assertIn('Command Test Co.', read.get_json()['answer'])

        task = client.post('/api/agent/command', json={'command': '给 Command Test Co. 安排确认报价数量，明天'})
        self.assertEqual(task.status_code, 200, task.get_json())
        self.assertIn('proposal', task.get_json(), task.get_json())
        task_proposal = task.get_json()['proposal']
        self.assertEqual(task.get_json()['mode'], 'proposal')
        self.assertEqual(task_proposal['type'], 'task')
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reminders WHERE customer_id=?", (customer_id,)).fetchone()[0], 0)
        finally:
            conn.close()
        confirmed = client.post(f"/api/agent/proposals/{task_proposal['id']}/confirm")
        self.assertEqual(confirmed.status_code, 200, confirmed.get_json())

        activity = client.post('/api/agent/command', json={'command': '记录沟通到 Command Test Co.：客户回复了上次报价'})
        self.assertEqual(activity.status_code, 200, activity.get_json())
        activity_proposal = activity.get_json()['proposal']
        self.assertEqual(activity_proposal['type'], 'activity')
        cancelled = client.post(f"/api/agent/proposals/{activity_proposal['id']}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM follow_up_logs WHERE customer_id=?", (customer_id,)).fetchone()[0], 0)
        finally:
            conn.close()

    def test_excel_sync_rejects_paths_outside_the_current_upload(self):
        spec = importlib.util.spec_from_file_location('crm_app_sync_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        allowed = str(Path(self.tempdir.name) / 'allowed.xlsx')
        outside = str(Path(self.tempdir.name) / 'outside.xlsx')
        Path(allowed).write_bytes(b'placeholder')
        Path(outside).write_bytes(b'placeholder')
        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        with mock.patch.object(module, 'get_uploaded_excel_path', return_value=allowed), \
             mock.patch.object(module, 'sync_from_excel') as sync:
            response = client.post('/api/sync', json={'excel_path': outside})
        self.assertEqual(response.status_code, 400)
        sync.assert_not_called()

    def test_marked_customer_order_can_be_saved(self):
        spec = importlib.util.spec_from_file_location('crm_app_priority_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company) VALUES ('A', '客户 A')")
            conn.execute("INSERT INTO customers (name, company) VALUES ('B', '客户 B')")
            conn.commit()
        finally:
            conn.close()
        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        self.assertEqual(client.post('/api/customers/1/priority', json={'action': 'pin'}).status_code, 200)
        self.assertEqual(client.post('/api/customers/2/priority', json={'action': 'pin'}).status_code, 200)
        response = client.post('/api/customers/priority/order', json={'ids': [2, 1]})
        self.assertEqual(response.status_code, 200)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            rows = conn.execute(
                'SELECT id, is_pinned, pinned_order FROM customers ORDER BY pinned_order'
            ).fetchall()
            self.assertEqual(rows, [(2, 1, 1), (1, 1, 2)])
        finally:
            conn.close()


class CustomerFileAttachmentTest(unittest.TestCase):
    """客户文件附件：上传、列表、预览/下载、删除和越权隔离。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.init_all_dbs()

    def tearDown(self):
        db.DB_DIR = self.original_db_dir
        self.tempdir.cleanup()

    def _module_and_customer(self):
        spec = importlib.util.spec_from_file_location('crm_app_customer_file_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        created = client.post('/api/customers', json={'name': '文件测试客户', 'customer_type': 'existing'})
        self.assertEqual(created.status_code, 201, created.get_json())
        return module, client, created.get_json()['id']

    def _fake_pdf(self, name='报价单.pdf'):
        return (io.BytesIO(b'%PDF-1.4\n' + b'fake-content-' * 40), name)

    def test_customer_file_upload_list_preview_download_delete_round_trip(self):
        module, client, customer_id = self._module_and_customer()
        stream, name = self._fake_pdf()
        with mock.patch.object(module, 'schedule_safety_backup'):
            try:
                uploaded = client.post(
                    f'/api/customers/{customer_id}/files',
                    data={'files': (stream, name)},
                    content_type='multipart/form-data',
                )
            finally:
                stream.close()
        self.assertEqual(uploaded.status_code, 200, uploaded.get_json())
        created = uploaded.get_json()['created']
        self.assertEqual(len(created), 1)
        file_id = created[0]['id']
        self.assertEqual(created[0]['name'], '报价单.pdf')

        detail = client.get(f'/api/customers/{customer_id}').get_json()
        self.assertEqual(len(detail['files']), 1)
        self.assertEqual(detail['files'][0]['id'], file_id)
        self.assertTrue(detail['files'][0]['sha256'])
        self.assertFalse(detail['files'][0]['missing'])

        listed = client.get(f'/api/customers/{customer_id}/files').get_json()['files']
        self.assertEqual(len(listed), 1)

        download = client.get(f'/api/customers/{customer_id}/files/{file_id}/download')
        try:
            self.assertEqual(download.status_code, 200, download.get_json())
            self.assertIn('attachment', download.headers.get('Content-Disposition', ''))
            expected_stream, _ = self._fake_pdf()
            try:
                self.assertEqual(download.data, expected_stream.getvalue())
            finally:
                expected_stream.close()
        finally:
            download.close()

        preview = client.get(f'/api/customers/{customer_id}/files/{file_id}/download?inline=1')
        try:
            self.assertEqual(preview.status_code, 200)
            self.assertIn('inline', preview.headers.get('Content-Disposition', ''))
        finally:
            preview.close()

        stored_path = os.path.join(
            self.tempdir.name, 'uploads', 'customer_files', str(customer_id))
        stored_files = os.listdir(stored_path)
        self.assertEqual(len(stored_files), 1)

        with mock.patch.object(module, 'schedule_safety_backup'):
            removed = client.delete(f'/api/customers/{customer_id}/files/{file_id}')
        self.assertEqual(removed.status_code, 200, removed.get_json())
        self.assertEqual(os.listdir(stored_path), [])
        self.assertEqual(client.get(f'/api/customers/{customer_id}/files').get_json()['files'], [])
        self.assertEqual(client.get(f'/api/customers/{customer_id}').get_json()['files'], [])
        self.assertEqual(
            client.get(f'/api/customers/{customer_id}/files/{file_id}/download').status_code, 404)

    def test_customer_file_excel_preview_renders_table_and_escapes_content(self):
        module, client, customer_id = self._module_and_customer()
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['公司', '报价', '<script>alert(1)</script>'])
        sheet.append(['ACME', 1200.5, 'OK'])
        stream = io.BytesIO()
        workbook.save(stream)
        stream.seek(0)
        with mock.patch.object(module, 'schedule_safety_backup'):
            try:
                uploaded = client.post(
                    f'/api/customers/{customer_id}/files',
                    data={'files': (stream, '报价表.xlsx')},
                    content_type='multipart/form-data',
                )
            finally:
                stream.close()
        file_id = uploaded.get_json()['created'][0]['id']

        preview = client.get(f'/api/customers/{customer_id}/files/{file_id}/preview')
        self.assertEqual(preview.status_code, 200, preview.data[:200])
        self.assertIn('text/html', preview.headers.get('Content-Type', ''))
        self.assertIn('ACME', preview.get_data(as_text=True))
        self.assertIn('1200', preview.get_data(as_text=True))
        # 单元格内容必须被转义，不能作为 HTML/脚本执行。
        self.assertNotIn('<script>', preview.get_data(as_text=True))
        self.assertIn('&lt;script&gt;', preview.get_data(as_text=True))
        # 预览页提供下载入口。
        self.assertIn('/download', preview.get_data(as_text=True))

    def _make_docx(self, paragraphs, table_rows=None):
        ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        body = ''.join(f'<w:p><w:r><w:t>{para}</w:t></w:r></w:p>' for para in paragraphs)
        if table_rows:
            body += '<w:tbl>' + ''.join(
                '<w:tr>' + ''.join(
                    f'<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>' for cell in row)
                + '</w:tr>' for row in table_rows) + '</w:tbl>'
        xml = f'<w:document {ns}><w:body>{body}</w:body></w:document>'
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as archive:
            archive.writestr('word/document.xml', xml)
        buf.seek(0)
        return buf

    def _make_pptx(self, slides):
        ns = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
              'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as archive:
            for index, texts in enumerate(slides, 1):
                paras = ''.join(f'<a:p><a:r><a:t>{text}</a:t></a:r></a:p>' for text in texts)
                xml = (f'<p:sld {ns}><p:cSld><p:spTree>'
                       f'<p:sp><p:txBody>{paras}</p:txBody></p:sp>'
                       f'</p:spTree></p:cSld></p:sld>')
                archive.writestr(f'ppt/slides/slide{index}.xml', xml)
        buf.seek(0)
        return buf

    def _make_eml(self):
        raw = ('From: supplier@example.com\r\n'
               'To: hamid@example.com\r\n'
               'Subject: 报价邮件\r\n'
               'Date: Mon, 10 Aug 2026 10:00:00 +0800\r\n'
               'Content-Type: text/plain; charset=utf-8\r\n'
               '\r\n'
               '报价内容测试').encode('utf-8')
        return io.BytesIO(raw)

    def _make_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as archive:
            archive.writestr('报价单.pdf', b'%PDF-fake')
            archive.writestr('合同.docx', b'docx-fake')
        buf.seek(0)
        return buf

    def _upload_preview(self, module, client, customer_id, stream, name):
        with mock.patch.object(module, 'schedule_safety_backup'):
            try:
                uploaded = client.post(
                    f'/api/customers/{customer_id}/files',
                    data={'files': (stream, name)},
                    content_type='multipart/form-data',
                )
            finally:
                stream.close()
        self.assertEqual(uploaded.status_code, 200, uploaded.get_json())
        file_id = uploaded.get_json()['created'][0]['id']
        preview = client.get(f'/api/customers/{customer_id}/files/{file_id}/preview')
        self.assertEqual(preview.status_code, 200, preview.data[:200])
        return preview.get_data(as_text=True)

    def test_customer_file_docx_preview_renders_paragraphs_and_tables(self):
        module, client, customer_id = self._module_and_customer()
        body = self._upload_preview(
            module, client, customer_id,
            self._make_docx(['报价说明', '含税价格'], [['型号', '价格'], ['A-100', '1200']]),
            '报价单.docx')
        self.assertIn('报价说明', body)
        self.assertIn('A-100', body)
        self.assertIn('1200', body)
        self.assertIn('Word 文本预览', body)
        self.assertIn('/download', body)

    def test_customer_file_pptx_preview_renders_slide_text(self):
        module, client, customer_id = self._module_and_customer()
        body = self._upload_preview(
            module, client, customer_id,
            self._make_pptx([['第一页标题', '第一页内容'], ['第二页标题']]),
            '产品介绍.pptx')
        self.assertIn('第一页标题', body)
        self.assertIn('第一页内容', body)
        self.assertIn('第二页标题', body)
        self.assertIn('PPT 文本预览', body)

    def test_customer_file_eml_preview_renders_email(self):
        module, client, customer_id = self._module_and_customer()
        body = self._upload_preview(module, client, customer_id, self._make_eml(), '往来邮件.eml')
        self.assertIn('报价邮件', body)
        self.assertIn('supplier@example.com', body)
        self.assertIn('报价内容测试', body)
        self.assertIn('邮件预览', body)

    def test_customer_file_zip_preview_lists_contents(self):
        module, client, customer_id = self._module_and_customer()
        body = self._upload_preview(module, client, customer_id, self._make_zip(), '资料包.zip')
        self.assertIn('报价单.pdf', body)
        self.assertIn('合同.docx', body)
        self.assertIn('ZIP 内容', body)

    def test_customer_file_rtf_preview_strips_markup(self):
        module, client, customer_id = self._module_and_customer()
        raw = b'{\\rtf1\\ansi Hello \\b bold\\b0  text \\par second line}'
        body = self._upload_preview(module, client, customer_id, io.BytesIO(raw), '说明.rtf')
        self.assertIn('Hello', body)
        self.assertIn('bold', body)
        self.assertIn('second line', body)
        self.assertNotIn('\\rtf', body)
        self.assertIn('RTF 文本预览', body)

    def test_customer_file_unsupported_types_show_unavailable_preview(self):
        module, client, customer_id = self._module_and_customer()
        for name, content in (('旧版.doc', b'D0CF11E0 binary'), ('演示.ppt', b'D0CF11E0 binary'),
                              ('压缩.rar', b'Rar!\x1a\x07'), ('压缩.7z', b'7z\xbc\xaf\x27\x1c'),
                              ('邮件.msg', b'msg-binary')):
            body = self._upload_preview(module, client, customer_id, io.BytesIO(content), name)
            self.assertIn('暂不支持预览', body)
            self.assertIn('/download', body)

    def test_customer_file_rejects_unsupported_type_empty_and_unknown_customer(self):
        module, client, customer_id = self._module_and_customer()
        stream = io.BytesIO(b'MZ-binary')
        try:
            rejected = client.post(
                f'/api/customers/{customer_id}/files',
                data={'files': (stream, 'tool.exe')},
                content_type='multipart/form-data',
            )
        finally:
            stream.close()
        self.assertEqual(rejected.status_code, 400, rejected.get_json())
        self.assertIn('没有成功上传的文件', rejected.get_json()['error'])

        empty = client.post(
            f'/api/customers/{customer_id}/files',
            data={}, content_type='multipart/form-data')
        self.assertEqual(empty.status_code, 400, empty.get_json())

        stream2, name2 = self._fake_pdf()
        try:
            unknown = client.post(
                '/api/customers/999999/files',
                data={'files': (stream2, name2)}, content_type='multipart/form-data')
        finally:
            stream2.close()
        self.assertEqual(unknown.status_code, 404, unknown.get_json())

    def test_customer_file_download_is_isolated_between_customers(self):
        module, client, customer_id = self._module_and_customer()
        other = client.post('/api/customers', json={'name': '另一个客户', 'customer_type': 'existing'})
        other_id = other.get_json()['id']
        stream, name = self._fake_pdf('机密资料.pdf')
        with mock.patch.object(module, 'schedule_safety_backup'):
            try:
                uploaded = client.post(
                    f'/api/customers/{customer_id}/files',
                    data={'files': (stream, name)},
                    content_type='multipart/form-data',
                )
            finally:
                stream.close()
        file_id = uploaded.get_json()['created'][0]['id']
        self.assertEqual(
            client.get(f'/api/customers/{other_id}/files/{file_id}/download').status_code, 404)
        self.assertEqual(client.delete(f'/api/customers/{other_id}/files/{file_id}').status_code, 404)
        self.assertEqual(client.get(f'/api/customers/{other_id}/files').status_code, 200)
        self.assertEqual(client.get(f'/api/customers/{other_id}/files').get_json()['files'], [])

    def test_customer_file_survives_startup_maintenance_and_remains_downloadable(self):
        module, client, customer_id = self._module_and_customer()
        stream, name = self._fake_pdf('历史合同.pdf')
        with mock.patch.object(module, 'schedule_safety_backup'):
            try:
                uploaded = client.post(
                    f'/api/customers/{customer_id}/files',
                    data={'files': (stream, name)},
                    content_type='multipart/form-data',
                )
            finally:
                stream.close()
        self.assertEqual(uploaded.status_code, 200, uploaded.get_json())
        file_id = uploaded.get_json()['created'][0]['id']
        stored_path = os.path.join(
            self.tempdir.name, 'uploads', 'customer_files', str(customer_id))
        stored_file = os.path.join(stored_path, os.listdir(stored_path)[0])
        old_timestamp = time.time() - 46 * 86400
        os.utime(stored_file, (old_timestamp, old_timestamp))

        result = db.run_startup_maintenance()

        self.assertEqual(result.get('removed_files', 0), 0)
        self.assertTrue(os.path.exists(stored_file))
        download = client.get(f'/api/customers/{customer_id}/files/{file_id}/download')
        try:
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.data, b'%PDF-1.4\n' + b'fake-content-' * 40)
        finally:
            download.close()

    def test_customer_file_delete_is_recoverable(self):
        module, client, customer_id = self._module_and_customer()
        stream, name = self._fake_pdf('可撤销合同.pdf')
        try:
            with mock.patch.object(module, 'schedule_safety_backup'):
                uploaded = client.post(
                    f'/api/customers/{customer_id}/files',
                    data={'files': (stream, name)}, content_type='multipart/form-data')
        finally:
            stream.close()
        file_id = uploaded.get_json()['created'][0]['id']
        with mock.patch.object(module, 'schedule_safety_backup'):
            deleted = client.delete(f'/api/customers/{customer_id}/files/{file_id}')
        self.assertTrue(deleted.get_json()['undoable'])
        self.assertEqual(client.get(f'/api/customers/{customer_id}/files/{file_id}/download').status_code, 404)
        restored = client.post(f'/api/customers/{customer_id}/files/{file_id}/restore')
        self.assertEqual(restored.status_code, 200, restored.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            file_path = conn.execute('SELECT file_path FROM customer_files WHERE id=?', (file_id,)).fetchone()[0]
        finally:
            conn.close()
        self.assertTrue(os.path.isfile(os.path.join(self.tempdir.name, file_path)), file_path)
        download = client.get(f'/api/customers/{customer_id}/files/{file_id}/download')
        try:
            self.assertEqual(download.status_code, 200, download.get_json())
            self.assertIn(b'%PDF-1.4', download.data)
        finally:
            download.close()

    def test_customer_file_backup_restores_attachment_binary(self):
        module, client, customer_id = self._module_and_customer()
        stream, name = self._fake_pdf('备份合同.pdf')
        try:
            with mock.patch.object(module, 'schedule_safety_backup'):
                uploaded = client.post(
                    f'/api/customers/{customer_id}/files',
                    data={'files': (stream, name)}, content_type='multipart/form-data')
        finally:
            stream.close()
        file_id = uploaded.get_json()['created'][0]['id']
        snapshot = db.backup_database('attachment_restore_test')
        relative = os.path.relpath(snapshot['path'], os.path.join(db.DB_DIR, 'backups')).replace('\\', '/')
        manifest = json.loads(Path(snapshot['path'], 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(len(manifest['attachments']), 1)
        attachment = manifest['attachments'][0]['name']
        os.remove(os.path.join(db.DB_DIR, attachment))
        result = db.restore_from_backup(relative)
        self.assertTrue(result['success'], result)
        restored = client.get(f'/api/customers/{customer_id}/files/{file_id}/download')
        try:
            self.assertEqual(restored.status_code, 200)
            self.assertIn(b'fake-content', restored.data)
        finally:
            restored.close()

    def test_customer_file_batch_uses_per_file_limit_not_request_limit(self):
        module, client, customer_id = self._module_and_customer()
        payload = b'x' * (13 * 1024 * 1024)
        with mock.patch.object(module, 'schedule_safety_backup'):
            response = client.post(
                f'/api/customers/{customer_id}/files',
                data={'files': [
                    (io.BytesIO(payload), '批量一.txt'),
                    (io.BytesIO(payload), '批量二.txt'),
                ]}, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(len(response.get_json()['created']), 2)


class SmartWebsiteImportTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_dir = db.DB_DIR
        db.DB_DIR = self.tempdir.name
        os.environ.pop('CRM_SEED_DEMO_DATA', None)
        db.init_all_dbs()

    def tearDown(self):
        db.DB_DIR = self.original_db_dir
        self.tempdir.cleanup()

    def test_website_facts_are_review_only_and_need_no_model(self):
        spec = importlib.util.spec_from_file_location('crm_app_smart_import_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})

        website_facts = {
            'name': 'Central Signs Group',
            'description': 'Central Group provides acrylic signage and fabrication in Colombia.',
            'emails': ['hello@centralsigns.example'],
            'phones': ['+57 300 123 4567'],
            'linkedin': ['https://www.linkedin.com/company/central-signs-group/'],
            'contacts': [{
                'name': 'Alex Rivera', 'title': 'Sales Manager',
                'email': 'alex@centralsigns.example', 'source': '官网事实（结构化数据）',
            }],
        }
        with mock.patch.object(module, 'fetch_website_content', return_value=(
            'Central Group acrylic signage distributor in Colombia. Contact hello@centralco.example.',
            {'ok': True, 'pages_read': ['https://centralco.example'], 'read_method': 'direct', 'website_facts': website_facts},
        )), mock.patch.object(module, 'exa_search', return_value=([], {'enabled': False})), \
             mock.patch.object(module, 'quick_chat') as chat:
            response = client.post('/api/customers/smart-import', json={
                'company': '', 'website': 'https://centralco.example',
            })

        self.assertEqual(response.status_code, 200, response.get_json())
        result = response.get_json()
        self.assertEqual(result['name'], 'Central Signs Group')
        self.assertEqual(result['country'], '哥伦比亚')
        self.assertEqual(result['field'], '亚克力分销')
        self.assertEqual(result['contacts'][0]['email'], 'alex@centralsigns.example')
        self.assertFalse(result['ai_used'])
        chat.assert_not_called()

        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0], 0)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
