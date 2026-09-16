-- Repair pinned highlights hidden by the 0030 user-scoped projection.
--
-- Before 0030 the Customer list read is_pinned/pinned_order/pinned_at from
-- the shared canonical account, so every pin/unpin wrote only trosa.accounts
-- (and trosa.customer_details).  Migration 0030 started preferring each
-- caller's own account_legacy_refs.legacy_payload snapshot, which still
-- carries the stale import-time pin values.  Every historical pin therefore
-- vanished from the list until the write path learned to mirror pin actions
-- into the payload (trosa_domain.update_customer_priority and the
-- /api/customers/priority/order endpoint now write both sides).
--
-- This migration is forward-only.  It copies the three pin snapshot keys
-- only when an account has exactly one Customer reference.  A shared account
-- has no authoritative per-user pin value in its canonical columns, so
-- copying it to every reference would turn one user's highlight into another
-- user's state.  Archive, name, company and every other snapshot key keep
-- the 0030 per-user isolation untouched.
BEGIN;

UPDATE trosa.account_legacy_refs ref
   SET legacy_payload = coalesce(ref.legacy_payload, '{}'::jsonb)
     || jsonb_build_object(
          'is_pinned', '1',
          'pinned_order', a.pinned_order::text,
          'pinned_at', coalesce(trosa.compat_local_date(d.pinned_at), '')
        )
  FROM trosa.accounts a
  LEFT JOIN trosa.customer_details d ON d.account_id = a.id
 WHERE ref.account_id = a.id
   AND a.is_pinned
   AND a.deleted_at IS NULL
   AND NOT EXISTS (
       SELECT 1 FROM trosa.account_legacy_refs other_ref
        WHERE other_ref.organization_id = ref.organization_id
          AND other_ref.account_id = ref.account_id
          AND (other_ref.legacy_user_id <> ref.legacy_user_id
               OR other_ref.legacy_customer_id <> ref.legacy_customer_id)
   )
   AND lower(coalesce(ref.legacy_payload->>'is_pinned', '0')) NOT IN ('1', 'true');

COMMIT;
