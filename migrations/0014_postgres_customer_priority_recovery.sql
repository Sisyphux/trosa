-- Recover attention marks and their explicit order from the preserved SQLite
-- payload.  The initial unified import archived these values but omitted the
-- two projected account columns, which made every customer appear unmarked.
BEGIN;

UPDATE trosa.accounts
   SET is_pinned = lower(coalesce(legacy_payload->>'is_pinned', '0')) IN ('1', 'true', 'yes'),
       pinned_order = CASE
           WHEN coalesce(legacy_payload->>'pinned_order', '') ~ '^[0-9]+$'
             THEN (legacy_payload->>'pinned_order')::integer
           ELSE 0
       END,
       updated_at = now()
 WHERE legacy_payload ? 'is_pinned'
    OR legacy_payload ? 'pinned_order';

-- The customers compatibility view is writable.  Keep its attention fields
-- projected into the account after every view update, so clicking a mark or
-- dragging its order remains durable in PostgreSQL mode.  This trigger is
-- deliberately separate from the identity-resolution trigger: it only owns
-- the three reversible attention fields.
CREATE OR REPLACE FUNCTION trosa.compat_customers_priority_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_legacy_user text := trosa.compat_current_user();
BEGIN
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;

    UPDATE trosa.accounts a
       SET is_pinned = coalesce(NEW.is_pinned, 0) = 1,
           pinned_order = greatest(coalesce(NEW.pinned_order, 0), 0),
           legacy_payload = a.legacy_payload || jsonb_build_object(
               'is_pinned', CASE WHEN coalesce(NEW.is_pinned, 0) = 1 THEN 1 ELSE 0 END,
               'pinned_order', greatest(coalesce(NEW.pinned_order, 0), 0),
               'pinned_at', coalesce(NEW.pinned_at, '')
           ),
           updated_at = now()
     WHERE a.id = (
         SELECT ar.account_id
           FROM trosa.account_legacy_refs ar
          WHERE ar.organization_id = trosa.compat_org_id()
            AND ar.legacy_user_id = v_legacy_user
            AND ar.legacy_customer_id = NEW.id
     );
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS compat_customers_priority_write ON trosa.customers;
CREATE TRIGGER compat_customers_priority_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trosa.customers
FOR EACH ROW EXECUTE FUNCTION trosa.compat_customers_priority_write();

COMMIT;
