-- An armed delivery holds an organization-wide identity/ownership freeze until
-- an explicit terminal provider outcome. This is intentionally conservative.
BEGIN;
CREATE TABLE audit.sela_send_barriers (
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    send_id uuid NOT NULL,
    requested_by text NOT NULL,
    identity_hash text NOT NULL,
    recipient_hash text NOT NULL,
    token text NOT NULL,
    state text NOT NULL CHECK (state IN ('PREPARED','ARMED','SENT','CONFIRMED_NOT_SENT')),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, send_id)
);
CREATE INDEX sela_send_barriers_armed_idx ON audit.sela_send_barriers (organization_id) WHERE state='ARMED';
CREATE OR REPLACE FUNCTION trosa.sela_send_barrier_lock(p_org uuid)
RETURNS void LANGUAGE sql VOLATILE AS $$
    SELECT pg_advisory_xact_lock(hashtextextended('sela-send-barrier:' || p_org::text, 0));
$$;
CREATE OR REPLACE FUNCTION trosa.sela_send_barrier_write_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_operation text := coalesce(current_setting('trosa.sela_send_barrier_operation', true), '');
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'SELA_SEND_BARRIER_DIRECT_WRITE: barrier history cannot be truncated' USING ERRCODE='23514';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'SELA_SEND_BARRIER_DIRECT_WRITE: barrier records are immutable' USING ERRCODE='23514';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF v_operation <> 'prepare' OR NEW.state <> 'PREPARED' THEN
            RAISE EXCEPTION 'SELA_SEND_BARRIER_DIRECT_WRITE: barrier insert requires prepare operation' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.organization_id IS DISTINCT FROM OLD.organization_id
       OR NEW.send_id IS DISTINCT FROM OLD.send_id
       OR NEW.requested_by IS DISTINCT FROM OLD.requested_by
       OR NEW.identity_hash IS DISTINCT FROM OLD.identity_hash
       OR NEW.recipient_hash IS DISTINCT FROM OLD.recipient_hash
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'SELA_SEND_BARRIER_DIRECT_WRITE: barrier identity is immutable' USING ERRCODE='23514';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state THEN
        IF OLD.state = 'PREPARED' AND NEW.state = 'ARMED' AND v_operation = 'arm' THEN
            NULL;
        ELSIF OLD.state IN ('PREPARED','ARMED')
              AND NEW.state IN ('SENT','CONFIRMED_NOT_SENT')
              AND v_operation = 'finalize' THEN
            NULL;
        ELSE
            RAISE EXCEPTION 'SELA_SEND_BARRIER_DIRECT_WRITE: invalid state transition' USING ERRCODE='23514';
        END IF;
    END IF;
    IF NEW.token IS DISTINCT FROM OLD.token
       AND NOT (OLD.state = 'PREPARED' AND v_operation = 'prepare') THEN
        RAISE EXCEPTION 'SELA_SEND_BARRIER_DIRECT_WRITE: token rotation requires prepare operation' USING ERRCODE='23514';
    END IF;
    IF NEW.expires_at IS DISTINCT FROM OLD.expires_at
       AND OLD.state = 'PREPARED' AND v_operation <> 'prepare' THEN
        RAISE EXCEPTION 'SELA_SEND_BARRIER_DIRECT_WRITE: prepared expiry requires prepare operation' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sela_send_barrier_write_guard
