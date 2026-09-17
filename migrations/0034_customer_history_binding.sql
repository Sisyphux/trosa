-- Bind every communication, email, and task to exactly one Customer per user.
--
-- account_legacy_refs can hold more than one legacy customer id for the same
-- account after historical customer merges (one canonical company account,
-- several customer records).  The 0021 formal read models joined only on
-- account_id, so one timeline event fanned out into one row per alias and a
-- customer page showed the merged sibling's history (Kaze vs Action Plus).
-- Migration 0033 already stopped the fan-out for Today, but it picked the
-- smallest alias id instead of the record's own customer.
--
-- The stable original binding already exists: the importer stored the raw
-- legacy row in timeline_events.payload / outreach_messages.legacy_payload /
-- tasks.legacy_payload, and every legacy row carried its customer_id.  Runtime
-- writes now store the same key (trosa_domain record_external_interaction,
-- merge_open_task, create_outreach_message).  This migration redefines the
-- customer-history views to read that binding:
--
--   * a row with payload customer_id belongs to that one Customer, and only
--     when that id is one of the caller's aliases for the row's account;
--   * a row without a binding stays visible only while the caller has a
--     single alias for the account, so it can never fan out again;
--   * rows that are ambiguous (missing binding, several aliases) are excluded
--     from customer history and must be surfaced by the boundary audit tool
--     instead of being shown under the wrong Customer.
--
-- Write triggers on the legacy-shaped views are unchanged: they address rows
-- through legacy_row_refs and resolve customer ids through account_legacy_refs.
BEGIN;

