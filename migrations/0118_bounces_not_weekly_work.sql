-- Bounces are delivery facts, not weekly work.
--
-- Sela's reply handoff used to store every bounce ("outreach_bounced") as a
-- reported timeline entry, so each one showed up in the weekly "实际工作" with an
-- internal status line ("自动处理：REPLY；事件：BOUNCED；意图：UNKNOWN") as its
-- result.  The same mailbox bouncing twice in one day also left two entries.
--
-- 1. Repeated bounces of one mailbox on one day keep the earliest entry; the
--    rest are soft-deleted (is_deleted flag, the same soft delete the member's
--    own "delete record" uses, restorable through /api/follow-history/<id>/restore).
-- 2. Every remaining bounce is taken out of the weekly report and its result is
--    replaced by a plain statement.  Nothing else about the entry changes.
--
-- Idempotent; no rows are physically deleted.
BEGIN;

WITH bounce AS (
    SELECT event.id, event.account_id,
           trosa.compat_local_date(event.occurred_at) AS day,
           lower(substring(coalesce(event.content, '') FROM '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')) AS email,
           row_number() OVER (
               PARTITION BY event.account_id, trosa.compat_local_date(event.occurred_at),
                            lower(substring(coalesce(event.content, '') FROM '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'))
               ORDER BY event.created_at, event.id
           ) AS position
      FROM trosa.timeline_events event
     WHERE event.event_type = 'outreach_bounced'
       AND coalesce(event.payload->>'is_deleted', '0') NOT IN ('1', 'true')
)
UPDATE trosa.timeline_events event
   SET payload = coalesce(event.payload, '{}'::jsonb)
                 || jsonb_build_object('is_deleted', true, 'is_reported', false,
                                       'deleted_reason', 'duplicate_bounce')
  FROM bounce
 WHERE event.id = bounce.id AND bounce.email IS NOT NULL AND bounce.email <> '' AND bounce.position > 1;

UPDATE trosa.timeline_events
   SET payload = coalesce(payload, '{}'::jsonb) || jsonb_build_object('is_reported', false),
       result = '邮箱无效，待换邮箱'
 WHERE event_type = 'outreach_bounced';

COMMIT;
