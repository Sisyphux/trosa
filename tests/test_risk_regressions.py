import importlib.util
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import subprocess
import tempfile
import time
import unittest
import zipfile
import uuid
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db
import postgres_schema_contract as postgres_contract
from postgres_compat import _translate_sql
import scheduler
from ical_gen import build_icalendar
from tools.unified_postgres_import import (
    compat_dedupe_key, compat_uuid, clean, legacy_bool, legacy_int, parse_time,
    sela_evidence_entries,
)


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

    def test_duplicate_external_identities_are_quarantined_before_unique_indexes(self):
        db.init_all_dbs()
        path = db.get_user_db_path('hamid')
        conn = sqlite3.connect(path)
        conn.execute('DROP INDEX idx_customers_external_identity')
        conn.execute('DROP INDEX idx_outreach_external_identity')
        conn.execute('DROP INDEX idx_import_unmatched_hash')
        conn.execute(
            '''INSERT INTO customers(name, company, external_source, external_id)
               VALUES ('A', 'A', 'sela', 'candidate-duplicate')'''
        )
        conn.execute(
            '''INSERT INTO customers(name, company, external_source, external_id)
               VALUES ('B', 'B', 'sela', 'candidate-duplicate')'''
        )
        conn.execute(
            '''INSERT INTO outreach_emails(customer_id, subject, external_source, external_id)
               VALUES (1, 'One', 'sela', 'outreach-duplicate')'''
        )
        conn.execute(
            '''INSERT INTO outreach_emails(customer_id, subject, external_source, external_id)
               VALUES (1, 'Two', 'sela', 'outreach-duplicate')'''
        )
        conn.execute(
            '''INSERT INTO import_unmatched_customers
               (unmatched_hash, customer_name, reason)
               VALUES ('unmatched-duplicate', 'A', 'old')'''
        )
        conn.execute(
            '''INSERT INTO import_unmatched_customers
               (unmatched_hash, customer_name, reason)
               VALUES ('unmatched-duplicate', 'B', 'old')'''
        )
        conn.commit()
        conn.close()

        db.init_user_tables('hamid')
        conn = sqlite3.connect(path)
        try:
            customer_ids = [row[0] for row in conn.execute(
                "SELECT external_id FROM customers WHERE external_source='sela' ORDER BY id"
            ).fetchall()]
            outreach_ids = [row[0] for row in conn.execute(
                "SELECT external_id FROM outreach_emails WHERE external_source='sela' ORDER BY id"
            ).fetchall()]
            unmatched_hashes = [row[0] for row in conn.execute(
                "SELECT unmatched_hash FROM import_unmatched_customers ORDER BY id"
            ).fetchall()]
            self.assertEqual(customer_ids[0], 'candidate-duplicate')
            self.assertTrue(customer_ids[1].startswith('candidate-duplicate#duplicate:'))
            self.assertEqual(outreach_ids[0], 'outreach-duplicate')
            self.assertTrue(outreach_ids[1].startswith('outreach-duplicate#duplicate:'))
            self.assertEqual(unmatched_hashes[0], 'unmatched-duplicate')
            self.assertTrue(unmatched_hashes[1].startswith('unmatched-duplicate#duplicate:'))
            note = conn.execute(
                "SELECT system_notes FROM customers WHERE external_id LIKE 'candidate-duplicate#duplicate:%'"
            ).fetchone()[0]
            self.assertIn('外部身份键重复', note)
            index_names = {row[1] for row in conn.execute('PRAGMA index_list(customers)').fetchall()}
            self.assertIn('idx_customers_external_identity', index_names)
        finally:
            conn.close()

    def test_restore_validates_snapshot_and_removes_stale_wal(self):
        # Backups now enforce the same closed user set as production. Build a
        # complete empty installation before adding the fixture row so this
        # test exercises restore/WAL behavior rather than an incomplete-set
        # rejection.
        db.init_all_dbs()
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
        db.init_all_dbs()
        system_path = os.path.join(db.DB_DIR, 'system.db')
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

    def test_postgres_mode_never_reports_or_restores_a_sqlite_backup(self):
        with mock.patch.object(db, 'postgres_mode', return_value=True):
            backup = db.backup_database('postgres-test')
            self.assertEqual(backup['database'], 'postgresql')
            self.assertTrue(backup['managed_externally'])
            self.assertEqual(backup['path'], '')
            self.assertEqual(db.run_scheduled_local_backup()['database'], 'postgresql')
            restored = db.restore_from_backup('2026-01-01/000000')
            self.assertFalse(restored['success'])
            self.assertTrue(restored['managed_externally'])

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

    def test_postgres_backup_routes_do_not_expose_sqlite_operations(self):
        spec = importlib.util.spec_from_file_location('crm_app_postgres_backup_routes_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        client = module.app.test_client()
        self.assertEqual(client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)
        with mock.patch.object(module, 'postgres_mode', return_value=True):
            manual = client.post('/api/backup')
            self.assertEqual(manual.status_code, 409)
            self.assertTrue(manual.get_json()['managed_externally'])
            listed = client.get('/api/backup/list')
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.get_json()['backups'], [])
            restored = client.post('/api/backup/restore', json={'date': '2026-01-01/000000'})
            self.assertEqual(restored.status_code, 409)

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

    def test_contact_update_returns_saved_contact_preserves_fields_and_supports_undo(self):
        spec = importlib.util.spec_from_file_location('crm_app_contact_update_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company) VALUES ('联系人编辑客户', 'Contact Edit Co.')")
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute('''INSERT INTO contacts
                            (customer_id, name, title, email, phone, whatsapp, linkedin,
                             preferred_channel, contact_type, is_primary, notes)
                            VALUES (?, '原联系人', '原职位', 'original@example.com', '100', '200',
                                    'https://linkedin.example/original', 'email', 'person', 1, '保留备注')''',
                         (customer_id,))
            contact_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        with mock.patch.object(module, 'schedule_safety_backup'):
            response = client.put(f'/api/contacts/{contact_id}', json={
                'name': '更新联系人',
                'title': '采购负责人',
                'email': ' UPDATED@example.com ',
                'phone': '300',
                'whatsapp': '400',
                'linkedin': 'https://linkedin.example/updated',
            })
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload['contact']['name'], '更新联系人')
        self.assertEqual(payload['contact']['email'], 'updated@example.com')
        self.assertTrue(payload['undo_token'])

        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        conn.row_factory = sqlite3.Row
        try:
            saved = conn.execute('SELECT * FROM contacts WHERE id=?', (contact_id,)).fetchone()
            self.assertEqual(saved['preferred_channel'], 'email')
            self.assertEqual(saved['contact_type'], 'person')
            self.assertEqual(saved['is_primary'], 1)
            self.assertEqual(saved['notes'], '保留备注')
            conn.execute("INSERT INTO contacts (customer_id, name, email) VALUES (?, '其他联系人', 'other@example.com')",
                         (customer_id,))
            conn.commit()
        finally:
            conn.close()

        conflict = client.put(f'/api/contacts/{contact_id}', json={'email': 'other@example.com'})
        self.assertEqual(conflict.status_code, 409, conflict.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            unchanged = conn.execute('SELECT email FROM contacts WHERE id=?', (contact_id,)).fetchone()
            self.assertEqual(unchanged[0], 'updated@example.com')
        finally:
            conn.close()

        with mock.patch.object(module, 'schedule_safety_backup'):
            undone = client.post('/api/undo/' + payload['undo_token'])
        self.assertEqual(undone.status_code, 200, undone.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            restored = conn.execute('SELECT name, email, phone FROM contacts WHERE id=?', (contact_id,)).fetchone()
            self.assertEqual(tuple(restored), ('原联系人', 'original@example.com', '100'))
        finally:
            conn.close()

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

    def test_recording_communication_with_empty_contact_id_stores_null(self):
        """An unlinked communication must not send an empty string to typed databases."""
        spec = importlib.util.spec_from_file_location('crm_app_empty_contact_id_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("INSERT INTO customers (name, company, created_at, updated_at) VALUES (?, ?, date('now'), date('now'))",
                         ('未关联联系人', '空联系人测试客户'))
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})
        response = client.post(f'/api/customers/{customer_id}/follow_history', json={
            'activity_content': '记录一条未关联联系人的沟通', 'activity_type': 'follow_up',
            'direction': 'outbound', 'follow_date': '2026-09-04', 'contact_id': ''
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        activity_id = response.get_json()['id']
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertIsNone(conn.execute('SELECT contact_id FROM follow_up_logs WHERE id=?',
                                           (activity_id,)).fetchone()[0])
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

    def test_customer_search_prioritizes_identity_before_records_and_paginates(self):
        """Customer identity matches must outrank communication and task text."""
        spec = importlib.util.spec_from_file_location('crm_app_search_rank_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute("""INSERT INTO customers
                         (name, company, created_at, updated_at, is_deleted)
                         VALUES ('Regal Buyer', 'Regal Plastics', '2026-08-01', '2026-08-01', 0)""")
            identity_customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("""INSERT INTO reminders
                         (customer_id, title, remind_date, is_done, reminder_type, created_at)
                         VALUES (?, '联系 Regal Plastics', '2026-09-09', 0, 'manual', '2026-08-01')""",
                         (identity_customer_id,))

            conn.execute("""INSERT INTO customers
                         (name, company, created_at, updated_at, is_deleted)
                         VALUES ('Quote Timeline Buyer', 'Quote Timeline Co.', '2026-08-02', '2026-08-02', 0)""")
            communication_customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("""INSERT INTO follow_up_logs
                         (customer_id, content, follow_date, activity_type, direction, created_at)
                         VALUES (?, '报价已发出，等待客户确认', '2026-08-02', 'email', 'outbound', '2026-08-02')""",
                         (communication_customer_id,))

            conn.execute("""INSERT INTO customers
                         (name, company, created_at, updated_at, is_deleted)
                         VALUES ('Quote Task Buyer', 'Quote Task Co.', '2026-08-03', '2026-08-03', 0)""")
            task_customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute("""INSERT INTO reminders
                         (customer_id, title, remind_date, is_done, reminder_type, created_at)
                         VALUES (?, '报价', '2026-09-03', 0, 'manual', '2026-08-03')""",
                         (task_customer_id,))

            conn.execute("""INSERT INTO customers
                         (name, company, field, created_at, updated_at, is_deleted)
                         VALUES ('Plastic Field Buyer', 'Plastic Field Co.', '塑料分销', '2026-08-04', '2026-08-04', 0)""")
            for index in range(5):
                conn.execute("""INSERT INTO customers
                             (name, company, field, created_at, updated_at, is_deleted)
                             VALUES (?, ?, '塑料分销', '2026-08-05', '2026-08-05', 0)""",
                             (f'Plastic Extra Buyer {index + 1}', f'Plastic Extra Co. {index + 1}'))
            conn.execute("""INSERT INTO customers
                         (name, company, field, created_at, updated_at, is_deleted)
                         VALUES ('Plastic Duplicate Buyer', 'Plastic Field Co.', '塑料分销', '2026-08-06', '2026-08-06', 0)""")

            conn.execute("""INSERT INTO customers
                         (name, company, created_at, updated_at, is_deleted)
                         VALUES ('Archived Regal', 'Regal Archived Co.', '2026-08-06', '2026-08-06', 1)""")
            conn.commit()
        finally:
            conn.close()

        client = module.app.test_client()
        client.post('/api/auth/login', json={'user': 'hamid'})

        identity_search = client.get('/api/customers?search=Regal&page=1&per_page=5')
        self.assertEqual(identity_search.status_code, 200, identity_search.get_json())
        identity_data = identity_search.get_json()
        self.assertEqual(identity_data['total'], 1)
        identity_customer = identity_data['customers'][0]
        self.assertEqual(identity_customer['id'], identity_customer_id)
        self.assertEqual(identity_customer['match_context']['type'], 'customer_field')
        self.assertEqual(identity_customer['match_context']['label'], '公司名称')
        self.assertEqual(identity_customer['search_matches'][0]['label'], '公司名称')

        record_search = client.get('/api/customers?search=报价&page=1&per_page=5')
        self.assertEqual(record_search.status_code, 200, record_search.get_json())
        record_data = record_search.get_json()
        self.assertEqual(record_data['total'], 2)
        self.assertEqual(record_data['customers'][0]['id'], communication_customer_id)
        self.assertEqual(record_data['customers'][0]['match_context']['type'], 'communication')
        self.assertEqual(record_data['customers'][1]['id'], task_customer_id)
        self.assertEqual(record_data['customers'][1]['match_context']['type'], 'task')

        paged_search = client.get('/api/customers?search=塑料&page=2&per_page=5')
        self.assertEqual(paged_search.status_code, 200, paged_search.get_json())
        paged_data = paged_search.get_json()
        self.assertEqual(paged_data['total'], 6)
        self.assertEqual(paged_data['pages'], 2)
        self.assertEqual(len(paged_data['customers']), 1)
        self.assertEqual(len({item['id'] for item in paged_data['customers']}), 1)
        all_search = client.get('/api/customers?search=塑料&page=1&per_page=100').get_json()
        self.assertEqual(len({(item.get('company') or item.get('name')).casefold() for item in all_search['customers']}), 6)

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

    def test_pi_runtime_starts_each_chat_turn_without_legacy_session_history(self):
        spec = importlib.util.spec_from_file_location('crm_app_pi_isolated_turn_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pi_events = json.dumps({
            'type': 'message_end',
            'message': {
                'role': 'assistant',
                'content': [{'type': 'text', 'text': '当前入口不处理文件搜索。'}],
                'stopReason': 'stop',
            },
        })
        fake_completed = mock.Mock(returncode=0, stdout=pi_events, stderr='')
        with mock.patch.dict(os.environ, {
            'TROSA_PI_AGENT_ENABLED': 'true',
            'TROSA_PI_GATEWAY_TOKEN': 'test-token',
            'TROSA_PI_EXECUTABLE': sys.executable,
            'TROSA_PI_EXTENSION': str(ROOT / 'pi-agent' / 'trosa-tools.ts'),
            'TROSA_PI_SYSTEM_PROMPT': str(ROOT / 'pi-agent' / 'system-prompt.md'),
        }, clear=False), mock.patch.object(module.subprocess, 'run', return_value=fake_completed) as run:
            response, status = module._run_pi_agent('检查一下 Excel 文件', request_id='pi-isolated-turn-test')
        self.assertEqual(status, 200, response)
        command = run.call_args.args[0]
        self.assertIn('--no-session', command)
        self.assertNotIn('--session', command)

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


class InputBoundaryRegressionTest(unittest.TestCase):
    """Typed compatibility inputs fail safely before a business write."""

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

    def _load_module(self, name):
        spec = importlib.util.spec_from_file_location(name, ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _client_and_customer(self, module):
        client = module.app.test_client()
        self.assertEqual(client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)
        response = client.post('/api/customers', json={
            'name': '边界测试客户', 'company': 'Boundary Input Co.',
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        return client, response.get_json()['id']

    def test_malformed_dates_are_rejected_before_any_write(self):
        module = self._load_module('crm_app_input_dates_test')
        client = module.app.test_client()
        self.assertEqual(client.post('/api/auth/login', json={'user': 'hamid'}).status_code, 200)

        invalid_create = client.post('/api/customers', json={
            'name': '不应写入', 'company': 'Invalid Date Co.', 'last_contact': 'not-a-date',
        })
        self.assertEqual(invalid_create.status_code, 400, invalid_create.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM customers WHERE company='Invalid Date Co.'").fetchone()[0], 0)
        finally:
            conn.close()

        created = client.post('/api/customers', json={
            'name': '日期边界客户', 'company': 'Date Boundary Co.', 'next_follow_up': '2026-09-10',
        })
        self.assertEqual(created.status_code, 201, created.get_json())
        customer_id = created.get_json()['id']
        bad_update = client.put(f'/api/customers/{customer_id}', json={'next_follow_up': '2026-99-01'})
        self.assertEqual(bad_update.status_code, 400, bad_update.get_json())

        bad_batch = client.post('/api/customers/batch/next_follow_up', json={
            'ids': [customer_id], 'value': 'not-a-date',
        })
        self.assertEqual(bad_batch.status_code, 400, bad_batch.get_json())

        task = client.post(f'/api/customers/{customer_id}/tasks', json={
            'title': '有效待办', 'due_date': '2026-09-12',
        })
        self.assertEqual(task.status_code, 201, task.get_json())
        task_id = task.get_json()['id']
        bad_complete = client.put(f'/api/reminders/{task_id}', json={
            'activity_content': '不应完成', 'next_task': '下一步', 'next_follow_up': 'bad-date',
        })
        self.assertEqual(bad_complete.status_code, 400, bad_complete.get_json())
        bad_edit = client.patch(f'/api/reminders/{task_id}', json={'remind_date': 'bad-date'})
        self.assertEqual(bad_edit.status_code, 400, bad_edit.get_json())
        bad_reschedule = client.post(f'/api/reminders/{task_id}/reschedule', json={'remind_date': 'bad-date'})
        self.assertEqual(bad_reschedule.status_code, 400, bad_reschedule.get_json())

        bad_outreach = client.post(f'/api/customers/{customer_id}/outreach', json={
            'subject': '不应写入', 'sent_date': 'bad-date',
        })
        self.assertEqual(bad_outreach.status_code, 400, bad_outreach.get_json())
        valid_outreach = client.post(f'/api/customers/{customer_id}/outreach', json={
            'subject': '有效开发信', 'sent_date': '2026-09-01',
        })
        self.assertEqual(valid_outreach.status_code, 201, valid_outreach.get_json())
        outreach_id = valid_outreach.get_json()['outreach']['id']
        bad_reply_date = client.put(f'/api/outreach/{outreach_id}', json={'reply_date': 'bad-date'})
        self.assertEqual(bad_reply_date.status_code, 400, bad_reply_date.get_json())

        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute('''INSERT INTO follow_up_logs
                         (customer_id, content, follow_date, activity_type, direction, source, created_at)
                         VALUES (?, '已有沟通', '2026-09-01', 'follow_up', 'outbound', 'test', '2026-09-01')''',
                         (customer_id,))
            log_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        bad_history = client.put(f'/api/follow-history/{log_id}', json={'follow_date': 'bad-date'})
        self.assertEqual(bad_history.status_code, 400, bad_history.get_json())

        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            reminder = conn.execute('SELECT is_done, remind_date FROM reminders WHERE id=?', (task_id,)).fetchone()
            self.assertEqual(tuple(reminder), (0, '2026-09-12'))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM follow_up_logs WHERE content=?', ('不应完成',)).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT reply_date FROM outreach_emails WHERE id=?', (outreach_id,)).fetchone()[0], '')
            self.assertEqual(conn.execute('SELECT follow_date FROM follow_up_logs WHERE id=?', (log_id,)).fetchone()[0], '2026-09-01')
        finally:
            conn.close()

        extension = client.post('/api/extension/communications', json={
            'customer_id': customer_id, 'content': '扩展日期也不应写入',
            'follow_date': 'bad-date', 'messages': [{'text': '消息', 'time': '2026-09-04'}],
        })
        self.assertEqual(extension.status_code, 400, extension.get_json())

    def test_typed_ids_are_normalized_or_rejected_without_500(self):
        module = self._load_module('crm_app_input_ids_test')
        client, customer_id = self._client_and_customer(module)

        archived = client.post('/api/inbox/archive', json={
            'dedupe_key': 'boundary-archive', 'item_type': 'customer_reply', 'customer_id': '',
        })
        self.assertEqual(archived.status_code, 200, archived.get_json())
        snooze_bad = client.post('/api/inbox/snooze', json={
            'dedupe_key': 'boundary-snooze', 'item_type': 'ai_suggestion',
            'customer_id': '', 'days': 'not-an-int',
        })
        self.assertEqual(snooze_bad.status_code, 400, snooze_bad.get_json())
        snoozed = client.post('/api/inbox/snooze', json={
            'dedupe_key': 'boundary-snooze', 'item_type': 'ai_suggestion',
            'customer_id': '', 'days': 7,
        })
        self.assertEqual(snoozed.status_code, 200, snoozed.get_json())
        reply = client.post('/api/inbox/reply', json={'customer_id': '', 'content': '回复'})
        self.assertEqual(reply.status_code, 400, reply.get_json())
        suggestion = client.post('/api/inbox/resolve-suggestion', json={
            'dedupe_key': f'ai_suggestion:{customer_id}:x', 'customer_id': 'not-an-id',
            'reason': 'waiting_reply',
        })
        self.assertEqual(suggestion.status_code, 400, suggestion.get_json())

        malformed_batch_requests = (
            ('/api/customers/batch/status', {'ids': [''], 'value': '跟进中'}),
            ('/api/customers/batch/level', {'ids': [''], 'value': 'B'}),
            ('/api/customers/batch/next_follow_up', {'ids': [''], 'value': '2026-09-10'}),
            ('/api/customers/batch/follow_history', {'ids': [''], 'content': '批量记录'}),
            ('/api/customers/batch/delete', {'ids': ['']}),
            ('/api/customers/priority/order', {'ids': ['']}),
            ('/api/reminders/batch/complete', {'ids': ['']}),
            ('/api/reminders/today/order', {'ids': ['']}),
        )
        for path, payload in malformed_batch_requests:
            response = client.post(path, json=payload)
            self.assertEqual(response.status_code, 400, (path, response.get_json()))

        contact = client.post(f'/api/customers/{customer_id}/contacts', json={
            'name': '空开关联系人', 'email': 'blank-flag@example.com', 'is_primary': '',
        })
        self.assertEqual(contact.status_code, 201, contact.get_json())
        contact_id = contact.get_json()['contact_id']
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            self.assertEqual(conn.execute('SELECT is_primary FROM contacts WHERE id=?', (contact_id,)).fetchone()[0], 0)
            self.assertIsNone(conn.execute('SELECT customer_id FROM inbox_items WHERE dedupe_key=?', ('boundary-archive',)).fetchone()[0])
            self.assertIsNone(conn.execute('SELECT customer_id FROM inbox_items WHERE dedupe_key=?', ('boundary-snooze',)).fetchone()[0])
        finally:
            conn.close()

        second = client.post('/api/customers', json={'name': '第二客户', 'company': 'Second Boundary Co.'})
        self.assertEqual(second.status_code, 201, second.get_json())
        cross_customer = client.post(f"/api/customers/{second.get_json()['id']}/follow_history", json={
            'activity_content': '不应关联其他客户联系人', 'contact_id': contact_id,
        })
        self.assertEqual(cross_customer.status_code, 400, cross_customer.get_json())


    def test_static_compatibility_and_diagnostic_404(self):
        module = self._load_module('crm_app_static_boundary_test')
        client = module.app.test_client()
        self.assertEqual(client.get('/').status_code, 200)
        for path in (
            '/app.js', '/static/app.js', '/style.css', '/visual-v2.css',
            '/icons/phosphor/check.svg', '/assets/workspace-tree-lines-v2.webp', '/favicon.ico',
        ):
            response = client.get(path)
            self.assertEqual(response.status_code, 200, path)
            body = response.data
            response.close()
            self.assertGreater(len(body), 0, path)
        with self.assertLogs(module.logger, level='WARNING') as captured:
            missing = client.get('/static/definitely-missing.js')
        self.assertEqual(missing.status_code, 404)
        missing.close()
        self.assertTrue(any('definitely-missing.js' in line for line in captured.output))

    def test_postgres_hardening_migration_is_registered_and_restores_ids(self):
        migration = (ROOT / 'migrations' / '0007_postgres_runtime_hardening.sql').read_text(encoding='utf-8')
        self.assertIn('compat_legacy_bigint', migration)
        self.assertIn("e.payload->>'contact_id'", migration)
        self.assertIn("e.payload->>'related_task_id'", migration)
        self.assertIn("o.legacy_payload->>'contact_id'", migration)
        self.assertEqual(Path(db._postgres_migration_paths()[-1]).name, '0013_postgres_company_match_boundaries.sql')
        tool_source = (ROOT / 'tools' / 'unified_postgres_migration.py').read_text(encoding='utf-8')
        self.assertIn('0007_postgres_runtime_hardening.sql', tool_source)

    def test_postgres_integrity_hardening_is_registered(self):
        migration = (ROOT / 'migrations' / '0008_postgres_runtime_integrity_hardening.sql').read_text(encoding='utf-8')
        self.assertIn('compat_dedupe_key', migration)
        self.assertIn('trosa.compat_time', migration)
        self.assertIn('pg_advisory_xact_lock', migration)
        self.assertIn('company_id=CASE WHEN coalesce(core.contact_methods.person_id,excluded.person_id)', migration)
        self.assertIn('username text NOT NULL DEFAULT', migration)
        self.assertIn('web_fetched_at text NOT NULL DEFAULT', migration)
        tool_source = (ROOT / 'tools' / 'unified_postgres_migration.py').read_text(encoding='utf-8')
        self.assertIn('0008_postgres_runtime_integrity_hardening.sql', tool_source)

    def test_production_applied_postgres_migrations_keep_immutable_hashes(self):
        # These are the hashes recorded by the read-only ECS audit. Any future
        # schema change must be a new forward migration, never an edit to one
        # of the seven migrations already applied in production.
        expected = {
            '0001_unified_trade_os.sql': '59073dc1493174b6f84feb9c26c68fafece348da917294e4963d00b72ac89ab0',
            '0002_postgres_runtime.sql': '2ae2780ed33a210b202977f0a28b1b0a9842ea620fda1a22491ca0cc66c020f6',
            '0003_postgres_app_compat.sql': '0be9840cd8af7e21778ad85164fbcf195cc356c631461cf59a1c8e2d2e3d85b8',
            '0004_postgres_runtime_surfaces.sql': 'b562bfed7ebebafbab6dd79537740ddeefafff0b3b0fdac1e096c60156932523',
            '0005_postgres_runtime_write_fixes.sql': '086e52bb1c9f41ff706b08b463820b6f49e83bb9f80f225b6b76bf1c22f58c2f',
            '0006_postgres_runtime_surface_writes.sql': '9f4db2576d8ab46dcbc8e8e7a0d57251fffda46ad7abe51d9aae2fb9598211b2',
            '0007_postgres_runtime_hardening.sql': '955d2c0fd9038d5c238ff47d64004ba8c84f7e7af8340d0eb53717debfcb85e1',
        }
        for name, digest in expected.items():
            actual = hashlib.sha256((ROOT / 'migrations' / name).read_bytes()).hexdigest()
            self.assertEqual(actual, digest, name)

    def test_legacy_import_values_are_not_silently_coerced(self):
        self.assertEqual(clean(0), '0')
        self.assertEqual(clean(False), 'False')
        self.assertEqual(legacy_int('12'), 12)
        self.assertIsNone(legacy_int(True))
        self.assertIsNone(legacy_int('2.5'))
        self.assertFalse(legacy_bool('0'))
        self.assertTrue(legacy_bool('yes'))
        self.assertTrue(legacy_bool('', True))
        self.assertEqual(compat_dedupe_key('amy', 'ai_suggestion:1:hold'), 'compat:amy:ai_suggestion:1:hold')
        digest = hashlib.md5('agent-gateway:amy:write:create_task:key-1'.encode()).hexdigest()
        self.assertEqual(compat_uuid('agent-gateway:amy:write:create_task:key-1'), uuid.UUID(digest))
        parsed = parse_time('2026-09-04T12:00:00')
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.utcoffset().total_seconds(), 8 * 60 * 60)
        utc_parsed = parse_time('2026-09-04 12:00:00+00:00')
        self.assertIsNotNone(utc_parsed)
        self.assertEqual(utc_parsed.utcoffset().total_seconds(), 0)

    def test_postgres_final_integrity_boundaries_are_registered(self):
        migration = (ROOT / 'migrations' / '0009_postgres_final_integrity_boundaries.sql').read_text(encoding='utf-8')
        for marker in (
            'integration_sync_receipt_rows', 'team_invitations_write', 'source_batch_id',
            'pg_advisory_xact_lock', 'email_message_receipts', 'email_delivery_events',
            'agent_gateway_idempotency', 'agent-proposal:', 'agent-gateway:',
            'Conflicting integration_sync_receipts rows',
        ):
            self.assertIn(marker, migration)
        tool_source = (ROOT / 'tools' / 'unified_postgres_migration.py').read_text(encoding='utf-8')
        self.assertIn('0009_postgres_final_integrity_boundaries.sql', tool_source)

    def test_postgres_external_provider_keys_are_user_scoped(self):
        migration = (ROOT / 'migrations' / '0010_postgres_user_scoped_external_ids.sql').read_text(encoding='utf-8')
        for marker in (
            'legacy_user_id', 'trosa_email_receipts_org_user_provider_idx',
            'trosa_communication_items_org_user_fp_idx',
            'gmail-receipt:', 'communication-item:',
            'organization_id,legacy_user_id,provider_message_id',
            'organization_id,legacy_user_id,source_fingerprint',
        ):
            self.assertIn(marker, migration)
        importer = (ROOT / 'tools' / 'unified_postgres_import.py').read_text(encoding='utf-8')
        self.assertIn('compat_uuid(f"gmail-receipt:{user}:{provider}")', importer)
        self.assertIn('compat_uuid(f"communication-item:{user}:{fingerprint}")', importer)

    def test_postgres_legacy_identity_updates_cannot_duplicate_rows(self):
        migration = (ROOT / 'migrations' / '0009_postgres_final_integrity_boundaries.sql').read_text(encoding='utf-8')
        for marker in (
            'integration receipt identity is immutable',
            'gateway idempotency identity is immutable',
            'agent action identity is immutable',
            'undo action identity is immutable',
            'weekly report identity is immutable',
            'imported activity hash already belongs to another row',
            'unmatched customer hash already belongs to another row',
            'imported activity audit hash already belongs to another row',
            'unmatched customer audit hash already belongs to another row',
            'communication source identity is immutable',
            'communication source item identity is immutable',
        ):
            self.assertIn(marker, migration)

    def test_postgres_final_compat_identity_guards_cover_legacy_views(self):
        migration = (ROOT / 'migrations' / '0011_postgres_compat_identity_guards.sql').read_text(encoding='utf-8')
        for marker in (
            'research report identity is immutable',
            'external analysis note identity is immutable',
            'customer understanding identity is immutable',
            'AI recommendation identity is immutable',
            'user identity is immutable',
            'email verification identity is immutable',
            'email verification job identity is immutable',
            'email domain probe identity is immutable',
            'email log identity is immutable',
        ):
            self.assertIn(marker, migration)
        importer = (ROOT / 'tools' / 'unified_postgres_import.py').read_text(encoding='utf-8')
        self.assertIn('company_id=case when coalesce(core.contact_methods.person_id,excluded.person_id)', importer)

    def test_postgres_schema_contract_covers_external_identity_columns(self):
        contract = (ROOT / 'postgres_schema_contract.py').read_text(encoding='utf-8')
        for marker in (
            '("trosa.email_verifications", "legacy_id")',
            '("trosa.email_verification_jobs", "legacy_id")',
            '("trosa.email_domain_probes", "legacy_id")',
            '("trosa.email_logs", "legacy_key")',
            'trosa_email_verification_legacy_idx',
            'trosa_email_verification_job_legacy_idx',
            'trosa_email_domain_probe_legacy_idx',
            'trosa_email_logs_legacy_key_idx',
        ):
            self.assertIn(marker, contract)

    def test_postgres_schema_contract_accepts_a_complete_surface(self):
        class CompleteContractCursor:
            def __init__(self):
                self.position = 0

            def execute(self, *_args, **_kwargs):
                self.position += 1

            def fetchall(self):
                responses = (
                    [(name,) for name in postgres_contract.REQUIRED_SCHEMAS],
                    ([(name, 'r') for name in postgres_contract.REQUIRED_TABLES]
                     + [(name, 'v') for name in postgres_contract.REQUIRED_VIEWS]),
                    list(postgres_contract.REQUIRED_COLUMNS),
                    [(f'{relation}.{name}', True, list(columns))
                     for relation, name, columns in postgres_contract.REQUIRED_INDEXES],
                    [(name, True) for name in postgres_contract.REQUIRED_FUNCTIONS],
                    [(name,) for name in postgres_contract.REQUIRED_TRIGGERS],
                )
                return responses[self.position - 1]

        status = postgres_contract.schema_status(CompleteContractCursor())
        self.assertTrue(status['ok'])
        self.assertTrue(all(status['schemas'].values()))

    def test_postgres_legacy_email_ids_are_projected_and_user_scoped(self):
        migration = (ROOT / 'migrations' / '0012_postgres_legacy_email_ids.sql').read_text(encoding='utf-8')
        for marker in (
            'coalesce(legacy_id,id) AS id',
            "legacy_key ~ '^[0-9]+$'",
            "compat_next_id('email_verifications'",
            "compat_next_id('email_verification_jobs'",
            "compat_next_id('email_domain_probes'",
            "compat_next_id('email_logs'",
            'email verification % is not visible for user %',
            'email log % is not visible for user %',
            "legacy:ambiguous:'||s.id::text",
        ):
            self.assertIn(marker, migration)

    def test_postgres_migration_ledger_refuses_historical_replay(self):
        for path in (ROOT / 'db.py', ROOT / 'tools' / 'unified_postgres_migration.py'):
            source = path.read_text(encoding='utf-8')
            self.assertIn('Historical PostgreSQL migration', source)
            self.assertIn('add a new forward migration', source)

    def test_postgres_migration_cli_works_without_pythonpath(self):
        env = os.environ.copy()
        env.pop('PYTHONPATH', None)
        result = subprocess.run(
            [sys.executable, str(ROOT / 'tools' / 'unified_postgres_migration.py'), '--help'],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('usage:', result.stdout.lower())

    def test_importer_routes_ambiguous_company_matches_to_review(self):
        importer = (ROOT / 'tools' / 'unified_postgres_import.py').read_text(encoding='utf-8')
        self.assertIn('AMBIGUOUS_COMPANY_MATCH', importer)
        self.assertIn('COMPANY_MATCH_REVIEW', importer)
        self.assertIn('company-candidate:{source_key}', importer)
        self.assertIn('source_identity=f"trosa:{user}:customer:{legacy_customer_id}"', importer)
        self.assertIn('source_identity=f"sela:candidate:{candidate_id}"', importer)
        self.assertIn("identity_status=case when excluded.identity_status='review'", importer)

    def test_postgres_compat_company_matching_requires_exact_domain(self):
        migration = (ROOT / 'migrations' / '0013_postgres_company_match_boundaries.sql').read_text(encoding='utf-8')
        for marker in (
            'CREATE OR REPLACE FUNCTION trosa.compat_customers_write()',
            'v_domain_match_count',
            'company-candidate:',
            'identity_status',
            "CASE WHEN v_match_review THEN 'review' ELSE 'imported' END",
            'Name + country is a candidate signal',
        ):
            self.assertIn(marker, migration)
        self.assertNotIn('normalized_name=v_normalized_name', migration)
        self.assertNotIn('ORDER BY d.is_primary DESC, d.created_at ASC', migration)
        self.assertIn('0013_postgres_company_match_boundaries.sql', (ROOT / 'tools' / 'unified_postgres_migration.py').read_text(encoding='utf-8'))

    def test_importer_covers_every_legacy_user_table(self):
        importer = (ROOT / 'tools' / 'unified_postgres_import.py').read_text(encoding='utf-8')
        declared = {
            match.group(1)
            for match in re.finditer(r'CREATE TABLE IF NOT EXISTS (\w+)', '\n'.join(db.USER_TABLE_SQL))
        }
        handled = {
            match.group(1)
            for match in re.finditer(r'rows\.get\("([a-z_]+)"', importer)
        }
        self.assertFalse(declared - handled)

    def test_sela_evidence_shapes_are_preserved_as_separate_rows(self):
        entries = sela_evidence_entries({
            'evidence': '[{"type":"directory","url":"https://example.test","text":"proof"}]',
            'website': 'https://fallback.test',
        })
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['evidence_type'], 'directory')
        self.assertEqual(entries[0]['source_url'], 'https://example.test')
        fallback = sela_evidence_entries({'website': 'https://fallback.test'})
        self.assertEqual(fallback[0]['source_url'], 'https://fallback.test')


class PostgresCompatibilityRegressionTest(unittest.TestCase):
    """SQLite-shaped writes must remain valid against PostgreSQL views."""

    def test_conflict_clauses_are_removed_for_every_writable_compat_view(self):
        statements = (
            "INSERT INTO customer_understandings (customer_id, version) VALUES (?, ?) "
            "ON CONFLICT(customer_id) DO UPDATE SET version=excluded.version",
            "INSERT INTO inbox_items (item_type, dedupe_key) VALUES (?, ?) "
            "ON CONFLICT(dedupe_key) DO UPDATE SET status='resolved'",
            "INSERT OR IGNORE INTO contacts (customer_id, name) VALUES (?, ?)",
            "INSERT INTO trade_os_compat.customer_files (customer_id, original_name) VALUES (?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            "INSERT INTO integration_sync_receipts (integration, idempotency_key) VALUES (?, ?) "
            "ON CONFLICT(integration, idempotency_key) DO NOTHING",
            "INSERT INTO team_invitations (id, token_hash, created_by) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO NOTHING",
        )
        for statement in statements:
            translated = _translate_sql(statement)
            self.assertNotRegex(translated, r"\bON\s+CONFLICT\b", statement)
            self.assertNotIn("?", translated)
            self.assertIn("%s", translated)

    def test_real_compatibility_tables_keep_conflict_semantics(self):
        translated = _translate_sql(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        self.assertRegex(translated, r"\bON\s+CONFLICT\b")


class CustomerAiSummaryTest(unittest.TestCase):
    """按需 AI 摘要必须兼容旧调用，并在无模型时保留事实回退。"""

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

    def _module_client_customer(self):
        spec = importlib.util.spec_from_file_location('crm_app_customer_ai_summary_test', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        client = module.app.test_client()
        login = client.post('/api/auth/login', json={'user': 'hamid'})
        self.assertEqual(login.status_code, 200, login.get_json())
        conn = sqlite3.connect(db.get_user_db_path('hamid'))
        try:
            conn.execute(
                '''INSERT INTO customers
                   (name, company, country, profile, field, notes, attention_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                ('SK Crafts', 'SK Crafts Limited', '英国', '采购亚克力与 PS 板材', '塑料板材', '客户询问规格和采购安排', '等待客户分享公司资料'),
            )
            customer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute(
                '''INSERT INTO contacts (customer_id, name, title, email, is_primary)
                   VALUES (?, ?, ?, ?, 1)''',
                (customer_id, 'Sam', 'Purchasing', 'sam@example.test'),
            )
            conn.execute(
                '''INSERT INTO follow_up_logs
                   (customer_id, content, follow_date, result, next_plan, direction)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (customer_id, '客户表示需要亚克力与 PS 板材，并要求分享公司资料', '2026-09-01', '等待资料确认', '', 'inbound'),
            )
            conn.execute(
                '''INSERT INTO reminders (customer_id, title, remind_date, reminder_type)
                   VALUES (?, ?, ?, 'follow_up')''',
                (customer_id, '确认客户资料', '2026-09-05'),
            )
            conn.commit()
        finally:
            conn.close()
        return module, client, customer_id

    def test_unknown_direction_from_old_client_is_accepted(self):
        module, client, customer_id = self._module_client_customer()
        llm_result = json.dumps({
            'summary': '客户询问亚克力与 PS 板材。', 'needs': ['亚克力与 PS 板材'],
            'key_facts': [], 'intent': '积极', 'mentioned_company': 'SK Crafts Limited',
            'mentioned_contact': 'Sam', 'message_date': '2026-09-01',
            'suggested_next_action': '', 'direction': 'inbound',
        }, ensure_ascii=False)
        with mock.patch.object(module, 'quick_chat', return_value=llm_result):
            response = client.post('/api/inbox/analyze-reply', json={
                'content': 'SK Crafts Limited: We order acrylic & PS sheets',
                'direction': 'unknown', 'customer_id': customer_id,
            })
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['analysis']['direction'], 'inbound')

    def test_customer_summary_returns_model_result_and_legacy_alias(self):
        module, client, customer_id = self._module_client_customer()
        model_summary = '## 客户是谁\nSK Crafts Limited 位于英国。\n\n## 已记录沟通与需求\n客户询问亚克力与 PS 板材。'
        with mock.patch.object(module, 'quick_chat', return_value=model_summary) as mocked:
            response = client.post(f'/api/customers/{customer_id}/ai-summary')
            legacy = client.post(f'/api/intelligence/analyze/{customer_id}')
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload['summary'], model_summary)
        self.assertEqual(payload['analysis'], model_summary)
        self.assertTrue(payload['ai_available'])
        self.assertEqual(payload['source'], 'llm')
        self.assertIn('SK Crafts Limited', mocked.call_args_list[0].kwargs['customer_context'])
        self.assertEqual(legacy.status_code, 200, legacy.get_json())
        self.assertEqual(legacy.get_json()['analysis'], model_summary)

    def test_customer_summary_falls_back_to_crm_facts_without_model(self):
        module, client, customer_id = self._module_client_customer()
        with mock.patch.object(module, 'quick_chat', return_value='[错误] 所有 LLM 后端均不可用'):
            response = client.post(f'/api/customers/{customer_id}/ai-summary')
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertFalse(payload['ai_available'])
        self.assertEqual(payload['source'], 'crm_facts')
        self.assertEqual(payload['summary'], payload['analysis'])
        self.assertIn('SK Crafts Limited', payload['summary'])
        self.assertIn('亚克力与 PS 板材', payload['summary'])

    def test_customer_summary_requires_login_and_frontend_exposes_both_paths(self):
        module, _, customer_id = self._module_client_customer()
        anonymous = module.app.test_client()
        self.assertEqual(anonymous.post(f'/api/customers/{customer_id}/ai-summary').status_code, 401)
        javascript = (ROOT / 'app' / 'static' / 'app.js').read_text(encoding='utf-8')
        self.assertIn("/api/customers/' + customerId + '/ai-summary", javascript)
        self.assertIn("direction: requestedDirection", javascript)

    def test_ai_config_status_is_safe_and_mutations_are_admin_only(self):
        module, client, _ = self._module_client_customer()
        safe_status = {
            'backend': 'openai',
            'backend_label': 'OpenAI / 兼容接口',
            'configured': True,
            'api_key_configured': True,
            'base_url': 'https://api.example.test/v1',
            'model': 'test-model',
            'vision_configured': True,
            'config_source': '快速接入配置',
            'providers': [],
        }
        with mock.patch.object(module, 'get_ai_config_status', return_value=dict(safe_status)):
            response = client.get('/api/ai/config')
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertTrue(payload['can_edit'])
        self.assertNotIn('api_key', payload)
        self.assertNotIn('secret', json.dumps(payload, ensure_ascii=False).lower())

        saved_status = dict(safe_status)
        with mock.patch.object(module, 'save_ai_config', return_value=saved_status) as save:
            response = client.put('/api/ai/config', json={
                'backend': 'openai', 'api_key': 'do-not-return-this',
                'base_url': 'https://api.example.test/v1', 'model': 'test-model',
            })
        self.assertEqual(response.status_code, 200, response.get_json())
        save.assert_called_once()
        self.assertNotIn('do-not-return-this', response.get_data(as_text=True))

        member = module.app.test_client()
        self.assertEqual(member.post('/api/auth/login', json={'user': 'amy'}).status_code, 200)
        with mock.patch.object(module, 'get_ai_config_status', return_value=dict(safe_status)):
            member_status = member.get('/api/ai/config')
        self.assertEqual(member_status.status_code, 200, member_status.get_json())
        self.assertFalse(member_status.get_json()['can_edit'])
        self.assertEqual(member.put('/api/ai/config', json={'backend': 'openai'}).status_code, 403)
        self.assertEqual(member.post('/api/ai/config/test', json={'backend': 'openai'}).status_code, 403)
        self.assertEqual(member.post('/api/ai/config/models', json={'backend': 'openai'}).status_code, 403)

        with mock.patch.object(module, 'list_ai_models', return_value={
            'success': True, 'provider': 'openai', 'models': [{'id': 'test-model'}],
        }) as list_models:
            response = client.post('/api/ai/config/models', json={
                'backend': 'openai', 'api_key': 'pending-secret',
                'base_url': 'https://api.example.test/v1',
            })
        self.assertEqual(response.status_code, 200, response.get_json())
        list_models.assert_called_once_with({
            'backend': 'openai', 'api_key': 'pending-secret',
            'base_url': 'https://api.example.test/v1',
        })
        self.assertNotIn('pending-secret', response.get_data(as_text=True))

    def test_ai_config_connection_test_is_separate_and_never_gets_crm_context(self):
        module, client, _ = self._module_client_customer()
        with mock.patch.object(module, 'test_ai_connection', return_value={
            'success': True, 'provider': 'openai', 'model': 'test-model'
        }) as probe:
            response = client.post('/api/ai/config/test', json={'backend': 'openai'})
        self.assertEqual(response.status_code, 200, response.get_json())
        probe.assert_called_once_with({'backend': 'openai'})
        self.assertNotIn('customer', json.dumps(probe.call_args.args, ensure_ascii=False).lower())

        javascript = (ROOT / 'app' / 'static' / 'app.js').read_text(encoding='utf-8')
        html = (ROOT / 'app' / 'static' / 'index.html').read_text(encoding='utf-8')
        self.assertIn("/api/ai/config/test", javascript)
        self.assertIn('id="aiConfigApiKey"', html)
        self.assertIn('AI API 快速接入', html)


class AiEngineConfigTest(unittest.TestCase):
    """The settings file is isolated from SQLite and never leaks its secret."""

    def test_save_and_clear_private_config_refresh_runtime_without_returning_key(self):
        import app.engine as engine

        config_dir = tempfile.TemporaryDirectory()
        config_path = os.path.join(config_dir.name, 'ai-config.env')
        original_env = {key: os.environ.get(key) for key in engine._AI_CONFIG_KEYS}
        try:
            for key in engine._AI_CONFIG_KEYS:
                os.environ.pop(key, None)
            with mock.patch.object(engine, '_AI_CONFIG_FILE', config_path), \
                    mock.patch.object(engine, '_AI_CONFIG_ENV_BASELINE', {key: None for key in engine._AI_CONFIG_KEYS}):
                saved = engine.save_ai_config({
                    'backend': 'openai',
                    'api_key': 'unit-test-secret',
                    'base_url': 'https://api.example.test/v1',
                    'model': 'test-model',
                })
                self.assertTrue(saved['api_key_configured'])
                self.assertNotIn('unit-test-secret', json.dumps(saved, ensure_ascii=False))
                self.assertEqual(os.stat(config_path).st_mode & 0o777, 0o600)
                self.assertIn('unit-test-secret', Path(config_path).read_text(encoding='utf-8'))
                self.assertEqual(engine.OPENAI_API_KEY, 'unit-test-secret')

                cleared = engine.clear_ai_config()
                self.assertFalse(cleared['api_key_configured'])
                self.assertFalse(os.path.exists(config_path))
                self.assertEqual(engine.OPENAI_API_KEY, '')
        finally:
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            engine._runtime_ai_config()
            config_dir.cleanup()

    def test_image_recognition_reuses_the_shared_connection(self):
        import app.engine as engine

        config_dir = tempfile.TemporaryDirectory()
        config_path = os.path.join(config_dir.name, 'ai-config.env')
        original_env = {key: os.environ.get(key) for key in engine._AI_CONFIG_KEYS}
        try:
            for key in engine._AI_CONFIG_KEYS:
                os.environ.pop(key, None)
            with mock.patch.object(engine, '_AI_CONFIG_FILE', config_path), \
                    mock.patch.object(engine, '_AI_CONFIG_ENV_BASELINE', {key: None for key in engine._AI_CONFIG_KEYS}):
                os.environ['VISION_API_KEY'] = 'legacy-vision-secret'
                engine.save_ai_config({
                    'backend': 'openai',
                    'api_key': 'shared-test-secret',
                    'base_url': 'https://api.example.test/v1',
                    'model': 'vision-test-model',
                })
                response = mock.Mock()
                response.json.return_value = {'choices': [{'message': {'content': '识别成功'}}]}
                with mock.patch.object(engine.requests, 'post', return_value=response) as request:
                    result = engine.extract_text_from_image('data:image/png;base64,AAAA')
                self.assertEqual(result, '识别成功')
                request.assert_called_once()
                args, kwargs = request.call_args
                self.assertEqual(args[0], 'https://api.example.test/v1/chat/completions')
                self.assertEqual(kwargs['headers']['Authorization'], 'Bearer shared-test-secret')
                self.assertEqual(kwargs['json']['model'], 'vision-test-model')
                self.assertEqual(kwargs['json']['messages'][0]['content'][1]['image_url']['url'], 'data:image/png;base64,AAAA')
        finally:
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            engine._runtime_ai_config()
            config_dir.cleanup()

    def test_list_ai_models_reads_openai_compatible_pending_config(self):
        import app.engine as engine

        config_dir = tempfile.TemporaryDirectory()
        config_path = os.path.join(config_dir.name, 'ai-config.env')
        original_env = {key: os.environ.get(key) for key in engine._AI_CONFIG_KEYS}
        try:
            for key in engine._AI_CONFIG_KEYS:
                os.environ.pop(key, None)
            with mock.patch.object(engine, '_AI_CONFIG_FILE', config_path), \
                    mock.patch.object(engine, '_AI_CONFIG_ENV_BASELINE', {key: None for key in engine._AI_CONFIG_KEYS}):
                response = mock.Mock()
                response.json.return_value = {
                    'object': 'list',
                    'data': [
                        {'id': 'model-a'}, {'id': 'model-a'}, {'id': 'model-b'},
                        {'name': 'model-c'}, {'id': 'bad\nmodel'},
                    ],
                }
                with mock.patch.object(engine.requests, 'get', return_value=response) as request:
                    result = engine.list_ai_models({
                        'backend': 'openai',
                        'api_key': 'pending-secret',
                        'base_url': 'https://api.example.test/v1',
                    })
                self.assertTrue(result['success'])
                self.assertEqual(result['models'], [
                    {'id': 'model-a'}, {'id': 'model-b'}, {'id': 'model-c'},
                ])
                request.assert_called_once_with(
                    'https://api.example.test/v1/models',
                    headers={
                        'Accept': 'application/json',
                        'Authorization': 'Bearer pending-secret',
                    },
                    timeout=20,
                )
                self.assertNotIn('pending-secret', json.dumps(result, ensure_ascii=False))
        finally:
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            engine._runtime_ai_config()
            config_dir.cleanup()

    def test_list_ai_models_uses_ollama_tags_without_api_key(self):
        import app.engine as engine

        config_dir = tempfile.TemporaryDirectory()
        config_path = os.path.join(config_dir.name, 'ai-config.env')
        original_env = {key: os.environ.get(key) for key in engine._AI_CONFIG_KEYS}
        try:
            for key in engine._AI_CONFIG_KEYS:
                os.environ.pop(key, None)
            with mock.patch.object(engine, '_AI_CONFIG_FILE', config_path), \
                    mock.patch.object(engine, '_AI_CONFIG_ENV_BASELINE', {key: None for key in engine._AI_CONFIG_KEYS}):
                response = mock.Mock()
                response.json.return_value = {'models': [
                    {'name': 'qwen2.5:7b', 'model': 'qwen2.5:7b'},
                    {'name': 'llama3.2'},
                ]}
                with mock.patch.object(engine.requests, 'get', return_value=response) as request:
                    result = engine.list_ai_models({
                        'backend': 'ollama',
                        'base_url': 'http://localhost:11434',
                    })
                self.assertTrue(result['success'])
                self.assertEqual(result['models'], [
                    {'id': 'qwen2.5:7b'}, {'id': 'llama3.2'},
                ])
                request.assert_called_once_with(
                    'http://localhost:11434/api/tags',
                    headers={'Accept': 'application/json'},
                    timeout=20,
                    proxies={'http': None, 'https': None},
                )
        finally:
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            engine._runtime_ai_config()
            config_dir.cleanup()


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
