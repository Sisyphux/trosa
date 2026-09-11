-- Modern customer classifications are user-scoped business facts. They must
-- not be hidden in a SQLite-shaped compatibility JSON document.
BEGIN;

CREATE TABLE IF NOT EXISTS trosa.customer_states (
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    legacy_user_id text NOT NULL,
    legacy_customer_id bigint NOT NULL,
    account_id uuid NOT NULL REFERENCES trosa.accounts(id),
    business_stage text NOT NULL DEFAULT '' CHECK (business_stage IN ('', '成交', '流失')),
    business_role text NOT NULL DEFAULT '' CHECK (business_role IN ('', '中间商', '终端')),
    customer_judgment text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, legacy_user_id, legacy_customer_id),
    FOREIGN KEY (organization_id, legacy_user_id, legacy_customer_id)
        REFERENCES trosa.account_legacy_refs(organization_id, legacy_user_id, legacy_customer_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS trosa_customer_states_account_idx
    ON trosa.customer_states (account_id, legacy_user_id);

-- Backfill only durable classifications. Old lifecycle fields remain
-- recoverable in legacy_payload but do not become a running state machine.
INSERT INTO trosa.customer_states
       (organization_id, legacy_user_id, legacy_customer_id, account_id,
        business_stage, business_role, customer_judgment)
SELECT ref.organization_id, ref.legacy_user_id, ref.legacy_customer_id, ref.account_id,
       CASE WHEN coalesce(ref.legacy_payload->>'business_stage', ref.legacy_payload->>'status', '') IN ('成交', '流失')
            THEN coalesce(ref.legacy_payload->>'business_stage', ref.legacy_payload->>'status', '') ELSE '' END,
       CASE WHEN coalesce(ref.legacy_payload->>'business_role', ref.legacy_payload->>'type', '') IN ('中间商', '终端')
            THEN coalesce(ref.legacy_payload->>'business_role', ref.legacy_payload->>'type', '') ELSE '' END,
       CASE WHEN coalesce(ref.legacy_payload->>'customer_judgment', '') <> ''
            THEN ref.legacy_payload->>'customer_judgment'
            WHEN coalesce(ref.legacy_payload->>'attention_state', '')='custom'
            THEN coalesce(ref.legacy_payload->>'attention_reason', '') ELSE '' END
  FROM trosa.account_legacy_refs ref
ON CONFLICT (organization_id, legacy_user_id, legacy_customer_id) DO NOTHING;

-- The legacy SQL view remains an API boundary; modern fields project from the
-- explicit state table rather than legacy_payload.
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
       coalesce(s.business_stage, '') AS business_stage, coalesce(s.business_role, '') AS business_role,
       coalesce(s.customer_judgment, '') AS customer_judgment,
       CASE WHEN lower(coalesce(r.legacy_payload->>'is_pinned', CASE WHEN a.is_pinned THEN '1' ELSE '0' END)) IN ('1', 'true') THEN 1 ELSE 0 END AS is_pinned,
       a.pinned_order, coalesce(r.legacy_payload->>'pinned_at', '') AS pinned_at,
       CASE WHEN lower(coalesce(r.legacy_payload->>'is_deleted', CASE WHEN a.deleted_at IS NULL THEN '0' ELSE '1' END)) IN ('1', 'true') THEN 1 ELSE 0 END AS is_deleted,
       coalesce(r.legacy_payload->>'deleted_at', a.deleted_at::text, '') AS deleted_at,
       a.created_at::text AS created_at, a.updated_at::text AS updated_at
FROM trosa.account_legacy_refs r JOIN trosa.accounts a ON a.id=r.account_id
JOIN core.companies c ON c.id=a.company_id
LEFT JOIN trosa.customer_states s ON s.organization_id=r.organization_id AND s.legacy_user_id=r.legacy_user_id AND s.legacy_customer_id=r.legacy_customer_id
WHERE r.organization_id=trosa.compat_org_id() AND r.legacy_user_id=trosa.compat_current_user();

-- The old bridge writes the surrounding compatibility row. This final trigger
-- writes classifications only to customer_states and strips them from payloads.
CREATE OR REPLACE FUNCTION trosa.compat_customers_ref_payload_write()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_user text := trosa.compat_current_user(); v_customer_id bigint; v_account_id uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        UPDATE trosa.account_legacy_refs SET legacy_payload=coalesce(legacy_payload, '{}'::jsonb)||jsonb_build_object('is_deleted', 1, 'deleted_at', now()::text)
         WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=v_user AND legacy_customer_id=OLD.id;
        RETURN OLD;
    END IF;
    v_customer_id := NEW.id;
    IF TG_OP='INSERT' AND coalesce(v_customer_id, 0)=0 AND current_setting('trade_os.lastrowid', true) ~ '^[0-9]+$' THEN v_customer_id := current_setting('trade_os.lastrowid', true)::bigint; END IF;
    SELECT account_id INTO v_account_id FROM trosa.account_legacy_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=v_user AND legacy_customer_id=v_customer_id;
    IF v_account_id IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %', v_customer_id, v_user; END IF;
    INSERT INTO trosa.customer_states (organization_id, legacy_user_id, legacy_customer_id, account_id, business_stage, business_role, customer_judgment, updated_at)
    VALUES (trosa.compat_org_id(), v_user, v_customer_id, v_account_id, coalesce(NEW.business_stage, ''), coalesce(NEW.business_role, ''), coalesce(NEW.customer_judgment, ''), now())
    ON CONFLICT (organization_id, legacy_user_id, legacy_customer_id) DO UPDATE SET account_id=excluded.account_id, business_stage=excluded.business_stage, business_role=excluded.business_role, customer_judgment=excluded.customer_judgment, updated_at=now();
    UPDATE trosa.account_legacy_refs SET legacy_payload=coalesce(to_jsonb(NEW), '{}'::jsonb)-ARRAY['business_stage', 'business_role', 'customer_judgment']
     WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=v_user AND legacy_customer_id=v_customer_id;
    UPDATE trosa.accounts SET legacy_payload=coalesce(legacy_payload, '{}'::jsonb)-ARRAY['business_stage', 'business_role', 'customer_judgment'] WHERE id=v_account_id;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS zz_customers_ref_payload_write ON trosa.customers;
CREATE TRIGGER zz_customers_ref_payload_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trosa.customers FOR EACH ROW EXECUTE FUNCTION trosa.compat_customers_ref_payload_write();
DROP TRIGGER IF EXISTS zz_customers_ref_payload_write ON trade_os_compat.customers;
CREATE TRIGGER zz_customers_ref_payload_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.customers FOR EACH ROW EXECUTE FUNCTION trosa.compat_customers_ref_payload_write();
COMMIT;
