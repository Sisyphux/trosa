"""Isolated contract tests for the existing-customer sela workflow."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import db

ROOT = Path(__file__).resolve().parents[1]

class SelaFollowUpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_DIR
        db.DB_DIR = self.tmp.name
        db.init_all_dbs()
        spec = importlib.util.spec_from_file_location('trosa_followup_test', ROOT / 'app.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.backup = patch.object(self.module, 'schedule_safety_backup')
        self.backup.start()
        self.ui = self.module.app.test_client()
        self.ui.post('/api/auth/login', json={'user': 'hamid'})
        token = self.ui.post('/api/integrations/prospecting-lab/token').get_json()['token']
        self.agent = self.module.app.test_client()
        self.headers = {'Authorization': 'Bearer ' + token, 'X-Idempotency-Key': 'event-1'}
        self.conn = sqlite3.connect(db.get_user_db_path('hamid'))
        self.conn.execute("INSERT INTO customers(name,company,notes) VALUES ('Buyer','Existing Co','Keep this')")
        self.cid = self.conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.backup.stop()
        db.DB_DIR = self.old
        self.tmp.cleanup()

    def context(self):
        response = self.agent.get(f'/api/integrations/sela/customers/{self.cid}/context', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def submit(self, action='create_task', payload=None):
        self.body = {'customer_id': self.cid, 'revision': self.context()['revision'], 'assessment': '事实：用户要求下周确认样品。',
                     'evidence': [{'source': '用户本次说明', 'quote': '9 月 10 日确认样品'}],
                     'action': action, 'payload': payload or {'title': '确认样品', 'due_date': '2026-09-10'}}
        return self.agent.post('/api/integrations/sela/follow-up', json=self.body, headers=self.headers)

    def test_token_scope_idempotency_and_human_confirmation(self):
        response = self.submit()
        self.assertEqual(response.status_code, 201, response.get_json())
        pid = response.get_json()['proposal_id']
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM reminders').fetchone()[0], 0)
        repeat = self.agent.post('/api/integrations/sela/follow-up', json=self.body, headers=self.headers)
        self.assertEqual(repeat.get_json()['proposal_id'], pid)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM inbox_items').fetchone()[0], 1)
        self.assertEqual(self.agent.post(f'/api/agent/proposals/{pid}/confirm', headers=self.headers).status_code, 401)
        self.body['assessment'] = 'changed'
        self.assertEqual(self.agent.post('/api/integrations/sela/follow-up', json=self.body, headers=self.headers).status_code, 409)
        confirmed = self.ui.post(f'/api/agent/proposals/{pid}/confirm')
        self.assertEqual(confirmed.status_code, 200, confirmed.get_json())
        self.assertTrue(confirmed.get_json()['undo_token'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM reminders').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT status FROM inbox_items').fetchone()[0], 'resolved')
        self.ui.post(f'/api/agent/proposals/{pid}/confirm')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM reminders').fetchone()[0], 1)

    def test_stale_context_blocks_confirmation_and_cannot_be_overwritten(self):
        response = self.submit()
        pid = response.get_json()['proposal_id']
        self.conn.execute("UPDATE customers SET notes='new facts' WHERE id=?", (self.cid,)); self.conn.commit()
        edit = dict(self.body['payload'], _sela_revision=self.context()['revision'])
        self.assertEqual(self.ui.put(f'/api/agent/proposals/{pid}', json=edit).status_code, 200)
        response = self.ui.post(f'/api/agent/proposals/{pid}/confirm')
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM reminders').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT status FROM agent_proposals').fetchone()[0], 'pending')
        self.assertEqual(self.ui.post(f'/api/agent/proposals/{pid}/cancel').status_code, 200)
        self.assertEqual(self.conn.execute('SELECT status FROM inbox_items').fetchone()[0], 'resolved')

    def test_communication_and_next_task_use_atomic_writer(self):
        response = self.submit('record_communication', {'content': '用户确认样品收到', 'follow_date': '2026-09-05',
                         'direction': 'inbound', 'next_task': '确认测试结果', 'next_follow_up': '2026-09-10'})
        self.assertEqual(response.status_code, 201, response.get_json())
        response = self.ui.post('/api/agent/proposals/%s/confirm' % response.get_json()['proposal_id'])
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM follow_up_logs').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM reminders').fetchone()[0], 1)

    def test_profile_update_is_reviewable_and_undoable(self):
        response = self.submit('update_customer', {'notes': 'Keep this; sample received'})
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(self.conn.execute('SELECT notes FROM customers').fetchone()[0], 'Keep this')
        response = self.ui.post('/api/agent/proposals/%s/confirm' % response.get_json()['proposal_id'])
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()['undo_token'])
        self.assertEqual(self.conn.execute('SELECT notes FROM customers').fetchone()[0], 'Keep this; sample received')

    def test_complete_task_records_result_and_profile_undo_restores_data(self):
        created = self.submit()
        self.assertEqual(self.ui.post('/api/agent/proposals/%s/confirm' % created.get_json()['proposal_id']).status_code, 200)
        task_id = self.conn.execute('SELECT id FROM reminders').fetchone()[0]
        self.headers['X-Idempotency-Key'] = 'complete-1'
        proposed = self.submit('complete_task', {'task_id': task_id, 'content': '客户确认测试通过'})
        self.assertEqual(proposed.status_code, 201, proposed.get_json())
        result = self.ui.post('/api/agent/proposals/%s/confirm' % proposed.get_json()['proposal_id'])
        self.assertEqual(result.status_code, 200, result.get_json())
        self.assertEqual(self.conn.execute('SELECT is_done FROM reminders').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM follow_up_logs').fetchone()[0], 1)
        self.assertEqual(self.ui.post('/api/undo/' + result.get_json()['undo_token']).status_code, 200)
        self.assertEqual(self.conn.execute('SELECT is_done FROM reminders').fetchone()[0], 0)

    def test_privacy_validation_and_changed_submission(self):
        anon = self.module.app.test_client()
        self.assertEqual(anon.get('/api/integrations/sela/customers').status_code, 401)
        amy = self.module.app.test_client(); amy.post('/api/auth/login', json={'user': 'amy'})
        self.assertEqual(amy.get(f'/api/integrations/sela/customers/{self.cid}/context').status_code, 404)
        response = self.submit(payload={'title': 'Confirm', 'due_date': 'not-a-date'})
        self.assertEqual(response.status_code, 400)
        self.body['payload']['due_date'] = '2026-09-10'
        self.conn.execute("UPDATE customers SET notes='changed'"); self.conn.commit()
        self.assertEqual(self.agent.post('/api/integrations/sela/follow-up', json=self.body, headers=self.headers).status_code, 409)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM agent_proposals').fetchone()[0], 0)

if __name__ == '__main__':
    unittest.main()
