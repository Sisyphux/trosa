-- Keep the SQLite-shaped customer compatibility view writable after the
-- canonical Customer State facts were split out of legacy_payload.
--
-- ``trade_os_compat.customers`` intentionally does not expose the modern
-- business_stage/business_role/customer_judgment columns.  Trigger records
-- are therefore read through to_jsonb(NEW), which works for both the old
-- compatibility view and the richer trosa.customers projection without
-- making either view a second source of truth.
BEGIN;

CREATE OR REPLACE FUNCTION trosa.compat_customers_ref_payload_write()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_user text := trosa.compat_current_user();
    v_customer_id bigint;
    v_account_id uuid;
    v_pinned_at timestamptz;
    v_new jsonb;
    v_business_stage text := '';
    v_business_role text := '';
    v_customer_judgment text := '';
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

    v_new := to_jsonb(NEW);
    v_customer_id := NULLIF(v_new->>'id', '')::bigint;
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

    v_business_stage := coalesce(v_new->>'business_stage', '');
    v_business_role := coalesce(v_new->>'business_role', '');
    v_customer_judgment := coalesce(v_new->>'customer_judgment', '');
    -- The legacy compatibility view deliberately omits these state fields.
    -- Preserve the canonical values for a partial legacy update; only the
    -- modern trosa.customers projection can intentionally clear or replace
    -- them by carrying the keys in NEW.
    IF NOT (v_new ? 'business_stage') OR NOT (v_new ? 'business_role')
       OR NOT (v_new ? 'customer_judgment') THEN
        SELECT business_stage, business_role, customer_judgment
          INTO v_business_stage, v_business_role, v_customer_judgment
          FROM trosa.customer_states
         WHERE organization_id = trosa.compat_org_id()
           AND legacy_user_id = v_user
           AND legacy_customer_id = v_customer_id;
        v_business_stage := CASE WHEN v_new ? 'business_stage' THEN coalesce(v_new->>'business_stage', '') ELSE coalesce(v_business_stage, '') END;
        v_business_role := CASE WHEN v_new ? 'business_role' THEN coalesce(v_new->>'business_role', '') ELSE coalesce(v_business_role, '') END;
        v_customer_judgment := CASE WHEN v_new ? 'customer_judgment' THEN coalesce(v_new->>'customer_judgment', '') ELSE coalesce(v_customer_judgment, '') END;
    END IF;
    v_pinned_at := trosa.compat_time(v_new->>'pinned_at');

    INSERT INTO trosa.customer_states
        (organization_id, legacy_user_id, legacy_customer_id, account_id,
         business_stage, business_role, customer_judgment, updated_at)
    VALUES
        (trosa.compat_org_id(), v_user, v_customer_id, v_account_id,
         v_business_stage, v_business_role, v_customer_judgment, now())
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
        (v_account_id, coalesce(v_new->>'notes', ''), coalesce(v_new->>'system_notes', ''),
         coalesce(v_new->>'import_source', ''), coalesce(v_new->>'external_source', ''),
         coalesce(v_new->>'external_id', ''), v_pinned_at,
         lower(coalesce(v_new->>'manual_next_follow', '0')) IN ('1', 'true'), now())
    ON CONFLICT (account_id) DO UPDATE
       SET notes = excluded.notes,
           system_notes = excluded.system_notes,
           import_source = excluded.import_source,
           external_source = excluded.external_source,
           external_id = excluded.external_id,
           pinned_at = excluded.pinned_at,
           manual_next_task = excluded.manual_next_task,
           updated_at = now();

    -- This payload exists solely for old-view recovery and rollback. Modern
    -- reads resolve the above fields from customer_details and customer_states.
    UPDATE trosa.account_legacy_refs
       SET legacy_payload = coalesce(legacy_payload, '{}'::jsonb)
                          || (v_new - ARRAY['business_stage', 'business_role', 'customer_judgment'])
     WHERE organization_id = trosa.compat_org_id()
       AND legacy_user_id = v_user
       AND legacy_customer_id = v_customer_id;
    UPDATE trosa.accounts
       SET legacy_payload = coalesce(legacy_payload, '{}'::jsonb)
                           - ARRAY['business_stage', 'business_role', 'customer_judgment']
     WHERE id = v_account_id;
    RETURN NEW;
END $$;

COMMIT;
