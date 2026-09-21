-- Customer source is a durable, user-authored fact: how this customer first
-- entered Trosa (exhibition, customs data, LinkedIn, Google/website search,
-- Sela, referral, existing resource, Excel/history import, or other) plus an
-- optional short clarification. It is never inferred or backfilled: an
-- existing customer without a reliable source stays empty.
--
-- The canonical column lives in trosa.customer_details. Like notes and
-- import_source, a per-user edit is carried on account_legacy_refs.legacy_payload
-- so two users sharing one company Account cannot silently overwrite each
-- other's stated source. The modern read model prefers the caller's own payload
-- and otherwise falls back to the shared canonical column.
BEGIN;

ALTER TABLE trosa.customer_details ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT '';
ALTER TABLE trosa.customer_details ADD COLUMN IF NOT EXISTS source_detail text NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS trosa_customer_details_source_idx
    ON trosa.customer_details (source)
    WHERE source <> '';

-- Modern Customer read model: append source/source_detail after pinned_at so
-- the existing compatibility columns and their order are untouched.
CREATE OR REPLACE VIEW trosa.customer_records AS
SELECT ref.legacy_customer_id AS id, a.id AS account_id,
       CASE WHEN ref.legacy_payload ? 'name' THEN coalesce(ref.legacy_payload->>'name', '')
            ELSE a.display_name END AS name,
       CASE WHEN ref.legacy_payload ? 'company' THEN coalesce(ref.legacy_payload->>'company', '')
            ELSE c.canonical_name END AS company,
       CASE WHEN ref.legacy_payload ? 'country' THEN coalesce(ref.legacy_payload->>'country', '')
            ELSE c.country_code END AS country,
       CASE WHEN ref.legacy_payload ? 'level' THEN coalesce(ref.legacy_payload->>'level', '')
            ELSE a.priority_level END AS level,
       CASE WHEN ref.legacy_payload ? 'type' THEN coalesce(ref.legacy_payload->>'type', '')
            ELSE a.channel_type END AS customer_type,
       CASE WHEN ref.legacy_payload ? 'website' THEN coalesce(ref.legacy_payload->>'website', '')
            ELSE c.website END AS website,
       CASE WHEN ref.legacy_payload ? 'profile' THEN coalesce(ref.legacy_payload->>'profile', '')
            ELSE a.profile END AS profile,
       CASE WHEN ref.legacy_payload ? 'field' THEN coalesce(ref.legacy_payload->>'field', '')
            ELSE a.field END AS field,
       CASE WHEN ref.legacy_payload ? 'industry' THEN coalesce(ref.legacy_payload->>'industry', '')
            ELSE a.industry END AS industry,
       CASE WHEN ref.legacy_payload ? 'company_size' THEN coalesce(ref.legacy_payload->>'company_size', '')
            ELSE a.company_size END AS company_size,
       CASE WHEN ref.legacy_payload ? 'annual_revenue' THEN coalesce(ref.legacy_payload->>'annual_revenue', '')
            ELSE a.annual_revenue END AS annual_revenue,
       CASE WHEN ref.legacy_payload ? 'tags' THEN coalesce(ref.legacy_payload->>'tags', '')
            ELSE a.tags END AS tags,
       CASE WHEN ref.legacy_payload ? 'status' THEN coalesce(ref.legacy_payload->>'status', '')
            ELSE a.account_status END AS status,
       CASE WHEN ref.legacy_payload ? 'notes' THEN coalesce(ref.legacy_payload->>'notes', '')
            ELSE coalesce(d.notes, '') END AS notes,
       CASE WHEN ref.legacy_payload ? 'system_notes' THEN coalesce(ref.legacy_payload->>'system_notes', '')
            ELSE coalesce(d.system_notes, '') END AS system_notes,
       CASE WHEN ref.legacy_payload ? 'import_source' THEN coalesce(ref.legacy_payload->>'import_source', '')
            ELSE coalesce(d.import_source, '') END AS import_source,
       CASE WHEN ref.legacy_payload ? 'external_source' THEN coalesce(ref.legacy_payload->>'external_source', '')
            ELSE coalesce(d.external_source, '') END AS external_source,
       CASE WHEN ref.legacy_payload ? 'external_id' THEN coalesce(ref.legacy_payload->>'external_id', '')
            ELSE coalesce(d.external_id, '') END AS external_id,
       CASE WHEN lower(coalesce(
                    CASE WHEN ref.legacy_payload ? 'is_pinned'
                         THEN ref.legacy_payload->>'is_pinned' END,
                    CASE WHEN a.is_pinned THEN '1' ELSE '0' END)) IN ('1', 'true')
            THEN true ELSE false END AS is_pinned,
       CASE WHEN coalesce(ref.legacy_payload->>'pinned_order', '') ~ '^-?[0-9]+$'
            THEN (ref.legacy_payload->>'pinned_order')::integer
            ELSE a.pinned_order END AS pinned_order,
       CASE WHEN lower(coalesce(ref.legacy_payload->>'is_deleted',
                    CASE WHEN a.deleted_at IS NULL THEN '0' ELSE '1' END)) IN ('1', 'true')
            THEN coalesce(NULLIF(ref.legacy_payload->>'deleted_at', '')::timestamptz,
                          a.deleted_at, now())
            ELSE NULL END AS deleted_at,
       a.created_at, a.updated_at,
       coalesce(s.business_stage, '') AS business_stage,
       coalesce(s.business_role, '') AS business_role,
       coalesce(s.customer_judgment, '') AS customer_judgment,
       CASE WHEN lower(coalesce(
                    CASE WHEN ref.legacy_payload ? 'manual_next_follow'
                         THEN ref.legacy_payload->>'manual_next_follow' END,
                    CASE WHEN d.manual_next_task THEN '1' ELSE '0' END)) IN ('1', 'true')
            THEN true ELSE false END AS manual_next_task,
       CASE WHEN ref.legacy_payload ? 'last_contact' THEN coalesce(ref.legacy_payload->>'last_contact', '')
            WHEN a.last_contact_at IS NOT NULL THEN trosa.compat_local_date(a.last_contact_at)
            ELSE coalesce(a.legacy_payload->>'last_contact', '') END AS last_interaction_on,
       CASE WHEN ref.legacy_payload ? 'next_follow_up' THEN coalesce(ref.legacy_payload->>'next_follow_up', '')
            WHEN a.next_follow_up_at IS NOT NULL THEN trosa.compat_local_date(a.next_follow_up_at)
            ELSE coalesce(a.legacy_payload->>'next_follow_up', '') END AS next_task_on,
       CASE WHEN ref.legacy_payload ? 'pinned_at' THEN coalesce(ref.legacy_payload->>'pinned_at', '')
            ELSE trosa.compat_local_date(d.pinned_at) END AS pinned_at,
       CASE WHEN ref.legacy_payload ? 'source' THEN coalesce(ref.legacy_payload->>'source', '')
            ELSE coalesce(d.source, '') END AS source,
       CASE WHEN ref.legacy_payload ? 'source_detail' THEN coalesce(ref.legacy_payload->>'source_detail', '')
            ELSE coalesce(d.source_detail, '') END AS source_detail
  FROM trosa.account_legacy_refs ref
  JOIN trosa.accounts a ON a.id=ref.account_id
  JOIN core.companies c ON c.id=a.company_id
  LEFT JOIN trosa.customer_details d ON d.account_id=a.id
  LEFT JOIN trosa.customer_states s ON s.organization_id=ref.organization_id
       AND s.legacy_user_id=ref.legacy_user_id AND s.legacy_customer_id=ref.legacy_customer_id
 WHERE ref.organization_id=trosa.compat_org_id()
   AND ref.legacy_user_id=trosa.compat_current_user();

