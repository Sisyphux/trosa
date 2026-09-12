-- Customer details are a present-day Trosa fact.  The SQLite-shaped customer
-- view is retained for HTTP compatibility, so its final write trigger mirrors
-- only the remaining detail fields here after the canonical account bridge has
-- resolved the account id.  Product readers never need legacy_payload.
BEGIN;

CREATE OR REPLACE FUNCTION trosa.compat_customers_ref_payload_write()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_user text := trosa.compat_current_user();
    v_customer_id bigint;
    v_account_id uuid;
    v_pinned_at timestamptz;
BEGIN
    IF TG_OP = 'DELETE' THEN
        UPDATE trosa.account_legacy_refs
           SET legacy_payload = coalesce(legacy_payload, '{}'::jsonb)
               || jsonb_build_object('is_deleted', 1, 'deleted_at', now()::text)
         WHERE organization_id = trosa.compat_org_id()
           AND legacy_user_id = v_user
           AND legacy_customer_id = OLD.id;
        RETURN OLD;
    END IF;

    v_customer_id := NEW.id;
    IF TG_OP = 'INSERT' AND coalesce(v_customer_id, 0) = 0
       AND current_setting('trade_os.lastrowid', true) ~ '^[0-9]+$' THEN
        v_customer_id := current_setting('trade_os.lastrowid', true)::bigint;
    END IF;

    SELECT account_id INTO v_account_id
      FROM trosa.account_legacy_refs
     WHERE organization_id = trosa.compat_org_id()
       AND legacy_user_id = v_user
       AND legacy_customer_id = v_customer_id;
    IF v_account_id IS NULL THEN
        RAISE EXCEPTION 'customer % is not visible for user %', v_customer_id, v_user;
    END IF;

    v_pinned_at := trosa.compat_time(NEW.pinned_at);

    INSERT INTO trosa.customer_states
        (organization_id, legacy_user_id, legacy_customer_id, account_id,
         business_stage, business_role, customer_judgment, updated_at)
    VALUES
        (trosa.compat_org_id(), v_user, v_customer_id, v_account_id,
         coalesce(NEW.business_stage, ''), coalesce(NEW.business_role, ''),
         coalesce(NEW.customer_judgment, ''), now())
    ON CONFLICT (organization_id, legacy_user_id, legacy_customer_id) DO UPDATE
       SET account_id = excluded.account_id,
           business_stage = excluded.business_stage,
           business_role = excluded.business_role,
           customer_judgment = excluded.customer_judgment,
           updated_at = now();

    INSERT INTO trosa.customer_details
        (account_id, notes, system_notes, import_source, external_source,
         external_id, pinned_at, manual_next_task, updated_at)
    VALUES
        (v_account_id, coalesce(NEW.notes, ''), coalesce(NEW.system_notes, ''),
         coalesce(NEW.import_source, ''), coalesce(NEW.external_source, ''),
         coalesce(NEW.external_id, ''), v_pinned_at,
         lower(coalesce(NEW.manual_next_follow::text, '0')) IN ('1', 'true'), now())
    ON CONFLICT (account_id) DO UPDATE
       SET notes = excluded.notes,
           system_notes = excluded.system_notes,
           import_source = excluded.import_source,
           external_source = excluded.external_source,
           external_id = excluded.external_id,
           pinned_at = excluded.pinned_at,
           manual_next_task = excluded.manual_next_task,
           updated_at = now();

    -- This payload exists solely for old-view recovery and rollback.  Modern
    -- reads resolve the above fields from customer_details and customer_states.
    UPDATE trosa.account_legacy_refs
       SET legacy_payload = coalesce(to_jsonb(NEW), '{}'::jsonb)
                         - ARRAY['business_stage', 'business_role', 'customer_judgment']
     WHERE organization_id = trosa.compat_org_id()
       AND legacy_user_id = v_user
       AND legacy_customer_id = v_customer_id;
    UPDATE trosa.accounts
       SET legacy_payload = coalesce(legacy_payload, '{}'::jsonb)
                         - ARRAY['business_stage', 'business_role', 'customer_judgment']
     WHERE id = v_account_id;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS zz_customers_ref_payload_write ON trosa.customers;
CREATE TRIGGER zz_customers_ref_payload_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trosa.customers
FOR EACH ROW EXECUTE FUNCTION trosa.compat_customers_ref_payload_write();

DROP TRIGGER IF EXISTS zz_customers_ref_payload_write ON trade_os_compat.customers;
CREATE TRIGGER zz_customers_ref_payload_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.customers
FOR EACH ROW EXECUTE FUNCTION trosa.compat_customers_ref_payload_write();

COMMIT;
