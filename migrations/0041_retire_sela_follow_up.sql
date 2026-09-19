-- Retire the Sela existing-customer follow-up interface.
--
-- Sela's second business line is human fact input, not customer follow-up.
-- The bridge endpoints GET /api/integrations/sela/customers,
-- GET /api/integrations/sela/customers/<id>/context and
-- POST /api/integrations/sela/follow-up are gone, so any pending proposal,
-- open Inbox item or idempotency receipt they created is now orphaned runtime
-- state.  This migration clears that state.  Real CRM writes that were already
-- confirmed live in the business timeline/tasks and are not touched.
BEGIN;

-- 1. Remove Sela follow-up Inbox items: they can no longer be reviewed,
--    confirmed or cancelled through the retired interface.
DELETE FROM trosa.inbox_items
 WHERE item_type = 'sela_follow_up';

-- 2. Remove Sela follow-up proposal shells.  Confirmed CRM writes remain in
--    the business tables; only the retired interface's proposal rows are gone.
DELETE FROM audit.agent_proposals
 WHERE source = 'sela_follow_up';

-- 3. Remove the retired endpoint's idempotency receipts.
DELETE FROM audit.integration_receipts
 WHERE integration = 'sela'
   AND idempotency_key LIKE 'followup:%';

COMMIT;