-- The SQLite-shaped compatibility view stays an API/rollback boundary. Append
-- source/source_detail after the existing columns; modern readers use
-- trosa.customer_records above.
CREATE OR REPLACE VIEW trosa.customers AS
SELECT r.legacy_customer_id AS id,
       CASE WHEN r.legacy_payload ? 'name' THEN coalesce(r.legacy_payload->>'name', '') ELSE a.display_name END AS name,
       CASE WHEN r.legacy_payload ? 'company' THEN coalesce(r.legacy_payload->>'company', '') ELSE c.canonical_name END AS company,
       CASE WHEN r.legacy_payload ? 'country' THEN coalesce(r.legacy_payload->>'country', '') ELSE c.country_code END AS country,
       CASE WHEN r.legacy_payload ? 'level' THEN coalesce(r.legacy_payload->>'level', '') ELSE a.priority_level END AS level,
       CASE WHEN r.legacy_payload ? 'type' THEN coalesce(r.legacy_payload->>'type', '') ELSE a.channel_type END AS type,
       CASE WHEN r.legacy_payload ? 'website' THEN coalesce(r.legacy_payload->>'website', '') ELSE c.website END AS website,
       CASE WHEN r.legacy_payload ? 'profile' THEN coalesce(r.legacy_payload->>'profile', '') ELSE a.profile END AS profile,
       CASE WHEN r.legacy_payload ? 'field' THEN coalesce(r.legacy_payload->>'field', '') ELSE a.field END AS field,
       CASE WHEN r.legacy_payload ? 'status' THEN coalesce(r.legacy_payload->>'status', '') ELSE a.account_status END AS status,
       coalesce(r.legacy_payload->>'notes', a.legacy_payload->>'notes', '') AS notes,
       coalesce(r.legacy_payload->>'system_notes', a.legacy_payload->>'system_notes', '') AS system_notes,
       CASE WHEN r.legacy_payload ? 'last_contact' THEN coalesce(r.legacy_payload->>'last_contact', '') WHEN a.last_contact_at IS NOT NULL THEN trosa.compat_local_date(a.last_contact_at) ELSE coalesce(a.legacy_payload->>'last_contact', '') END AS last_contact,
       CASE WHEN r.legacy_payload ? 'next_follow_up' THEN coalesce(r.legacy_payload->>'next_follow_up', '') WHEN a.next_follow_up_at IS NOT NULL THEN trosa.compat_local_date(a.next_follow_up_at) ELSE coalesce(a.legacy_payload->>'next_follow_up', '') END AS next_follow_up,
       CASE WHEN lower(coalesce(r.legacy_payload->>'manual_next_follow', a.legacy_payload->>'manual_next_follow', '0')) IN ('1', 'true') THEN 1 ELSE 0 END AS manual_next_follow,
       CASE WHEN r.legacy_payload ? 'customer_type' THEN coalesce(r.legacy_payload->>'customer_type', '') ELSE a.customer_type END AS customer_type,
       a.industry, a.company_size, a.annual_revenue, a.tags,
       coalesce(r.legacy_payload->>'import_source', a.legacy_payload->>'import_source', 'legacy') AS import_source,
       coalesce(r.legacy_payload->>'external_source', a.legacy_payload->>'external_source', '') AS external_source,
       coalesce(r.legacy_payload->>'external_id', a.legacy_payload->>'external_id', '') AS external_id,
       CASE WHEN r.legacy_payload ? 'attention_state' THEN coalesce(r.legacy_payload->>'attention_state', '') ELSE a.attention_state END AS attention_state,
       CASE WHEN r.legacy_payload ? 'attention_reason' THEN coalesce(r.legacy_payload->>'attention_reason', '') ELSE a.attention_reason END AS attention_reason,
       coalesce(a.attention_updated_at::text, r.legacy_payload->>'attention_updated_at', '') AS attention_updated_at,
       coalesce(a.attention_review_date::text, r.legacy_payload->>'attention_review_date', '') AS attention_review_date,
       CASE WHEN lower(coalesce(r.legacy_payload->>'is_pinned', CASE WHEN a.is_pinned THEN '1' ELSE '0' END)) IN ('1', 'true') THEN 1 ELSE 0 END AS is_pinned,
       a.pinned_order, coalesce(r.legacy_payload->>'pinned_at', '') AS pinned_at,
       CASE WHEN lower(coalesce(r.legacy_payload->>'is_deleted', CASE WHEN a.deleted_at IS NULL THEN '0' ELSE '1' END)) IN ('1', 'true') THEN 1 ELSE 0 END AS is_deleted,
       coalesce(r.legacy_payload->>'deleted_at', a.deleted_at::text, '') AS deleted_at,
       a.created_at::text AS created_at, a.updated_at::text AS updated_at,
       coalesce(s.business_stage, '') AS business_stage,
       coalesce(s.business_role, '') AS business_role,
       coalesce(s.customer_judgment, '') AS customer_judgment,
       CASE WHEN r.legacy_payload ? 'source' THEN coalesce(r.legacy_payload->>'source', '')
            ELSE coalesce(d.source, '') END AS source,
       CASE WHEN r.legacy_payload ? 'source_detail' THEN coalesce(r.legacy_payload->>'source_detail', '')
            ELSE coalesce(d.source_detail, '') END AS source_detail
  FROM trosa.account_legacy_refs r JOIN trosa.accounts a ON a.id=r.account_id
  JOIN core.companies c ON c.id=a.company_id
  LEFT JOIN trosa.customer_details d ON d.account_id=a.id
  LEFT JOIN trosa.customer_states s ON s.organization_id=r.organization_id AND s.legacy_user_id=r.legacy_user_id AND s.legacy_customer_id=r.legacy_customer_id
  WHERE r.organization_id=trosa.compat_org_id() AND r.legacy_user_id=trosa.compat_current_user();

