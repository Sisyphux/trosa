"""Deterministic Inbox-only fixture for the Tabbit acceptance script.

It runs only with ``CRM_ENV=rehearsal`` and uses the normal canonical domain
writer; no HTTP test endpoint or production behaviour is introduced.
"""
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if os.environ.get('CRM_ENV') != 'rehearsal':
    raise SystemExit('Inbox browser fixture is rehearsal-only')

from tools.postgres_rehearsal import load_fixture
import db
import trosa_domain

KEY = 'inbox-browser-specialist-v1'


def load():
    ids = load_fixture()
    db.set_db_user('hamid')
    conn = db.get_db()
    try:
        # This is an isolated rehearsal database. Close unrelated leftovers
        # from prior integration exercises so the visible count is deterministic.
        conn.execute("UPDATE trosa.inbox_items SET status='resolved', resolved_at=now(), resolution_reason='rehearsal_fixture_reset' WHERE status='open'")
        # The correction scenario starts from a deliberately stale rehearsal
        # mailbox; it is restored by each fixture reload.
        conn.execute('UPDATE trade_os_compat.contacts SET email=? WHERE id=?',
                     ('invalid-mailbox@rehearsal.invalid', ids['contact_id']))
        # Recreate just this explicitly namespaced fixture set on every run.
        conn.execute("DELETE FROM trade_os_compat.inbox_items WHERE dedupe_key LIKE ?", (KEY + ':%',))
        specs = [
            ('fact_request', '文本回答', '请填写可审计的联系人事实。'),
            ('fact_request', '更正失效邮箱', '请为测试联系人提供新的可联系邮箱。'),
            ('investigation_request', '调查证据 CSV', '上传 supported.csv。'),
            ('investigation_request', '调查证据 XLSX', '上传 not_supported.xlsx。'),
            ('investigation_request', '调查证据 PDF', '上传 insufficient.pdf。'),
            ('investigation_request', '调查证据图片', '上传 image_without_ocr.png。'),
            ('investigation_request', '调查证据损坏 PDF', '上传 broken.pdf。'),
            ('approval', '最后一张卡', '请在右侧选择处理决定并说明结果后提交。'),
        ]
        result = {}
        for index, (kind, title, content) in enumerate(specs, 1):
            # Use existing transport kinds so startup reconciliation never
            # downgrades test rows merely because their transport is unknown.
            transport = 'sela_agent_request' if kind == 'investigation_request' else 'customer_reply'
            item_id = trosa_domain.create_inbox_item(
                conn, item_type=transport, title=title, content=content,
                customer_id=ids['customer_id'], dedupe_key=f'{KEY}:{index}', status='open',
                created_at=f'2026-09-21 09:00:0{index}', question_kind=kind,
                question_key=f'{KEY}:{index}', source_type='system')
            result[title] = item_id
        # Response receipts are durable by design.  Remove only receipts whose
        # keys address this namespaced fixture's current rows, so a fixture
        # reset remains repeatable instead of colliding with a prior browser
        # run's idempotency receipt.
        receipt_scope = (KEY + ':%',)
        conn.execute(
            '''DELETE FROM audit.agent_gateway_idempotency receipt
                 WHERE receipt.action='inbox_response'
                   AND EXISTS (
                       SELECT 1 FROM trade_os_compat.inbox_items item
                        WHERE item.dedupe_key LIKE ?
                          AND receipt.idempotency_key LIKE ('inbox-' || item.id::text || '-%')
                   )''', receipt_scope)
        conn.execute(
            '''DELETE FROM trade_os_compat.agent_gateway_rows receipt
                 WHERE receipt.action='inbox_response'
                   AND EXISTS (
                       SELECT 1 FROM trade_os_compat.inbox_items item
                        WHERE item.dedupe_key LIKE ?
                          AND receipt.idempotency_key LIKE ('inbox-' || item.id::text || '-%')
                   )''', receipt_scope)
        conn.commit()
        result.update(ids)
        return result
    finally:
        conn.close()


if __name__ == '__main__':
    import json
    print(json.dumps(load(), ensure_ascii=False))
