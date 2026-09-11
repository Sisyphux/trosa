-- Formal read models for present-day Trosa.  These views read canonical
-- PostgreSQL facts directly; the SQLite-shaped projections remain below this
-- boundary only for legacy HTTP and recovery paths.
BEGIN;

CREATE OR REPLACE VIEW trosa.customer_tasks AS
SELECT ref.legacy_customer_id AS customer_id,
       row_ref.legacy_id AS id,
       task.title, task.content, task.reason,
       trosa.compat_local_date(task.due_at) AS due_date,
       task.status, task.task_type, task.source_activity_legacy_id,
       task.manual_order, task.completed_at::text AS completed_at,
       task.created_at::text AS created_at
  FROM trosa.tasks task
  JOIN trosa.account_legacy_refs ref ON ref.account_id=task.account_id
  JOIN trosa.legacy_row_refs row_ref ON row_ref.target_id=task.id
       AND row_ref.table_name='reminders'
       AND row_ref.organization_id=ref.organization_id
       AND row_ref.legacy_user_id=ref.legacy_user_id
 WHERE ref.organization_id=trosa.compat_org_id()
   AND ref.legacy_user_id=trosa.compat_current_user()
   AND task.task_type NOT LIKE 'outreach_%';

CREATE OR REPLACE VIEW trosa.customer_interactions AS
SELECT ref.legacy_customer_id AS customer_id,
       row_ref.legacy_id AS id,
       'communication'::text AS kind,
       trosa.compat_local_date(event.occurred_at) AS occurred_on,
       event.event_type AS activity_type, event.direction,
       event.content, event.result, event.next_plan,
       event.source_module AS source,
       COALESCE((event.payload->>'is_reported')::boolean, false) AS is_reported,
       ''::text AS delivery_status, ''::text AS reply_date,
       event.created_at::text AS created_at
  FROM trosa.timeline_events event
  JOIN trosa.account_legacy_refs ref ON ref.account_id=event.account_id
  JOIN trosa.legacy_row_refs row_ref ON row_ref.target_id=event.id
       AND row_ref.table_name='follow_up_logs'
       AND row_ref.organization_id=ref.organization_id
       AND row_ref.legacy_user_id=ref.legacy_user_id
 WHERE ref.organization_id=trosa.compat_org_id()
   AND ref.legacy_user_id=trosa.compat_current_user()
   AND COALESCE(event.payload->>'is_deleted', '0') NOT IN ('1', 'true')
UNION ALL
SELECT ref.legacy_customer_id AS customer_id,
       row_ref.legacy_id AS id,
       'email'::text AS kind,
       trosa.compat_local_date(message.sent_at) AS occurred_on,
       'outreach_email'::text AS activity_type, 'outbound'::text AS direction,
       message.subject AS content, message.reply_content AS result,
       ''::text AS next_plan, COALESCE(NULLIF(message.provider,''), 'gmail_delivery') AS source,
       COALESCE((message.legacy_payload->>'is_reported')::boolean, false) AS is_reported,
       message.reply_status AS delivery_status,
       COALESCE(trosa.compat_local_date(message.reply_at), '') AS reply_date,
       message.created_at::text AS created_at
  FROM trosa.outreach_messages message
  JOIN trosa.account_legacy_refs ref ON ref.account_id=message.account_id
  JOIN trosa.legacy_row_refs row_ref ON row_ref.target_id=message.id
       AND row_ref.table_name='outreach_emails'
       AND row_ref.organization_id=ref.organization_id
       AND row_ref.legacy_user_id=ref.legacy_user_id
 WHERE ref.organization_id=trosa.compat_org_id()
   AND ref.legacy_user_id=trosa.compat_current_user();

COMMIT;
