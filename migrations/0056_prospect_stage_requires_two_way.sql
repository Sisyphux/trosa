-- Tighten the prospect-stage boundary to genuine two-way interaction.
--
-- 0055 treated any non-system recorded communication as "engaged", so Customers
-- with an imported/legacy outbound note (or any logged one-way contact) kept
-- their routine "联系 X" follow-ups in Today even though nothing had ever come
-- back.  Those Customers are still new-customer development / unreplied
-- development follow-ups and belong to Sela's periodic cadence, not to Today.
--
-- The relationship fact now requires the other side to have actually engaged:
-- an inbound or two-way communication, an inbound reply to an outreach, or an
-- unprocessed inbound reply.  A one-way record never qualifies.
--
-- The Today view calls trosa.account_has_real_interaction dynamically, so
-- replacing the function is enough to change the projection; existing
-- prospect-stage follow-ups are then closed with the same audit marker.
--
-- Idempotent and non-destructive: only the function body and open follow-up
-- status/completed_at/legacy_payload change; rows are never deleted.
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
