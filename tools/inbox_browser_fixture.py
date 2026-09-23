"""Deterministic Inbox-only fixture for the Tabbit acceptance script.

It runs only with ``CRM_ENV=rehearsal`` and uses the normal canonical domain
writer; no HTTP test endpoint or production behaviour is introduced.
"""
from pathlib import Path
import importlib.util
import json
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

app_spec = importlib.util.spec_from_file_location('trosa_inbox_browser_fixture_app', ROOT / 'app.py')
app_module = importlib.util.module_from_spec(app_spec)
app_spec.loader.exec_module(app_module)

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
        prospect_source_id = 'inbox-browser-prospect-1'
        profile = conn.execute(
            '''SELECT customer_id FROM trosa.agent_prospect_profiles
               WHERE organization_id=trosa.compat_org_id() AND legacy_user_id='hamid'
                 AND source='sela' AND source_id=? LIMIT 1''',
            (prospect_source_id,),
        ).fetchone()
        if profile:
            cold_customer_id = int(profile['customer_id'])
            trosa_domain.update_customer(conn, customer_id=cold_customer_id, values={
                'business_stage': '', 'business_role': '', 'customer_judgment': '',
            })
        else:
            cold_customer_id = trosa_domain.create_customer(
                conn,
                values={
                    'name': 'Inbox Browser Prospect',
                    'company': 'Inbox Browser Prospect',
                    'country': 'US',
                    'website': 'https://inbox-browser-prospect.example/',
                    'field': 'acrylic sheet',
                    'industry': 'fabrication',
                    'import_source': 'inbox-browser-fixture',
                    'business_stage': '', 'business_role': '', 'customer_judgment': '',
                },
            )
            with app_module.app.app_context():
                app_module._sela_upsert_profile(
                    conn, cold_customer_id, prospect_source_id,
                    {'contact': {}, 'email': '', 'outreach_status': '', 'subject': '', 'email_draft': '',
                     'gmail_draft_id': '', 'gmail_thread_id': '', 'sent_at': ''},
                    '2026-09-24 00:00:00',
                )
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
        sela_request = {
            'source_id': prospect_source_id,
            'session_id': 'inbox-browser-session-1',
            'kind': 'DECISION',
            'severity': 'AMBER',
            'company': 'Inbox Browser Prospect',
            'proposal': '请确认后续研究方向。',
            'decision': {
                'question': '下一步先做什么？',
                'options': ['先补齐联系人事实', '继续整理公开来源'],
                'recommended': '继续整理公开来源',
            },
            'evidence': [{'source': '官网', 'quote': '提供 acrylic sheet 产品目录。'}],
            'resume': '读取你的选择后继续准备研究摘要；对外联系仍需人工确认。',
        }
        result['Sela 需要确认 prospect 的下一步'] = trosa_domain.create_inbox_item(
            conn, item_type='sela_agent_request', title='Sela 需要确认 prospect 的下一步',
            content='公司：Inbox Browser Prospect\n类型：DECISION\n优先级：AMBER\n请确认后续研究方向。',
            customer_id=cold_customer_id, dedupe_key=f'{KEY}:9', status='open',
            created_at='2026-09-21 09:00:09', question_kind='sela_request',
            question_key=f'{KEY}:9', source_type='sela', request_json=json.dumps(sela_request, ensure_ascii=False),
        )
        unlinked_sela_request = {
            'session_id': 'inbox-browser-session-unlinked',
            'kind': 'DECISION',
            'severity': 'AMBER',
            'company': 'Unlinked Inbox Prospect',
            'proposal': '请确认后续研究方向。',
            'decision': {
                'question': '下一步先做什么？',
                'options': ['先补齐联系人事实', '继续整理公开来源'],
                'recommended': '继续整理公开来源',
            },
            'evidence': [{'source': '官网', 'quote': '提供 acrylic sheet 产品目录。'}],
            'resume': '读取你的选择后继续准备研究摘要；对外联系仍需人工确认。',
        }
        result['Sela 请求但未关联 prospect'] = trosa_domain.create_inbox_item(
            conn, item_type='sela_agent_request', title='Sela 请求但未关联 prospect',
            content='公司：Unlinked Inbox Prospect\n类型：DECISION\n优先级：AMBER\n请确认后续研究方向。',
            customer_id=None, dedupe_key=f'{KEY}:10', status='open',
            created_at='2026-09-21 09:00:10', question_kind='sela_request',
            question_key=f'{KEY}:10', source_type='sela', request_json=json.dumps(unlinked_sela_request, ensure_ascii=False),
        )
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
