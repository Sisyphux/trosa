-- Keep PostgreSQL compatibility projections in the date-shaped format used by
-- the legacy SQLite API.  The application compares these fields with
-- YYYY-MM-DD values for Today, upcoming tasks, communication matching, and
-- customer summaries; exposing timestamptz::text makes same-day rows sort
-- after the date boundary and silently disappear from those queries.

BEGIN;

CREATE OR REPLACE FUNCTION trosa.compat_local_date(value timestamptz)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN $1 IS NULL THEN ''
        ELSE to_char($1 AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD')
    END
$$;

CREATE OR REPLACE VIEW trosa.customers AS
SELECT r.legacy_customer_id AS id,
       a.display_name AS name,
       c.canonical_name AS company,
       c.country_code AS country,
       a.priority_level AS level,
       a.channel_type AS type,
       c.website,
       a.profile,
       a.field,
       a.account_status AS status,
       COALESCE(a.legacy_payload->>'notes', '') AS notes,
       COALESCE(a.legacy_payload->>'system_notes', '') AS system_notes,
       CASE WHEN a.last_contact_at IS NOT NULL
            THEN trosa.compat_local_date(a.last_contact_at)
            ELSE COALESCE(a.legacy_payload->>'last_contact', '') END AS last_contact,
       CASE WHEN a.next_follow_up_at IS NOT NULL
            THEN trosa.compat_local_date(a.next_follow_up_at)
            ELSE COALESCE(a.legacy_payload->>'next_follow_up', '') END AS next_follow_up,
       CASE WHEN lower(COALESCE(a.legacy_payload->>'manual_next_follow','0')) IN ('1','true') THEN 1 ELSE 0 END AS manual_next_follow,
       a.customer_type,
       a.industry,
       a.company_size,
       a.annual_revenue,
       a.tags,
       COALESCE(a.legacy_payload->>'import_source', 'legacy') AS import_source,
       COALESCE(a.legacy_payload->>'external_source', '') AS external_source,
       COALESCE(a.legacy_payload->>'external_id', '') AS external_id,
       a.attention_state,
       a.attention_reason,
       CASE WHEN a.attention_updated_at IS NOT NULL
            THEN a.attention_updated_at::text ELSE COALESCE(a.legacy_payload->>'attention_updated_at', '') END AS attention_updated_at,
       CASE WHEN a.attention_review_date IS NOT NULL
            THEN a.attention_review_date::text ELSE COALESCE(a.legacy_payload->>'attention_review_date', '') END AS attention_review_date,
       CASE WHEN a.is_pinned THEN 1 ELSE 0 END AS is_pinned,
       a.pinned_order,
       COALESCE(a.legacy_payload->>'pinned_at', '') AS pinned_at,
       CASE WHEN a.deleted_at IS NULL THEN 0 ELSE 1 END AS is_deleted,
       COALESCE(a.deleted_at::text, '') AS deleted_at,
       a.created_at::text AS created_at,
       a.updated_at::text AS updated_at
FROM trosa.account_legacy_refs r
JOIN trosa.accounts a ON a.id=r.account_id
JOIN core.companies c ON c.id=a.company_id
WHERE r.organization_id=trosa.compat_org_id()
  AND r.legacy_user_id=trosa.compat_current_user();

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
  AND lr.table_name='outreach_emails';

CREATE OR REPLACE VIEW trade_os_compat.reminders AS SELECT * FROM trosa.reminders;
CREATE OR REPLACE VIEW trade_os_compat.follow_up_logs AS SELECT * FROM trosa.follow_up_logs;
CREATE OR REPLACE VIEW trade_os_compat.outreach_emails AS SELECT * FROM trosa.outreach_emails;

COMMIT;