CREATE OR REPLACE FUNCTION trosa.compat_customer_binding(
    payload jsonb, organization_id uuid, legacy_user_id text, account_id uuid,
    legacy_customer_id bigint
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN trosa.compat_legacy_bigint(payload->>'customer_id') IS NOT NULL
        THEN trosa.compat_legacy_bigint(payload->>'customer_id') = legacy_customer_id
        ELSE (
            SELECT count(DISTINCT r.legacy_customer_id)
              FROM trosa.account_legacy_refs r
             WHERE r.organization_id=organization_id
               AND r.legacy_user_id=legacy_user_id
               AND r.account_id=account_id
        ) = 1
    END
$$;

CREATE INDEX IF NOT EXISTS trosa_account_legacy_refs_account_scoped_idx
    ON trosa.account_legacy_refs (organization_id, legacy_user_id, account_id);

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
   AND trosa.compat_customer_binding(task.legacy_payload, ref.organization_id,
                                     ref.legacy_user_id, ref.account_id,
                                     ref.legacy_customer_id)
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
   AND trosa.compat_customer_binding(event.payload, ref.organization_id,
                                     ref.legacy_user_id, ref.account_id,
                                     ref.legacy_customer_id)
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
   AND ref.legacy_user_id=trosa.compat_current_user()
   AND trosa.compat_customer_binding(message.legacy_payload, ref.organization_id,
                                     ref.legacy_user_id, ref.account_id,
                                     ref.legacy_customer_id);

-- One canonical task must produce one Today row under its own Customer.
CREATE OR REPLACE VIEW trosa.today_tasks AS
SELECT DISTINCT ON (task.id)
       task_ref.legacy_id AS id, customer_ref.legacy_customer_id AS customer_id,
       task.title, task.content, task.reason, trosa.compat_local_date(task.due_at) AS due_date,
       task.task_type, task.manual_order, customer.display_name AS customer_name,
       company.canonical_name AS customer_company
  FROM trosa.tasks task
  JOIN trosa.account_legacy_refs customer_ref ON customer_ref.account_id=task.account_id
  JOIN trosa.legacy_row_refs task_ref ON task_ref.target_id=task.id
       AND task_ref.table_name='reminders' AND task_ref.organization_id=customer_ref.organization_id
       AND task_ref.legacy_user_id=customer_ref.legacy_user_id
  JOIN trosa.accounts customer ON customer.id=task.account_id
  JOIN core.companies company ON company.id=customer.company_id
 WHERE customer_ref.organization_id=trosa.compat_org_id()
   AND customer_ref.legacy_user_id=trosa.compat_current_user()
   AND trosa.compat_customer_binding(task.legacy_payload, customer_ref.organization_id,
                                     customer_ref.legacy_user_id, customer_ref.account_id,
                                     customer_ref.legacy_customer_id)
   AND task.status='open' AND task.task_type NOT LIKE 'outreach_%'
   AND customer.deleted_at IS NULL
 ORDER BY task.id, customer_ref.legacy_customer_id, task_ref.legacy_id;

-- The SQLite-shaped projections keep their columns and write triggers; only
-- the customer attribution changes so recovery/undo reads the same boundary.
CREATE OR REPLACE VIEW trosa.reminders AS
SELECT lr.legacy_id AS id,
       ar.legacy_customer_id AS customer_id,
       t.title,
       t.content,
       t.reason,
       trosa.compat_local_date(t.due_at) AS remind_date,
       CASE WHEN t.status='done' THEN 1 ELSE 0 END AS is_done,
       t.task_type AS reminder_type,
       COALESCE(t.completed_at::text, '') AS completed_at,
       trosa.compat_legacy_bigint(t.source_activity_legacy_id) AS source_activity_id,
       t.manual_order,
       t.created_at::text AS created_at,
       t.updated_at::text AS updated_at
FROM trosa.legacy_row_refs lr
JOIN trosa.tasks t ON t.id=lr.target_id
JOIN trosa.account_legacy_refs ar ON ar.account_id=t.account_id
 AND ar.organization_id=lr.organization_id AND ar.legacy_user_id=lr.legacy_user_id
WHERE lr.organization_id=trosa.compat_org_id()
  AND lr.legacy_user_id=trosa.compat_current_user()
  AND trosa.compat_customer_binding(t.legacy_payload, ar.organization_id,
                                    ar.legacy_user_id, ar.account_id,
                                    ar.legacy_customer_id)
  AND lr.table_name='reminders';

CREATE OR REPLACE VIEW trosa.follow_up_logs AS
SELECT lr.legacy_id AS id,
       ar.legacy_customer_id AS customer_id,
       e.content,
       trosa.compat_local_date(e.occurred_at) AS follow_date,
       e.result,
       e.next_plan,
       e.event_type AS activity_type,
       e.direction,
       trosa.compat_legacy_bigint(e.payload->>'contact_id') AS contact_id,
       trosa.compat_legacy_bigint(e.payload->>'related_task_id') AS related_task_id,
       e.source_module AS source,
       CASE WHEN lower(COALESCE(e.payload->>'is_reported','0')) IN ('1','true') THEN 1 ELSE 0 END AS is_reported,
       CASE WHEN lower(COALESCE(e.payload->>'is_deleted','0')) IN ('1','true') THEN 1 ELSE 0 END AS is_deleted,
       COALESCE(e.payload->>'deleted_at', '') AS deleted_at,
       e.created_at::text AS updated_at,
       e.created_at::text AS created_at
FROM trosa.legacy_row_refs lr
JOIN trosa.timeline_events e ON e.id=lr.target_id
JOIN trosa.account_legacy_refs ar ON ar.account_id=e.account_id
 AND ar.organization_id=lr.organization_id AND ar.legacy_user_id=lr.legacy_user_id
WHERE lr.organization_id=trosa.compat_org_id()
  AND lr.legacy_user_id=trosa.compat_current_user()
  AND trosa.compat_customer_binding(e.payload, ar.organization_id,
                                    ar.legacy_user_id, ar.account_id,
                                    ar.legacy_customer_id)
  AND lr.table_name='follow_up_logs';

CREATE OR REPLACE VIEW trosa.outreach_emails AS
SELECT lr.legacy_id AS id,
       ar.legacy_customer_id AS customer_id,
       o.subject,
       o.body AS content,
       COALESCE(trosa.compat_local_date(o.sent_at), '') AS sent_date,
       o.reply_status,
       o.reply_content,
       COALESCE(trosa.compat_local_date(o.reply_at), '') AS reply_date,
       CASE WHEN lower(COALESCE(o.legacy_payload->>'is_reported','0')) IN ('1','true') THEN 1 ELSE 0 END AS is_reported,
       o.created_at::text AS created_at,
       COALESCE(o.legacy_payload->>'external_source', '') AS external_source,
       COALESCE(o.provider_message_id, o.legacy_payload->>'external_id', '') AS external_id,
       COALESCE(o.legacy_payload->>'external_updated_at', '') AS external_updated_at,
       COALESCE(o.legacy_payload->>'recipient_email', '') AS recipient_email,
       trosa.compat_legacy_bigint(o.legacy_payload->>'contact_id') AS contact_id,
       COALESCE(o.legacy_payload->>'message_id', o.provider_message_id, '') AS message_id
FROM trosa.legacy_row_refs lr
JOIN trosa.outreach_messages o ON o.id=lr.target_id
JOIN trosa.account_legacy_refs ar ON ar.account_id=o.account_id
 AND ar.organization_id=lr.organization_id AND ar.legacy_user_id=lr.legacy_user_id
WHERE lr.organization_id=trosa.compat_org_id()
  AND lr.legacy_user_id=trosa.compat_current_user()
  AND trosa.compat_customer_binding(o.legacy_payload, ar.organization_id,
                                    ar.legacy_user_id, ar.account_id,
                                    ar.legacy_customer_id)
  AND lr.table_name='outreach_emails';

COMMIT;
