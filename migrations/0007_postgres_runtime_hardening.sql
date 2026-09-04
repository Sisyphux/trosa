-- Harden the legacy-shaped PostgreSQL projections after the first production
-- cutover.  The canonical timeline payload already retained the legacy
-- contact/task ids; expose those values again instead of silently returning
-- NULL to the application.

BEGIN;

CREATE OR REPLACE FUNCTION trosa.compat_legacy_bigint(value text)
RETURNS bigint
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    parsed bigint;
BEGIN
    IF NULLIF(trim(coalesce(value, '')), '') IS NULL THEN
        RETURN NULL;
    END IF;
    parsed := trim(value)::bigint;
    IF parsed <= 0 THEN
        RETURN NULL;
    END IF;
    RETURN parsed;
EXCEPTION WHEN others THEN
    -- Legacy payloads are user/import input.  A malformed optional id must not
    -- make the whole compatibility view unreadable.
    RETURN NULL;
END
$$;

CREATE OR REPLACE VIEW trosa.reminders AS
SELECT lr.legacy_id AS id,
       ar.legacy_customer_id AS customer_id,
       t.title,
       t.content,
       t.reason,
       t.due_at::text AS remind_date,
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
  AND lr.legacy_user_id=trosa.compat_current_user() AND lr.table_name='reminders';

CREATE OR REPLACE VIEW trosa.follow_up_logs AS
SELECT lr.legacy_id AS id,
       ar.legacy_customer_id AS customer_id,
       e.content,
       e.occurred_at::text AS follow_date,
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
  AND lr.legacy_user_id=trosa.compat_current_user() AND lr.table_name='follow_up_logs';

CREATE OR REPLACE VIEW trosa.outreach_emails AS
SELECT lr.legacy_id AS id,
       ar.legacy_customer_id AS customer_id,
       o.subject,
       o.body AS content,
       COALESCE(o.sent_at::text, '') AS sent_date,
       o.reply_status,
       o.reply_content,
       COALESCE(o.reply_at::text, '') AS reply_date,
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
  AND lr.legacy_user_id=trosa.compat_current_user() AND lr.table_name='outreach_emails';

-- Keep the compatibility names explicitly tied to the hardened projections.
CREATE OR REPLACE VIEW trade_os_compat.reminders AS SELECT * FROM trosa.reminders;
CREATE OR REPLACE VIEW trade_os_compat.follow_up_logs AS SELECT * FROM trosa.follow_up_logs;
CREATE OR REPLACE VIEW trade_os_compat.outreach_emails AS SELECT * FROM trosa.outreach_emails;

COMMIT;