BEFORE INSERT OR UPDATE OR DELETE ON audit.sela_send_barriers
FOR EACH ROW EXECUTE FUNCTION trosa.sela_send_barrier_write_guard();
CREATE TRIGGER sela_send_barrier_truncate_guard
BEFORE TRUNCATE ON audit.sela_send_barriers
FOR EACH STATEMENT EXECUTE FUNCTION trosa.sela_send_barrier_write_guard();
CREATE OR REPLACE FUNCTION trosa.sela_send_barrier_mutation_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_org uuid; v_orgs uuid[] := ARRAY[]::uuid[]; v_company_ids uuid[] := ARRAY[]::uuid[]; v_company record;
BEGIN
    IF TG_TABLE_NAME IN ('accounts', 'companies', 'users') THEN
        IF TG_OP <> 'INSERT' THEN v_orgs := array_append(v_orgs, OLD.organization_id); END IF;
        IF TG_OP <> 'DELETE' THEN v_orgs := array_append(v_orgs, NEW.organization_id); END IF;
    ELSE
        IF TG_OP <> 'INSERT' THEN v_company_ids := array_append(v_company_ids, OLD.company_id); END IF;
        IF TG_OP <> 'DELETE' THEN v_company_ids := array_append(v_company_ids, NEW.company_id); END IF;
        -- Lock parent rows before deriving their organization. A plain SELECT
        -- can use a stale REPEATABLE READ snapshot after a concurrent move and
        -- then acquire the former organization's advisory lock.
        FOR v_company IN
            SELECT company.id, company.organization_id
              FROM core.companies company
             WHERE company.id = ANY(v_company_ids)
             ORDER BY company.id
             FOR UPDATE
        LOOP
            v_orgs := array_append(v_orgs, v_company.organization_id);
        END LOOP;
    END IF;
    FOR v_org IN SELECT DISTINCT x FROM unnest(v_orgs) x WHERE x IS NOT NULL ORDER BY x LOOP
        PERFORM trosa.sela_send_barrier_lock(v_org);
        IF EXISTS (SELECT 1 FROM audit.sela_send_barriers WHERE organization_id=v_org AND state='ARMED') THEN
            RAISE EXCEPTION 'SELA_SEND_BARRIER_ARMED: ownership and company identity are locked' USING ERRCODE='23514';
        END IF;
    END LOOP;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER aa_sela_send_barrier_accounts BEFORE INSERT OR DELETE OR UPDATE OF organization_id, company_id, owner_user_id ON trosa.accounts FOR EACH ROW EXECUTE FUNCTION trosa.sela_send_barrier_mutation_guard();
CREATE TRIGGER aa_sela_send_barrier_companies BEFORE INSERT OR DELETE OR UPDATE OF organization_id, canonical_name, normalized_name, website, identity_status ON core.companies FOR EACH ROW EXECUTE FUNCTION trosa.sela_send_barrier_mutation_guard();
CREATE TRIGGER aa_sela_send_barrier_aliases BEFORE INSERT OR UPDATE OR DELETE ON core.company_aliases FOR EACH ROW EXECUTE FUNCTION trosa.sela_send_barrier_mutation_guard();
CREATE TRIGGER aa_sela_send_barrier_domains BEFORE INSERT OR UPDATE OR DELETE ON core.company_domains FOR EACH ROW EXECUTE FUNCTION trosa.sela_send_barrier_mutation_guard();
-- The snapshot enumerates all organization users and resolves account owners
-- by ID, organization, legacy user ID, and display name.
CREATE TRIGGER aa_sela_send_barrier_users BEFORE INSERT OR DELETE OR UPDATE OF id, organization_id, legacy_user_id, display_name ON identity.users FOR EACH ROW EXECUTE FUNCTION trosa.sela_send_barrier_mutation_guard();
-- TRUNCATE does not fire row triggers. Hold every existing organization lock
-- in a deterministic order before allowing this exceptional bulk operation.
CREATE OR REPLACE FUNCTION trosa.sela_send_barrier_truncate_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_org uuid;
BEGIN
    FOR v_org IN SELECT id FROM identity.organizations ORDER BY id LOOP
        PERFORM trosa.sela_send_barrier_lock(v_org);
    END LOOP;
    IF EXISTS (SELECT 1 FROM audit.sela_send_barriers WHERE state='ARMED') THEN
        RAISE EXCEPTION 'SELA_SEND_BARRIER_ARMED: ownership and company identity are locked' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER aa_sela_send_barrier_truncate_accounts BEFORE TRUNCATE ON trosa.accounts FOR EACH STATEMENT EXECUTE FUNCTION trosa.sela_send_barrier_truncate_guard();
CREATE TRIGGER aa_sela_send_barrier_truncate_companies BEFORE TRUNCATE ON core.companies FOR EACH STATEMENT EXECUTE FUNCTION trosa.sela_send_barrier_truncate_guard();
CREATE TRIGGER aa_sela_send_barrier_truncate_aliases BEFORE TRUNCATE ON core.company_aliases FOR EACH STATEMENT EXECUTE FUNCTION trosa.sela_send_barrier_truncate_guard();
CREATE TRIGGER aa_sela_send_barrier_truncate_domains BEFORE TRUNCATE ON core.company_domains FOR EACH STATEMENT EXECUTE FUNCTION trosa.sela_send_barrier_truncate_guard();
CREATE TRIGGER aa_sela_send_barrier_truncate_users BEFORE TRUNCATE ON identity.users FOR EACH STATEMENT EXECUTE FUNCTION trosa.sela_send_barrier_truncate_guard();
COMMIT;
