-- Today belongs to real relationships; prospect development belongs to Sela.
--
-- New-Customer development and unreplied development follow-ups are Sela's
-- responsibility and must not appear in 今日跟进.  The judgment is a durable
-- relationship fact, never a title keyword, ``customer_type`` or mere presence
-- in the CRM (see trosa.account_has_real_interaction).
--
-- 1. A canonical helper decides whether an account has left the prospect stage:
--    an inbound/two-way communication, an explicitly recorded (non-system)
--    communication, a replied outreach, or an unprocessed inbound reply.  A sent
--    outreach, a delivery event or an internal agent decision is one-way
--    development contact and never qualifies on its own.
-- 2. trosa.today_tasks is redefined to require that fact, so prospect-stage
--    tasks can never enter Today again even if a legacy row is created.
-- 3. Existing historical prospect development tasks are closed with an audit
--    marker (rows retained, nothing deleted) so they leave Today immediately.
--
-- Idempotent and non-destructive: only status / completed_at / legacy_payload of
-- open prospect-stage follow-ups change.
BEGIN;

CREATE OR REPLACE FUNCTION trosa.account_has_real_interaction(p_account_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT
      EXISTS (
          SELECT 1
            FROM trosa.timeline_events event
           WHERE event.account_id = p_account_id
             AND lower(coalesce(event.payload->>'is_deleted', '0')) NOT IN ('1', 'true')
             AND (
                  lower(coalesce(event.direction, '')) IN ('inbound', 'two_way')
                  OR lower(coalesce(event.event_type, '')) = 'customer_reply'
                  OR lower(coalesce(event.source_module, '')) NOT IN
                     ('gmail', 'sela', 'sela_reply_engine', 'sela_agent')
             )
      )
      OR EXISTS (
          SELECT 1
            FROM trosa.outreach_messages message
           WHERE message.account_id = p_account_id
             AND lower(coalesce(message.reply_status, '')) = 'replied'
      )
      OR EXISTS (
          SELECT 1
            FROM trosa.inbox_items item
           WHERE item.account_id = p_account_id
             AND item.status = 'open' AND item.item_type = 'customer_reply'
      );
$$;

-- Same columns as 0038; the only change is the relationship-stage predicate.
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
   AND trosa.account_has_real_interaction(task.account_id)
   AND lower(coalesce(customer_ref.legacy_payload->>'is_deleted',
        CASE WHEN customer.deleted_at IS NULL THEN '0' ELSE '1' END)) NOT IN ('1', 'true')
 ORDER BY task.id, customer_ref.legacy_customer_id, task_ref.legacy_id;

-- Close historical prospect development tasks (new-customer development and
-- unreplied development follow-ups).  They stay as audited rows for history and
-- recovery instead of being deleted or keyword-matched.
UPDATE trosa.tasks task
   SET status='done',
       completed_at=coalesce(task.completed_at, now()),
       legacy_payload=coalesce(task.legacy_payload, '{}'::jsonb) || jsonb_build_object(
           'auto_closed_by', 'prospect_stage_boundary',
           'auto_closed_at', now()::text),
       updated_at=now()
 WHERE task.status='open'
   AND task.task_type='follow_up'
   AND NOT trosa.account_has_real_interaction(task.account_id);

COMMIT;