-- Refresh the SQLite-shaped adapter view so its explicit column list also
-- exposes the appended source columns to the compatibility write trigger.
CREATE OR REPLACE VIEW trade_os_compat.customers AS SELECT * FROM trosa.customers;

-- The final compatibility trigger also mirrors source/source_detail into
-- customer_details for legacy-shaped writes. A partial legacy update that does
-- not carry the keys preserves the canonical value instead of clearing it.
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
         external_id, source, source_detail, pinned_at, manual_next_task, updated_at)
    VALUES
        (v_account_id, coalesce(v_new->>'notes', ''), coalesce(v_new->>'system_notes', ''),
         coalesce(v_new->>'import_source', ''), coalesce(v_new->>'external_source', ''),
         coalesce(v_new->>'external_id', ''), coalesce(v_new->>'source', ''),
         coalesce(v_new->>'source_detail', ''), v_pinned_at,
         lower(coalesce(v_new->>'manual_next_follow', '0')) IN ('1', 'true'), now())
    ON CONFLICT (account_id) DO UPDATE
       SET notes = excluded.notes,
           system_notes = excluded.system_notes,
           import_source = excluded.import_source,
           external_source = excluded.external_source,
           external_id = excluded.external_id,
           source = CASE WHEN v_new ? 'source' THEN excluded.source ELSE trosa.customer_details.source END,
           source_detail = CASE WHEN v_new ? 'source_detail' THEN excluded.source_detail ELSE trosa.customer_details.source_detail END,
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
