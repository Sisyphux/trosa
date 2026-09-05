-- Final PostgreSQL integrity boundaries discovered by the full migration
-- audit.  This migration keeps the legacy API vocabulary, but makes every
-- shared-database key either organization-scoped or explicitly user-scoped.

BEGIN;

-- A default role of ``member`` in an old identity row must not hide a more
-- authoritative admin role that already exists in memberships.
UPDATE identity.users u
   SET role=m.role, updated_at=now()
  FROM identity.memberships m
 WHERE m.organization_id=u.organization_id
   AND m.user_id=u.id
   AND NULLIF(btrim(m.role), '') IS NOT NULL
   AND u.role IS DISTINCT FROM m.role;

-- Agent audit rows are produced by user-scoped compatibility ledgers.  The
-- original canonical uniqueness constraints were organization-wide, so the
-- same action_id or undo token from two independent SQLite stores could make
-- the second user's audit fact overwrite the first user's fact.  Keep the
-- human-facing legacy key unchanged, but make its canonical owner explicit.
ALTER TABLE audit.agent_actions
    ADD COLUMN IF NOT EXISTS legacy_user_id text;
ALTER TABLE audit.undo_snapshots
    ADD COLUMN IF NOT EXISTS legacy_user_id text;

UPDATE audit.agent_actions a
   SET legacy_user_id=COALESCE(
       (SELECT NULLIF(btrim(u.legacy_user_id),'')
          FROM identity.users u
         WHERE u.organization_id=a.organization_id AND u.id=a.actor_user_id
         LIMIT 1),
       'legacy')
 WHERE NULLIF(btrim(a.legacy_user_id),'') IS NULL;
UPDATE audit.undo_snapshots s
   SET legacy_user_id=COALESCE(
       (SELECT NULLIF(btrim(r.legacy_user_id),'')
          FROM trade_os_compat.undo_action_rows r
         WHERE r.token=s.token
         ORDER BY r.legacy_user_id
         LIMIT 1),
       'legacy')
 WHERE NULLIF(btrim(s.legacy_user_id),'') IS NULL;

ALTER TABLE audit.agent_actions
    ALTER COLUMN legacy_user_id SET DEFAULT 'legacy',
    ALTER COLUMN legacy_user_id SET NOT NULL;
ALTER TABLE audit.undo_snapshots
    ALTER COLUMN legacy_user_id SET DEFAULT 'legacy',
    ALTER COLUMN legacy_user_id SET NOT NULL;

ALTER TABLE audit.agent_actions
    DROP CONSTRAINT IF EXISTS agent_actions_organization_id_action_id_key;
DROP INDEX IF EXISTS audit.agent_actions_organization_id_action_id_key;
ALTER TABLE audit.undo_snapshots
    DROP CONSTRAINT IF EXISTS undo_snapshots_organization_id_token_key;
DROP INDEX IF EXISTS audit.undo_snapshots_organization_id_token_key;
CREATE UNIQUE INDEX IF NOT EXISTS audit_agent_actions_org_user_action_idx
    ON audit.agent_actions (organization_id,legacy_user_id,action_id);
CREATE UNIQUE INDEX IF NOT EXISTS audit_undo_snapshots_org_user_token_idx
    ON audit.undo_snapshots (organization_id,legacy_user_id,token);

-- 0003 created this as a physical table because the compatibility view did
-- not exist yet.  Rename the physical ledger before exposing the user-scoped
-- view.  The merge branch is defensive for a partially repaired rehearsal
-- database.  Exact duplicates may be merged, but a conflicting row aborts
-- the migration instead of being silently discarded.
DO $$
DECLARE
    old_kind "char";
    new_kind "char";
BEGIN
    SELECT c.relkind INTO old_kind
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='trade_os_compat' AND c.relname='integration_sync_receipts';
    SELECT c.relkind INTO new_kind
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='trade_os_compat' AND c.relname='integration_sync_receipt_rows';

    -- A failed rehearsal can leave either relation name as a view.  Remove
    -- those views first so the physical ledger merge below has one stable
    -- destination; no data-bearing table is dropped by this branch.
    IF new_kind IN ('v','m') THEN
        IF new_kind='m' THEN
            DROP MATERIALIZED VIEW trade_os_compat.integration_sync_receipt_rows;
        ELSE
            DROP VIEW trade_os_compat.integration_sync_receipt_rows;
        END IF;
        new_kind := NULL;
    END IF;
    IF old_kind IN ('v','m') THEN
        IF old_kind='m' THEN
            DROP MATERIALIZED VIEW trade_os_compat.integration_sync_receipts;
        ELSE
            DROP VIEW trade_os_compat.integration_sync_receipts;
        END IF;
        old_kind := NULL;
    END IF;

    IF old_kind IN ('r','p') AND new_kind IS NULL THEN
        ALTER TABLE trade_os_compat.integration_sync_receipts
            RENAME TO integration_sync_receipt_rows;
    ELSIF old_kind IN ('r','p') AND new_kind IN ('r','p') THEN
        IF EXISTS (
            SELECT 1
              FROM trade_os_compat.integration_sync_receipts o
              JOIN trade_os_compat.integration_sync_receipt_rows n
                ON n.legacy_user_id=o.legacy_user_id
               AND (n.id=o.id OR (n.integration=o.integration
                                  AND n.idempotency_key=o.idempotency_key))
             WHERE n.id IS DISTINCT FROM o.id
                OR n.integration IS DISTINCT FROM o.integration
                OR n.idempotency_key IS DISTINCT FROM o.idempotency_key
                OR n.request_sha256 IS DISTINCT FROM o.request_sha256
                OR n.candidate_id IS DISTINCT FROM o.candidate_id
                OR n.customer_id IS DISTINCT FROM o.customer_id
                OR n.response_json IS DISTINCT FROM o.response_json
                OR n.created_at IS DISTINCT FROM o.created_at
                OR n.updated_at IS DISTINCT FROM o.updated_at
        ) THEN
            RAISE EXCEPTION
                'Conflicting integration_sync_receipts rows require manual review before migration';
        END IF;
        INSERT INTO trade_os_compat.integration_sync_receipt_rows
            (legacy_user_id,id,integration,idempotency_key,request_sha256,candidate_id,
             customer_id,response_json,created_at,updated_at)
        SELECT o.legacy_user_id,o.id,o.integration,o.idempotency_key,o.request_sha256,
               o.candidate_id,o.customer_id,o.response_json,o.created_at,o.updated_at
          FROM trade_os_compat.integration_sync_receipts o
         WHERE NOT EXISTS (
             SELECT 1 FROM trade_os_compat.integration_sync_receipt_rows n
              WHERE n.legacy_user_id=o.legacy_user_id
                AND (n.id=o.id OR (n.integration=o.integration
                                  AND n.idempotency_key=o.idempotency_key))
         )
         ON CONFLICT DO NOTHING;
        DROP TABLE trade_os_compat.integration_sync_receipts;
    ELSIF old_kind IS NOT NULL AND new_kind IS NULL THEN
        RAISE EXCEPTION 'Unsupported integration_sync_receipts relation kind: %', old_kind;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS trade_os_compat.integration_sync_receipt_rows (
    legacy_user_id text NOT NULL,
    id bigint NOT NULL,
    integration text NOT NULL,
    idempotency_key text NOT NULL,
    request_sha256 text NOT NULL,
    candidate_id text NOT NULL DEFAULT '',
    customer_id bigint,
    response_json text NOT NULL DEFAULT '{}',
    created_at text NOT NULL DEFAULT '',
    updated_at text NOT NULL DEFAULT '',
    PRIMARY KEY (legacy_user_id,id),
    UNIQUE (legacy_user_id,integration,idempotency_key)
);

-- The old application only sees its own receipt keys.  The physical ledger
-- remains per-user so a retry from Amy can never return Hamid's response.
CREATE OR REPLACE VIEW trade_os_compat.integration_sync_receipts AS
SELECT id,integration,idempotency_key,request_sha256,candidate_id,customer_id,
       response_json,created_at,updated_at
  FROM trade_os_compat.integration_sync_receipt_rows
 WHERE legacy_user_id=trosa.compat_current_user();

CREATE OR REPLACE FUNCTION trade_os_compat.integration_sync_receipts_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_user text := trosa.compat_current_user();
    v_org uuid := trosa.compat_org_id();
    v_id bigint;
    v_integration text := btrim(coalesce(NEW.integration,''));
    v_key text := btrim(coalesce(NEW.idempotency_key,''));
    v_account uuid;
    v_created timestamptz;
    v_updated timestamptz;
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM trade_os_compat.integration_sync_receipt_rows
         WHERE legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF v_integration='' OR v_key='' THEN
        RAISE EXCEPTION 'integration and idempotency_key are required';
    END IF;
    IF TG_OP='UPDATE' AND (
        NEW.id IS DISTINCT FROM OLD.id
        OR
        v_integration IS DISTINCT FROM btrim(coalesce(OLD.integration,''))
        OR v_key IS DISTINCT FROM btrim(coalesce(OLD.idempotency_key,''))
    ) THEN
        RAISE EXCEPTION 'integration receipt identity is immutable';
    END IF;

    SELECT id INTO v_id
      FROM trade_os_compat.integration_sync_receipt_rows
     WHERE legacy_user_id=v_user AND integration=v_integration
       AND idempotency_key=v_key
     LIMIT 1;
    IF v_id IS NULL THEN
        v_id := CASE WHEN coalesce(NEW.id,0)<>0 THEN NEW.id
                     ELSE trosa.compat_next_id('integration_sync_receipts',v_user) END;
        IF EXISTS (SELECT 1 FROM trade_os_compat.integration_sync_receipt_rows
                    WHERE legacy_user_id=v_user AND id=v_id
                      AND (integration<>v_integration OR idempotency_key<>v_key)) THEN
            v_id := trosa.compat_next_id('integration_sync_receipts',v_user);
        END IF;
    END IF;
    v_account := trosa.compat_customer_account(NEW.customer_id,v_user);
    v_created := coalesce(trosa.compat_time(NEW.created_at),now());
    v_updated := coalesce(trosa.compat_time(NEW.updated_at),v_created);

    INSERT INTO trade_os_compat.integration_sync_receipt_rows
        (legacy_user_id,id,integration,idempotency_key,request_sha256,candidate_id,
         customer_id,response_json,created_at,updated_at)
    VALUES
        (v_user,v_id,v_integration,v_key,coalesce(NEW.request_sha256,''),
         coalesce(NEW.candidate_id,''),NEW.customer_id,coalesce(NEW.response_json,'{}'),
         v_created::text,v_updated::text)
    ON CONFLICT (legacy_user_id,integration,idempotency_key) DO UPDATE SET
        request_sha256=excluded.request_sha256,candidate_id=excluded.candidate_id,
        customer_id=excluded.customer_id,response_json=excluded.response_json,
        updated_at=excluded.updated_at;

    INSERT INTO audit.integration_receipts
        (id,organization_id,integration,idempotency_key,request_sha256,
         legacy_candidate_id,account_id,response_payload,created_at,updated_at)
    VALUES
        (trosa.compat_uuid('integration-receipt:'||v_user||':'||v_integration||':'||v_key),
         v_org,v_integration,v_user||':'||v_key,coalesce(NEW.request_sha256,''),
         coalesce(NEW.candidate_id,''),v_account,
         trosa.compat_jsonb(NEW.response_json,'{}'::jsonb),v_created,v_updated)
    ON CONFLICT (organization_id,integration,idempotency_key) DO UPDATE SET
        request_sha256=excluded.request_sha256,legacy_candidate_id=excluded.legacy_candidate_id,
        account_id=excluded.account_id,response_payload=excluded.response_payload,
        updated_at=excluded.updated_at;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS integration_sync_receipts_write ON trade_os_compat.integration_sync_receipts;
CREATE TRIGGER integration_sync_receipts_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.integration_sync_receipts
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.integration_sync_receipts_write();

-- Team invitations are system-level rows, so the system connection sees all
-- invitations belonging to this organization.  It must never be able to
-- insert/update a row in a different organization.
CREATE OR REPLACE VIEW trade_os_compat.team_invitations AS
SELECT id,token_hash,created_by,created_at,expires_at,accepted_at,
       accepted_user_id,revoked_at
  FROM identity.team_invitations
 WHERE organization_id=trosa.compat_org_id();

CREATE OR REPLACE FUNCTION trade_os_compat.team_invitations_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_id text := btrim(coalesce(NEW.id,''));
    v_token text := btrim(coalesce(NEW.token_hash,''));
    v_created_by text := btrim(coalesce(NEW.created_by,''));
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM identity.team_invitations
         WHERE organization_id=v_org AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF v_id='' OR v_token='' OR v_created_by='' THEN
        RAISE EXCEPTION 'invitation id, token_hash and created_by are required';
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'invitation identity is immutable';
    END IF;
    IF EXISTS (SELECT 1 FROM identity.team_invitations
                WHERE id=v_id AND organization_id<>v_org) THEN
        RAISE EXCEPTION 'invitation % belongs to another organization',v_id;
    END IF;
    IF EXISTS (SELECT 1 FROM identity.team_invitations
                WHERE token_hash=v_token AND organization_id<>v_org) THEN
        RAISE EXCEPTION 'invitation token belongs to another organization';
    END IF;
    INSERT INTO identity.team_invitations
        (id,organization_id,token_hash,created_by,created_at,expires_at,
         accepted_at,accepted_user_id,revoked_at)
    VALUES
        (v_id,v_org,v_token,v_created_by,coalesce(NEW.created_at,''),
         coalesce(NEW.expires_at,''),coalesce(NEW.accepted_at,''),
         coalesce(NEW.accepted_user_id,''),coalesce(NEW.revoked_at,''))
    ON CONFLICT (id) DO UPDATE SET
        token_hash=excluded.token_hash,created_by=excluded.created_by,
        created_at=excluded.created_at,expires_at=excluded.expires_at,
        accepted_at=excluded.accepted_at,accepted_user_id=excluded.accepted_user_id,
        revoked_at=excluded.revoked_at;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS team_invitations_write ON trade_os_compat.team_invitations;
CREATE TRIGGER team_invitations_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.team_invitations
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.team_invitations_write();

-- Feedback rows are append-only facts.  The old unique key ignored the
-- source batch, so a later export silently overwrote an earlier event with
-- the same row number.
ALTER TABLE sela.prospect_events
    ADD COLUMN IF NOT EXISTS source_batch_id uuid
    NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000'::uuid;

UPDATE sela.prospect_events e
   SET source_batch_id=COALESCE((
       SELECT b.id
         FROM audit.legacy_records r
         JOIN audit.import_batches b ON b.id=r.batch_id
        WHERE r.source_table='sela/feedback_events.json'
          AND r.legacy_row_number=e.legacy_row_number
          AND b.organization_id=e.organization_id
        ORDER BY b.imported_at DESC,b.id DESC
        LIMIT 1
   ),'00000000-0000-0000-0000-000000000000'::uuid)
 WHERE e.source_batch_id='00000000-0000-0000-0000-000000000000'::uuid;

ALTER TABLE sela.prospect_events
    DROP CONSTRAINT IF EXISTS prospect_events_organization_id_legacy_row_number_key;
DROP INDEX IF EXISTS sela.prospect_events_organization_id_legacy_row_number_key;
CREATE UNIQUE INDEX IF NOT EXISTS sela_prospect_events_org_batch_row_idx
    ON sela.prospect_events (organization_id,source_batch_id,legacy_row_number);

-- All compatibility ledgers use the same lock and allocation contract.  The
-- trigger functions below call this helper instead of racing on max(id)+1.
CREATE OR REPLACE FUNCTION trosa.compat_next_id(table_name text, legacy_user text)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE result bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('trade-os:compat-id:'||coalesce($1,'')||':'||coalesce($2,'')));
    IF $1 IN ('reminders','follow_up_logs','outreach_emails','research_reports',
              'external_analysis_notes','customer_understandings','ai_recommendations',
              'inbox_items','web_monitor_logs') THEN
        SELECT coalesce(max(lr.legacy_id),0)+1 INTO result
          FROM trosa.legacy_row_refs lr
         WHERE lr.organization_id=trosa.compat_org_id()
           AND lr.legacy_user_id=$2 AND lr.table_name=$1;
    ELSIF $1='customers' THEN
        SELECT coalesce(max(ar.legacy_customer_id),0)+1 INTO result
          FROM trosa.account_legacy_refs ar
         WHERE ar.organization_id=trosa.compat_org_id() AND ar.legacy_user_id=$2;
    ELSIF $1='contacts' THEN
        SELECT coalesce(max(cr.legacy_contact_id),0)+1 INTO result
          FROM trosa.contact_legacy_refs cr
         WHERE cr.organization_id=trosa.compat_org_id() AND cr.legacy_user_id=$2;
    ELSIF $1='customer_files' THEN
        SELECT coalesce(max(id),0)+1 INTO result
          FROM trade_os_compat.customer_file_rows WHERE legacy_user_id=$2;
    ELSIF $1='operation_logs' THEN
        SELECT coalesce(max(id),0)+1 INTO result FROM trade_os_compat.operation_log_rows WHERE legacy_user_id=$2;
    ELSIF $1='agent_proposals' THEN
        SELECT coalesce(max(id),0)+1 INTO result FROM trade_os_compat.agent_proposal_rows WHERE legacy_user_id=$2;
    ELSIF $1='agent_gateway_idempotency' THEN
        SELECT coalesce(max(id),0)+1 INTO result FROM trade_os_compat.agent_gateway_rows WHERE legacy_user_id=$2;
    ELSIF $1='agent_actions' THEN
        SELECT coalesce(max(id),0)+1 INTO result FROM trade_os_compat.agent_action_rows WHERE legacy_user_id=$2;
    ELSIF $1='undo_actions' THEN
        SELECT coalesce(max(id),0)+1 INTO result FROM trade_os_compat.undo_action_rows WHERE legacy_user_id=$2;
    ELSIF $1='import_batches' THEN
        SELECT coalesce(max(id),0)+1 INTO result FROM trade_os_compat.import_batch_rows WHERE legacy_user_id=$2;
    ELSIF $1='imported_activity_rows' THEN
        SELECT coalesce(max(id),0)+1 INTO result FROM trade_os_compat.imported_activity_row_rows WHERE legacy_user_id=$2;
    ELSIF $1='import_unmatched_customers' THEN
        SELECT coalesce(max(id),0)+1 INTO result FROM trade_os_compat.import_unmatched_customer_rows WHERE legacy_user_id=$2;
    ELSIF $1='email_delivery_events' THEN
        SELECT coalesce(max(id),0)+1 INTO result FROM trade_os_compat.email_delivery_event_rows WHERE legacy_user_id=$2;
    ELSIF $1='gmail_message_states' THEN
        SELECT coalesce(max(id),0)+1 INTO result FROM trade_os_compat.gmail_message_state_rows WHERE legacy_user_id=$2;
    ELSIF $1='communication_sources' THEN
        SELECT coalesce(max(id),0)+1 INTO result FROM trade_os_compat.communication_source_rows WHERE legacy_user_id=$2;
    ELSIF $1='communication_source_items' THEN
        SELECT coalesce(max(id),0)+1 INTO result FROM trade_os_compat.communication_source_item_rows WHERE legacy_user_id=$2;
    ELSIF $1='integration_sync_receipts' THEN
        SELECT coalesce(max(id),0)+1 INTO result FROM trade_os_compat.integration_sync_receipt_rows WHERE legacy_user_id=$2;
    ELSE
        result:=1;
    END IF;
    RETURN result;
END
$$;

-- The older runtime functions used an unprotected max(id)+1 expression.  In
-- addition to the lock in compat_next_id, resolve their natural idempotency
-- keys before allocating a new legacy id.
CREATE OR REPLACE FUNCTION trade_os_compat.operation_logs_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    u text := trosa.compat_current_user();
    lid bigint;
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM trade_os_compat.operation_log_rows
         WHERE legacy_user_id=u AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'operation log identity is immutable';
    END IF;
    lid := CASE WHEN coalesce(NEW.id,0)<>0 THEN NEW.id
                ELSE trosa.compat_next_id('operation_logs',u) END;
    INSERT INTO trade_os_compat.operation_log_rows
        (legacy_user_id,id,action,target_type,target_id,details,created_at,user_id)
    VALUES
        (u,lid,coalesce(NEW.action,''),coalesce(NEW.target_type,''),NEW.target_id,
         coalesce(NEW.details,''),coalesce(NEW.created_at,''),u)
    ON CONFLICT (legacy_user_id,id) DO UPDATE SET
        action=excluded.action,target_type=excluded.target_type,target_id=excluded.target_id,
        details=excluded.details,created_at=excluded.created_at,user_id=excluded.user_id;
    PERFORM trosa.compat_set_lastrowid(lid);
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.agent_proposals_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    u text := trosa.compat_current_user();
    lid bigint;
    account uuid;
    proposal uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        UPDATE audit.agent_proposals
           SET status='cancelled'
         WHERE id=trosa.compat_uuid('agent-proposal:'||u||':'||OLD.id::text)
           AND organization_id=trosa.compat_org_id();
        DELETE FROM trade_os_compat.agent_proposal_rows
         WHERE legacy_user_id=u AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'agent proposal identity is immutable';
    END IF;
    account := trosa.compat_customer_account(NEW.customer_id,u);
    IF account IS NULL THEN
        RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,u;
    END IF;
    lid := CASE WHEN coalesce(NEW.id,0)<>0 THEN NEW.id
                ELSE trosa.compat_next_id('agent_proposals',u) END;
    INSERT INTO trade_os_compat.agent_proposal_rows
        (legacy_user_id,id,proposal_type,customer_id,payload,proposal_action,source,
         source_reference,idempotency_key,request_sha256,status,created_at,confirmed_at)
    VALUES
        (u,lid,coalesce(NEW.proposal_type,''),NEW.customer_id,coalesce(NEW.payload,'{}'),
         coalesce(NEW.proposal_action,''),coalesce(NEW.source,''),coalesce(NEW.source_reference,''),
         coalesce(NEW.idempotency_key,''),coalesce(NEW.request_sha256,''),coalesce(NEW.status,'pending'),
         coalesce(NEW.created_at,''),coalesce(NEW.confirmed_at,''))
    ON CONFLICT (legacy_user_id,id) DO UPDATE SET
        proposal_type=excluded.proposal_type,customer_id=excluded.customer_id,payload=excluded.payload,
        proposal_action=excluded.proposal_action,source=excluded.source,source_reference=excluded.source_reference,
        idempotency_key=excluded.idempotency_key,request_sha256=excluded.request_sha256,
        status=excluded.status,created_at=excluded.created_at,confirmed_at=excluded.confirmed_at;
    proposal := trosa.compat_uuid('agent-proposal:'||u||':'||lid::text);
    INSERT INTO audit.agent_proposals
        (id,organization_id,account_id,proposal_type,payload,proposal_action,source,source_reference,
         idempotency_key,request_sha256,status,created_at,confirmed_at)
    VALUES
        (proposal,trosa.compat_org_id(),account,coalesce(NEW.proposal_type,''),
         trosa.compat_jsonb(NEW.payload,'{}'::jsonb),coalesce(NEW.proposal_action,''),
         coalesce(NEW.source,''),coalesce(NEW.source_reference,''),coalesce(NEW.idempotency_key,''),
         coalesce(NEW.request_sha256,''),coalesce(NEW.status,'pending'),
         coalesce(trosa.compat_time(NEW.created_at),now()),trosa.compat_time(NEW.confirmed_at))
    ON CONFLICT (id) DO UPDATE SET
        account_id=excluded.account_id,proposal_type=excluded.proposal_type,payload=excluded.payload,
        proposal_action=excluded.proposal_action,source=excluded.source,source_reference=excluded.source_reference,
        idempotency_key=excluded.idempotency_key,request_sha256=excluded.request_sha256,
        status=excluded.status,confirmed_at=excluded.confirmed_at;
    PERFORM trosa.compat_set_lastrowid(lid);
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.agent_gateway_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    u text := trosa.compat_current_user();
    lid bigint;
    action_name text := btrim(coalesce(NEW.action,''));
    idem_key text := btrim(coalesce(NEW.idempotency_key,''));
    proposal uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM trade_os_compat.agent_gateway_rows
         WHERE legacy_user_id=u AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF action_name='' OR idem_key='' THEN
        RAISE EXCEPTION 'action and idempotency_key are required';
    END IF;
    IF TG_OP='UPDATE' THEN
        IF action_name IS DISTINCT FROM btrim(coalesce(OLD.action,''))
           OR idem_key IS DISTINCT FROM btrim(coalesce(OLD.idempotency_key,''))
           OR NEW.id IS DISTINCT FROM OLD.id THEN
            RAISE EXCEPTION 'gateway idempotency identity is immutable';
        END IF;
        lid := OLD.id;
    ELSE
        SELECT id INTO lid
          FROM trade_os_compat.agent_gateway_rows
         WHERE legacy_user_id=u AND action=action_name AND idempotency_key=idem_key
         LIMIT 1;
        IF lid IS NULL THEN
            lid := CASE WHEN coalesce(NEW.id,0)<>0 THEN NEW.id
                        ELSE trosa.compat_next_id('agent_gateway_idempotency',u) END;
            IF EXISTS (SELECT 1 FROM trade_os_compat.agent_gateway_rows
                        WHERE legacy_user_id=u AND id=lid
                          AND (action<>action_name OR idempotency_key<>idem_key)) THEN
                lid := trosa.compat_next_id('agent_gateway_idempotency',u);
            END IF;
        END IF;
    END IF;
    INSERT INTO trade_os_compat.agent_gateway_rows
        (legacy_user_id,id,action,idempotency_key,request_sha256,proposal_id,response_json,
         created_at,updated_at)
    VALUES
        (u,lid,action_name,idem_key,coalesce(NEW.request_sha256,''),NEW.proposal_id,
         coalesce(NEW.response_json,'{}'),coalesce(NEW.created_at,''),coalesce(NEW.updated_at,''))
    ON CONFLICT (legacy_user_id,action,idempotency_key) DO UPDATE SET
        request_sha256=excluded.request_sha256,proposal_id=excluded.proposal_id,
        response_json=excluded.response_json,updated_at=excluded.updated_at;
    IF NEW.proposal_id IS NOT NULL THEN
        SELECT id INTO proposal
          FROM audit.agent_proposals
         WHERE organization_id=trosa.compat_org_id()
           AND id=trosa.compat_uuid('agent-proposal:'||u||':'||NEW.proposal_id::text)
         LIMIT 1;
    END IF;
    INSERT INTO audit.agent_gateway_idempotency
        (id,organization_id,legacy_user_id,action,idempotency_key,request_sha256,proposal_id,
         response_json,created_at,updated_at)
    VALUES
        (trosa.compat_uuid('agent-gateway:'||u||':'||action_name||':'||idem_key),
         trosa.compat_org_id(),u,action_name,idem_key,coalesce(NEW.request_sha256,''),proposal,
         trosa.compat_jsonb(NEW.response_json,'{}'::jsonb),
         coalesce(trosa.compat_time(NEW.created_at),now()),
         coalesce(trosa.compat_time(NEW.updated_at),now()))
    ON CONFLICT (organization_id,legacy_user_id,action,idempotency_key) DO UPDATE SET
        request_sha256=excluded.request_sha256,proposal_id=excluded.proposal_id,
        response_json=excluded.response_json,updated_at=excluded.updated_at;
    PERFORM trosa.compat_set_lastrowid(lid);
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.agent_actions_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    u text := trosa.compat_current_user();
    lid bigint;
    action_name text;
    account uuid;
    actor uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM trade_os_compat.agent_action_rows
         WHERE legacy_user_id=u AND id=OLD.id;
        RETURN OLD;
    END IF;
    lid := CASE WHEN coalesce(NEW.id,0)<>0 THEN NEW.id
                ELSE trosa.compat_next_id('agent_actions',u) END;
    action_name := coalesce(nullif(NEW.action_id,''),'legacy:'||u||':'||lid::text);
    IF TG_OP='UPDATE' AND action_name IS DISTINCT FROM btrim(coalesce(OLD.action_id,'')) THEN
        RAISE EXCEPTION 'agent action identity is immutable';
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'agent action identity is immutable';
    END IF;
    IF NEW.customer_id IS NOT NULL THEN
        account := trosa.compat_customer_account(NEW.customer_id,u);
        IF account IS NULL THEN
            RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,u;
        END IF;
    END IF;
    SELECT id INTO lid FROM trade_os_compat.agent_action_rows
     WHERE legacy_user_id=u AND action_id=action_name LIMIT 1;
    IF lid IS NULL THEN
        lid := CASE WHEN coalesce(NEW.id,0)<>0 THEN NEW.id
                    ELSE trosa.compat_next_id('agent_actions',u) END;
        IF EXISTS (SELECT 1 FROM trade_os_compat.agent_action_rows
                    WHERE legacy_user_id=u AND id=lid AND action_id<>action_name) THEN
            lid := trosa.compat_next_id('agent_actions',u);
        END IF;
    END IF;
    INSERT INTO trade_os_compat.agent_action_rows
        (legacy_user_id,id,action_id,token_id,user_id,action_type,customer_id,related_type,
         related_id,undo_token,request_json,status,created_at,undone_at)
    VALUES
        (u,lid,action_name,coalesce(NEW.token_id,''),u,coalesce(NEW.action_type,''),NEW.customer_id,
         coalesce(NEW.related_type,''),NEW.related_id,coalesce(nullif(NEW.undo_token,''),'legacy:'||u||':'||lid::text),
         coalesce(NEW.request_json,'{}'),coalesce(NEW.status,'completed'),coalesce(NEW.created_at,''),
         coalesce(NEW.undone_at,''))
    ON CONFLICT (legacy_user_id,action_id) DO UPDATE SET
        token_id=excluded.token_id,user_id=excluded.user_id,action_type=excluded.action_type,
        customer_id=excluded.customer_id,related_type=excluded.related_type,related_id=excluded.related_id,
        undo_token=excluded.undo_token,request_json=excluded.request_json,status=excluded.status,
        created_at=excluded.created_at,undone_at=excluded.undone_at;
    SELECT id INTO actor FROM identity.users
     WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u LIMIT 1;
    INSERT INTO audit.agent_actions
        (id,organization_id,legacy_user_id,action_id,token_id,actor_user_id,action_type,account_id,related_type,
         related_id,undo_token,request_payload,status,created_at,undone_at)
    VALUES
        (trosa.compat_uuid('agent-action:'||u||':'||action_name),trosa.compat_org_id(),u,action_name,
         coalesce(NEW.token_id,''),actor,coalesce(NEW.action_type,''),account,coalesce(NEW.related_type,''),
         coalesce(NEW.related_id::text,''),coalesce(nullif(NEW.undo_token,''),'legacy:'||u||':'||lid::text),
         trosa.compat_jsonb(NEW.request_json,'{}'::jsonb),coalesce(NEW.status,'completed'),
         coalesce(trosa.compat_time(NEW.created_at),now()),trosa.compat_time(NEW.undone_at))
    ON CONFLICT (organization_id,legacy_user_id,action_id) DO UPDATE SET
        token_id=excluded.token_id,actor_user_id=excluded.actor_user_id,action_type=excluded.action_type,
        account_id=excluded.account_id,related_type=excluded.related_type,related_id=excluded.related_id,
        undo_token=excluded.undo_token,request_payload=excluded.request_payload,status=excluded.status,
        undone_at=excluded.undone_at;
    PERFORM trosa.compat_set_lastrowid(lid);
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.undo_actions_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    u text := trosa.compat_current_user();
    lid bigint;
    token_value text;
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM trade_os_compat.undo_action_rows
         WHERE legacy_user_id=u AND id=OLD.id;
        RETURN OLD;
    END IF;
    lid := CASE WHEN coalesce(NEW.id,0)<>0 THEN NEW.id
                ELSE trosa.compat_next_id('undo_actions',u) END;
    token_value := coalesce(nullif(NEW.token,''),'legacy:'||u||':'||lid::text);
    IF TG_OP='UPDATE' AND (
        token_value IS DISTINCT FROM btrim(coalesce(OLD.token,''))
        OR NEW.id IS DISTINCT FROM OLD.id
    ) THEN
        RAISE EXCEPTION 'undo action identity is immutable';
    END IF;
    SELECT id INTO lid FROM trade_os_compat.undo_action_rows
     WHERE legacy_user_id=u AND token=token_value LIMIT 1;
    IF lid IS NULL THEN
        lid := CASE WHEN coalesce(NEW.id,0)<>0 THEN NEW.id
                    ELSE trosa.compat_next_id('undo_actions',u) END;
        IF EXISTS (SELECT 1 FROM trade_os_compat.undo_action_rows
                    WHERE legacy_user_id=u AND id=lid AND token<>token_value) THEN
            lid := trosa.compat_next_id('undo_actions',u);
        END IF;
    END IF;
    INSERT INTO trade_os_compat.undo_action_rows
        (legacy_user_id,id,token,operation,target_type,target_id,description,entities,status,created_at,undone_at)
    VALUES
        (u,lid,token_value,coalesce(NEW.operation,''),coalesce(NEW.target_type,''),NEW.target_id,
         coalesce(NEW.description,''),coalesce(NEW.entities,'[]'),coalesce(NEW.status,'available'),
         coalesce(NEW.created_at,''),coalesce(NEW.undone_at,''))
    ON CONFLICT (legacy_user_id,token) DO UPDATE SET
        operation=excluded.operation,target_type=excluded.target_type,target_id=excluded.target_id,
        description=excluded.description,entities=excluded.entities,status=excluded.status,
        created_at=excluded.created_at,undone_at=excluded.undone_at;
    INSERT INTO audit.undo_snapshots
        (id,organization_id,legacy_user_id,token,operation,target_type,target_id,description,entities,status,created_at,undone_at)
    VALUES
        (trosa.compat_uuid('undo:'||u||':'||token_value),trosa.compat_org_id(),u,token_value,
         coalesce(NEW.operation,''),coalesce(NEW.target_type,''),coalesce(NEW.target_id::text,''),
         coalesce(NEW.description,''),trosa.compat_jsonb(NEW.entities,'[]'::jsonb),coalesce(NEW.status,'available'),
         coalesce(trosa.compat_time(NEW.created_at),now()),trosa.compat_time(NEW.undone_at))
    ON CONFLICT (organization_id,legacy_user_id,token) DO UPDATE SET
        operation=excluded.operation,target_type=excluded.target_type,target_id=excluded.target_id,
        description=excluded.description,entities=excluded.entities,status=excluded.status,
        undone_at=excluded.undone_at;
    PERFORM trosa.compat_set_lastrowid(lid);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS operation_logs_write ON trade_os_compat.operation_logs;
CREATE TRIGGER operation_logs_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.operation_logs
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.operation_logs_write();
DROP TRIGGER IF EXISTS agent_proposals_write ON trade_os_compat.agent_proposals;
CREATE TRIGGER agent_proposals_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.agent_proposals
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.agent_proposals_write();
DROP TRIGGER IF EXISTS agent_gateway_write ON trade_os_compat.agent_gateway_idempotency;
CREATE TRIGGER agent_gateway_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.agent_gateway_idempotency
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.agent_gateway_write();
DROP TRIGGER IF EXISTS agent_actions_write ON trade_os_compat.agent_actions;
CREATE TRIGGER agent_actions_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.agent_actions
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.agent_actions_write();
DROP TRIGGER IF EXISTS undo_actions_write ON trade_os_compat.undo_actions;
CREATE TRIGGER undo_actions_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.undo_actions
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.undo_actions_write();

CREATE OR REPLACE FUNCTION trade_os_compat.gmail_message_states_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_user text := trosa.compat_current_user();
    v_id bigint;
    v_provider text;
    v_old_provider text;
    v_existing_id bigint;
    v_receipt uuid;
    v_account uuid;
    v_contact uuid;
    v_timeline uuid;
    v_inbox uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        SELECT provider_message_id INTO v_provider
          FROM trade_os_compat.gmail_message_state_rows
         WHERE legacy_user_id=v_user AND id=OLD.id;
        DELETE FROM trosa.email_message_receipts
         WHERE organization_id=v_org AND provider_message_id=coalesce(v_provider,'');
        DELETE FROM trade_os_compat.gmail_message_state_rows
         WHERE legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    v_provider := nullif(btrim(coalesce(NEW.provider_message_id,'')), '');
    IF TG_OP='UPDATE' THEN
        v_id := OLD.id;
        SELECT provider_message_id INTO v_old_provider
          FROM trade_os_compat.gmail_message_state_rows
         WHERE legacy_user_id=v_user AND id=v_id;
        SELECT id INTO v_existing_id
          FROM trade_os_compat.gmail_message_state_rows
         WHERE legacy_user_id=v_user AND provider_message_id=coalesce(v_provider,'')
         LIMIT 1;
        IF v_existing_id IS NOT NULL AND v_existing_id<>v_id THEN
            RAISE EXCEPTION 'provider message % already belongs to legacy row %',v_provider,v_existing_id;
        END IF;
    ELSE
        SELECT id INTO v_id FROM trade_os_compat.gmail_message_state_rows
         WHERE legacy_user_id=v_user AND provider_message_id=coalesce(v_provider,'') LIMIT 1;
        IF v_id IS NULL THEN
            v_id := CASE WHEN coalesce(NEW.id,0)>0 THEN NEW.id
                         ELSE trosa.compat_next_id('gmail_message_states',v_user) END;
            IF EXISTS (SELECT 1 FROM trade_os_compat.gmail_message_state_rows
                        WHERE legacy_user_id=v_user AND id=v_id
                          AND provider_message_id<>coalesce(v_provider,'')) THEN
                v_id := trosa.compat_next_id('gmail_message_states',v_user);
            END IF;
        END IF;
    END IF;
    v_provider := coalesce(v_provider,'legacy:'||v_user||':'||v_id::text);
    IF TG_OP='UPDATE' AND v_old_provider IS DISTINCT FROM v_provider THEN
        RAISE EXCEPTION 'provider message identity is immutable';
    END IF;
    v_account := trosa.compat_customer_account(NEW.customer_id,v_user);
    SELECT cr.contact_method_id INTO v_contact
      FROM trosa.contact_legacy_refs cr
     WHERE cr.organization_id=v_org AND cr.legacy_user_id=v_user
       AND cr.legacy_contact_id=NEW.contact_id LIMIT 1;
    SELECT lr.target_id INTO v_timeline
      FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=v_org AND lr.legacy_user_id=v_user
       AND lr.table_name='follow_up_logs' AND lr.legacy_id=NEW.activity_id LIMIT 1;
    SELECT lr.target_id INTO v_inbox
      FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=v_org AND lr.legacy_user_id=v_user
       AND lr.table_name='inbox_items' AND lr.legacy_id=NEW.inbox_item_id LIMIT 1;
    v_receipt := trosa.compat_uuid('gmail-receipt:'||v_user||':'||v_provider);
    INSERT INTO trosa.email_message_receipts
        (id,organization_id,provider_message_id,provider_thread_id,message_time,sender_email,
         recipient_emails,subject,account_id,contact_method_id,timeline_event_id,inbox_item_id,
         match_status,raw_payload,last_error,created_at,updated_at)
    VALUES
        (v_receipt,v_org,v_provider,coalesce(NEW.provider_thread_id,''),trosa.compat_time(NEW.message_time),
         coalesce(NEW.sender_email,''),trosa.compat_jsonb(NEW.recipient_emails,'[]'::jsonb),
         coalesce(NEW.subject,''),v_account,v_contact,v_timeline,v_inbox,coalesce(NEW.match_status,'unmatched'),
         trosa.compat_jsonb(NEW.raw_payload,'{}'::jsonb),coalesce(NEW.last_error,''),
         coalesce(trosa.compat_time(NEW.created_at),now()),coalesce(trosa.compat_time(NEW.updated_at),now()))
    ON CONFLICT (organization_id,provider_message_id) DO UPDATE SET
        provider_thread_id=excluded.provider_thread_id,message_time=excluded.message_time,
        sender_email=excluded.sender_email,recipient_emails=excluded.recipient_emails,
        subject=excluded.subject,account_id=excluded.account_id,contact_method_id=excluded.contact_method_id,
        timeline_event_id=excluded.timeline_event_id,inbox_item_id=excluded.inbox_item_id,
        match_status=excluded.match_status,raw_payload=excluded.raw_payload,last_error=excluded.last_error,
        updated_at=excluded.updated_at;
    INSERT INTO trade_os_compat.gmail_message_state_rows
        (legacy_user_id,id,provider_message_id,provider_thread_id,message_time,sender_email,
         recipient_emails,subject,customer_id,contact_id,match_status,activity_id,inbox_item_id,
         raw_payload,last_error,created_at,updated_at)
    VALUES
        (v_user,v_id,v_provider,coalesce(NEW.provider_thread_id,''),coalesce(NEW.message_time,''),
         coalesce(NEW.sender_email,''),coalesce(NEW.recipient_emails,'[]'),coalesce(NEW.subject,''),
         NEW.customer_id,NEW.contact_id,coalesce(NEW.match_status,'unmatched'),NEW.activity_id,
         NEW.inbox_item_id,coalesce(NEW.raw_payload,'{}'),coalesce(NEW.last_error,''),
         coalesce(NEW.created_at,''),coalesce(NEW.updated_at,''))
    ON CONFLICT (legacy_user_id,provider_message_id) DO UPDATE SET
        id=v_id,provider_thread_id=excluded.provider_thread_id,message_time=excluded.message_time,
        sender_email=excluded.sender_email,recipient_emails=excluded.recipient_emails,subject=excluded.subject,
        customer_id=excluded.customer_id,contact_id=excluded.contact_id,match_status=excluded.match_status,
        activity_id=excluded.activity_id,inbox_item_id=excluded.inbox_item_id,raw_payload=excluded.raw_payload,
        last_error=excluded.last_error,updated_at=excluded.updated_at;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.communication_sources_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_user text := trosa.compat_current_user();
    v_activity bigint := coalesce(NEW.activity_id,0);
    v_timeline uuid;
    v_source uuid;
    v_id bigint;
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM trosa.communication_source_items
         WHERE organization_id=v_org AND legacy_user_id=v_user
           AND communication_source_id=(
             SELECT cs.id FROM trosa.communication_sources cs
             JOIN trosa.legacy_row_refs lr ON lr.target_id=cs.timeline_event_id
              AND lr.organization_id=v_org AND lr.legacy_user_id=v_user
              AND lr.table_name='follow_up_logs' AND lr.legacy_id=OLD.activity_id
         );
        DELETE FROM trosa.communication_sources
         WHERE timeline_event_id=(SELECT lr.target_id FROM trosa.legacy_row_refs lr
             WHERE lr.organization_id=v_org AND lr.legacy_user_id=v_user
               AND lr.table_name='follow_up_logs' AND lr.legacy_id=OLD.activity_id);
        DELETE FROM trade_os_compat.communication_source_rows
         WHERE legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND (
        NEW.id IS DISTINCT FROM OLD.id
        OR v_activity IS DISTINCT FROM OLD.activity_id
    ) THEN
        RAISE EXCEPTION 'communication source identity is immutable';
    END IF;
    SELECT lr.target_id INTO v_timeline FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=v_org AND lr.legacy_user_id=v_user
       AND lr.table_name='follow_up_logs' AND lr.legacy_id=v_activity LIMIT 1;
    IF v_timeline IS NULL THEN
        RAISE EXCEPTION 'follow_up_log % is not visible for user %',v_activity,v_user;
    END IF;
    SELECT cs.id INTO v_source FROM trosa.communication_sources cs
     WHERE cs.timeline_event_id=v_timeline;
    v_source := coalesce(v_source,trosa.compat_uuid('communication-source:'||v_user||':'||v_activity::text));
    INSERT INTO trosa.communication_sources
        (id,timeline_event_id,channel,source_url,account,conversation_identity,adapter_version,
         extraction_scope,warnings,raw_payload,cleaned_payload,captured_at)
    VALUES
        (v_source,v_timeline,coalesce(NEW.channel,''),coalesce(NEW.source_url,''),coalesce(NEW.account,''),
         coalesce(NEW.conversation_identity,''),coalesce(NEW.adapter_version,''),coalesce(NEW.extraction_scope,''),
         trosa.compat_jsonb(NEW.warnings,'[]'::jsonb),trosa.compat_jsonb(NEW.raw_payload,'{}'::jsonb),
         coalesce(NEW.cleaned_payload,''),trosa.compat_time(NEW.captured_at))
    ON CONFLICT (timeline_event_id) DO UPDATE SET
        channel=excluded.channel,source_url=excluded.source_url,account=excluded.account,
        conversation_identity=excluded.conversation_identity,adapter_version=excluded.adapter_version,
        extraction_scope=excluded.extraction_scope,warnings=excluded.warnings,raw_payload=excluded.raw_payload,
        cleaned_payload=excluded.cleaned_payload,captured_at=excluded.captured_at;
    SELECT id INTO v_id FROM trade_os_compat.communication_source_rows
     WHERE legacy_user_id=v_user AND activity_id=v_activity LIMIT 1;
    IF v_id IS NULL THEN
        v_id := CASE WHEN coalesce(NEW.id,0)<>0 THEN NEW.id
                     ELSE trosa.compat_next_id('communication_sources',v_user) END;
        IF EXISTS (SELECT 1 FROM trade_os_compat.communication_source_rows
                    WHERE legacy_user_id=v_user AND id=v_id AND activity_id<>v_activity) THEN
            v_id := trosa.compat_next_id('communication_sources',v_user);
        END IF;
    END IF;
    INSERT INTO trade_os_compat.communication_source_rows
        (legacy_user_id,id,activity_id,channel,source_url,account,conversation_identity,
         adapter_version,extraction_scope,warnings,raw_payload,cleaned_payload,captured_at)
    VALUES
        (v_user,v_id,v_activity,coalesce(NEW.channel,''),coalesce(NEW.source_url,''),coalesce(NEW.account,''),
         coalesce(NEW.conversation_identity,''),coalesce(NEW.adapter_version,''),coalesce(NEW.extraction_scope,''),
         coalesce(NEW.warnings,'[]'),coalesce(NEW.raw_payload,'{}'),coalesce(NEW.cleaned_payload,''),
         coalesce(NEW.captured_at,''))
    ON CONFLICT (legacy_user_id,activity_id) DO UPDATE SET
        id=v_id,channel=excluded.channel,source_url=excluded.source_url,account=excluded.account,
        conversation_identity=excluded.conversation_identity,adapter_version=excluded.adapter_version,
        extraction_scope=excluded.extraction_scope,warnings=excluded.warnings,raw_payload=excluded.raw_payload,
        cleaned_payload=excluded.cleaned_payload,captured_at=excluded.captured_at;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.communication_source_items_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_user text := trosa.compat_current_user();
    v_activity bigint := coalesce(NEW.activity_id,0);
    v_timeline uuid;
    v_source uuid;
    v_item uuid;
    v_id bigint;
    v_fingerprint text := btrim(coalesce(NEW.source_fingerprint,''));
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM trosa.communication_source_items WHERE source_fingerprint=OLD.source_fingerprint;
        DELETE FROM trade_os_compat.communication_source_item_rows
         WHERE legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF v_fingerprint='' THEN RAISE EXCEPTION 'source_fingerprint is required'; END IF;
    IF TG_OP='UPDATE' AND (
        v_fingerprint IS DISTINCT FROM btrim(coalesce(OLD.source_fingerprint,''))
        OR v_activity IS DISTINCT FROM OLD.activity_id
    ) THEN
        RAISE EXCEPTION 'communication source item identity is immutable';
    END IF;
    SELECT lr.target_id INTO v_timeline FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=v_org AND lr.legacy_user_id=v_user
       AND lr.table_name='follow_up_logs' AND lr.legacy_id=v_activity LIMIT 1;
    SELECT cs.id INTO v_source FROM trosa.communication_sources cs WHERE cs.timeline_event_id=v_timeline;
    IF v_source IS NULL THEN RAISE EXCEPTION 'communication source for activity % is missing',v_activity; END IF;
    SELECT id INTO v_item FROM trosa.communication_source_items
     WHERE source_fingerprint=v_fingerprint LIMIT 1;
    v_item := coalesce(v_item,trosa.compat_uuid('communication-item:'||v_fingerprint));
    INSERT INTO trosa.communication_source_items
        (id,communication_source_id,source_fingerprint,message_time,direction,raw_text)
    VALUES
        (v_item,v_source,v_fingerprint,trosa.compat_time(NEW.message_time),coalesce(NEW.direction,'unknown'),
         coalesce(NEW.raw_text,''))
    ON CONFLICT (source_fingerprint) DO UPDATE SET
        communication_source_id=excluded.communication_source_id,message_time=excluded.message_time,
        direction=excluded.direction,raw_text=excluded.raw_text;
    SELECT id INTO v_id FROM trade_os_compat.communication_source_item_rows
     WHERE legacy_user_id=v_user AND source_fingerprint=v_fingerprint LIMIT 1;
    IF v_id IS NULL THEN
        v_id := CASE WHEN coalesce(NEW.id,0)<>0 THEN NEW.id
                     ELSE trosa.compat_next_id('communication_source_items',v_user) END;
        IF EXISTS (SELECT 1 FROM trade_os_compat.communication_source_item_rows
                    WHERE legacy_user_id=v_user AND id=v_id
                      AND source_fingerprint<>v_fingerprint) THEN
            v_id := trosa.compat_next_id('communication_source_items',v_user);
        END IF;
    END IF;
    INSERT INTO trade_os_compat.communication_source_item_rows
        (legacy_user_id,id,source_fingerprint,activity_id,message_time,direction,raw_text)
    VALUES
        (v_user,v_id,v_fingerprint,v_activity,coalesce(NEW.message_time,''),coalesce(NEW.direction,'unknown'),coalesce(NEW.raw_text,''))
    ON CONFLICT (legacy_user_id,source_fingerprint) DO UPDATE SET
        id=v_id,activity_id=excluded.activity_id,message_time=excluded.message_time,
        direction=excluded.direction,raw_text=excluded.raw_text;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS gmail_message_states_write ON trade_os_compat.gmail_message_states;
CREATE TRIGGER gmail_message_states_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.gmail_message_states
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.gmail_message_states_write();
DROP TRIGGER IF EXISTS communication_sources_write ON trade_os_compat.communication_sources;
CREATE TRIGGER communication_sources_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.communication_sources
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.communication_sources_write();
DROP TRIGGER IF EXISTS communication_source_items_write ON trade_os_compat.communication_source_items;
CREATE TRIGGER communication_source_items_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.communication_source_items
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.communication_source_items_write();

CREATE OR REPLACE FUNCTION trade_os_compat.email_delivery_events_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_user text := trosa.compat_current_user();
    v_id bigint;
    v_contact uuid;
    v_outreach uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM trosa.email_delivery_events
         WHERE id=trosa.compat_uuid('email-delivery:'||v_user||':'||OLD.id::text)
           AND organization_id=v_org;
        DELETE FROM trade_os_compat.email_delivery_event_rows
         WHERE legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' THEN
        -- An UPDATE must retain the legacy identity.  Reallocating when an
        -- editable email field changes would leave the old canonical event
        -- behind and make one delivery look like two events.
        IF NEW.id IS DISTINCT FROM OLD.id THEN
            RAISE EXCEPTION 'email delivery event identity is immutable';
        END IF;
        v_id := OLD.id;
    ELSE
        v_id := CASE WHEN coalesce(NEW.id,0)>0 THEN NEW.id
                     ELSE trosa.compat_next_id('email_delivery_events',v_user) END;
        IF EXISTS (SELECT 1 FROM trade_os_compat.email_delivery_event_rows
                    WHERE legacy_user_id=v_user AND id=v_id
                      AND coalesce(email,'')<>coalesce(NEW.email,'')) THEN
            v_id := trosa.compat_next_id('email_delivery_events',v_user);
        END IF;
    END IF;
    SELECT cr.contact_method_id INTO v_contact
      FROM trosa.contact_legacy_refs cr
     WHERE cr.organization_id=v_org AND cr.legacy_user_id=v_user
       AND cr.legacy_contact_id=NEW.contact_id LIMIT 1;
    IF v_contact IS NULL AND nullif(lower(btrim(coalesce(NEW.email,''))),'') IS NOT NULL THEN
        SELECT cm.id INTO v_contact FROM core.contact_methods cm
         WHERE cm.organization_id=v_org AND cm.kind='email'
           AND cm.normalized_value=lower(btrim(NEW.email)) LIMIT 1;
    END IF;
    SELECT lr.target_id INTO v_outreach FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=v_org AND lr.legacy_user_id=v_user
       AND lr.table_name='outreach_emails' AND lr.legacy_id=NEW.outreach_email_id LIMIT 1;
    INSERT INTO trosa.email_delivery_events
        (id,organization_id,contact_method_id,outreach_message_id,event_type,smtp_code,enhanced_status,
         diagnostic_text,remote_mta,provider_message_id,source,occurred_at,legacy_payload)
    VALUES
        (trosa.compat_uuid('email-delivery:'||v_user||':'||v_id::text),v_org,v_contact,v_outreach,
         coalesce(NEW.event_type,''),coalesce(NEW.smtp_code,''),coalesce(NEW.enhanced_status,''),
         coalesce(NEW.diagnostic_text,''),coalesce(NEW.remote_mta,''),coalesce(NEW.message_id,''),
         coalesce(NEW.source,'manual'),coalesce(trosa.compat_time(NEW.occurred_at),now()),to_jsonb(NEW))
    ON CONFLICT (id) DO UPDATE SET
        contact_method_id=excluded.contact_method_id,outreach_message_id=excluded.outreach_message_id,
        event_type=excluded.event_type,smtp_code=excluded.smtp_code,enhanced_status=excluded.enhanced_status,
        diagnostic_text=excluded.diagnostic_text,remote_mta=excluded.remote_mta,
        provider_message_id=excluded.provider_message_id,source=excluded.source,
        occurred_at=excluded.occurred_at,legacy_payload=excluded.legacy_payload;
    INSERT INTO trade_os_compat.email_delivery_event_rows
        (legacy_user_id,id,email,contact_id,outreach_email_id,event_type,smtp_code,enhanced_status,
         diagnostic_text,remote_mta,message_id,source,occurred_at)
    VALUES
        (v_user,v_id,coalesce(NEW.email,''),NEW.contact_id,NEW.outreach_email_id,coalesce(NEW.event_type,''),
         coalesce(NEW.smtp_code,''),coalesce(NEW.enhanced_status,''),coalesce(NEW.diagnostic_text,''),
         coalesce(NEW.remote_mta,''),coalesce(NEW.message_id,''),coalesce(NEW.source,'manual'),
         coalesce(NEW.occurred_at,''))
    ON CONFLICT (legacy_user_id,id) DO UPDATE SET
        email=excluded.email,contact_id=excluded.contact_id,outreach_email_id=excluded.outreach_email_id,
        event_type=excluded.event_type,smtp_code=excluded.smtp_code,enhanced_status=excluded.enhanced_status,
        diagnostic_text=excluded.diagnostic_text,remote_mta=excluded.remote_mta,message_id=excluded.message_id,
        source=excluded.source,occurred_at=excluded.occurred_at;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.weekly_reports_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_user text := trosa.compat_current_user();
    v_week text := btrim(coalesce(NEW.week_start,''));
    v_status text := coalesce(nullif(NEW.status,''),'draft');
    v_id bigint;
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM trosa.weekly_reports
         WHERE organization_id=v_org AND legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF v_week='' THEN RAISE EXCEPTION 'week_start is required'; END IF;
    IF TG_OP='UPDATE' AND (
        NEW.id IS DISTINCT FROM OLD.id
        OR v_week IS DISTINCT FROM btrim(coalesce(OLD.week_start,''))
    ) THEN
        RAISE EXCEPTION 'weekly report identity is immutable';
    END IF;
    IF v_status NOT IN ('draft','submitted') THEN v_status:='draft'; END IF;
    -- NEW.user_id is a client-controlled compatibility column.  Ownership is
    -- always taken from the authenticated connection, never from that value.
    INSERT INTO trosa.weekly_reports
        (organization_id,legacy_user_id,week_start,content,highlights,challenges,next_plan,status,created_at,updated_at)
    VALUES
        (v_org,v_user,v_week,coalesce(NEW.content,''),coalesce(NEW.highlights,''),coalesce(NEW.challenges,''),
         coalesce(NEW.next_plan,''),v_status,coalesce(trosa.compat_time(NEW.created_at),now()),
         coalesce(trosa.compat_time(NEW.updated_at),now()))
    ON CONFLICT (organization_id,legacy_user_id,week_start) DO UPDATE SET
        content=excluded.content,highlights=excluded.highlights,challenges=excluded.challenges,
        next_plan=excluded.next_plan,status=excluded.status,updated_at=excluded.updated_at;
    SELECT id INTO v_id FROM trosa.weekly_reports
     WHERE organization_id=v_org AND legacy_user_id=v_user AND week_start=v_week LIMIT 1;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS email_delivery_events_write ON trade_os_compat.email_delivery_events;
CREATE TRIGGER email_delivery_events_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.email_delivery_events
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.email_delivery_events_write();
DROP TRIGGER IF EXISTS weekly_reports_write ON trade_os_compat.weekly_reports;
CREATE TRIGGER weekly_reports_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.weekly_reports
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.weekly_reports_write();

CREATE OR REPLACE FUNCTION trade_os_compat.import_batches_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_user text := trosa.compat_current_user();
    v_id bigint;
    v_audit_id uuid;
    v_path text;
    v_source_name text := coalesce(NEW.source_name,'');
    v_source_sha256 text := coalesce(NEW.source_sha256,'');
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM trade_os_compat.import_batch_rows WHERE legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' THEN
        IF NEW.id IS DISTINCT FROM OLD.id THEN
            RAISE EXCEPTION 'import batch identity is immutable';
        END IF;
        v_id := OLD.id;
    ELSE
        -- A retry can arrive with a newly allocated legacy id even though it
        -- is the same source snapshot.  Resolve the natural key before the
        -- physical compatibility primary key so the audit batch is not
        -- duplicated or rejected by its source uniqueness constraint.
        IF v_source_name<>'' AND v_source_sha256<>'' THEN
            SELECT id INTO v_id
              FROM trade_os_compat.import_batch_rows
             WHERE legacy_user_id=v_user AND source_name=v_source_name
               AND source_sha256=v_source_sha256
             LIMIT 1;
        END IF;
        v_id := coalesce(v_id,CASE WHEN coalesce(NEW.id,0)<>0 THEN NEW.id
                                  ELSE trosa.compat_next_id('import_batches',v_user) END);
        IF EXISTS (
            SELECT 1 FROM trade_os_compat.import_batch_rows
             WHERE legacy_user_id=v_user AND id=v_id
               AND (source_name IS DISTINCT FROM v_source_name
                    OR source_sha256 IS DISTINCT FROM v_source_sha256)
        ) THEN
            v_id := trosa.compat_next_id('import_batches',v_user);
        END IF;
    END IF;
    INSERT INTO trade_os_compat.import_batch_rows
        (legacy_user_id,id,source_name,source_sha256,imported_at,imported_count,skipped_count,created_customers,details)
    VALUES
        (v_user,v_id,v_source_name,v_source_sha256,coalesce(NEW.imported_at,''),
         coalesce(NEW.imported_count,0),coalesce(NEW.skipped_count,0),coalesce(NEW.created_customers,0),coalesce(NEW.details,''))
    ON CONFLICT (legacy_user_id,id) DO UPDATE SET
        source_name=excluded.source_name,source_sha256=excluded.source_sha256,imported_at=excluded.imported_at,
        imported_count=excluded.imported_count,skipped_count=excluded.skipped_count,
        created_customers=excluded.created_customers,details=excluded.details;
    v_audit_id := trosa.compat_uuid('import-batch:'||v_user||':'||v_id::text);
    v_path := 'compat/'||v_user||'/'||coalesce(NEW.source_name,'')||'/'||v_id::text;
    INSERT INTO audit.import_batches
        (id,organization_id,source_name,source_path,source_sha256,source_rows,imported_at)
    VALUES
        (v_audit_id,trosa.compat_org_id(),v_source_name,v_path,v_source_sha256,
         greatest(0,coalesce(NEW.imported_count,0)+coalesce(NEW.skipped_count,0)),
         coalesce(trosa.compat_time(NEW.imported_at),now()))
    ON CONFLICT (id) DO UPDATE SET
        source_name=excluded.source_name,source_path=excluded.source_path,source_sha256=excluded.source_sha256,
        source_rows=excluded.source_rows,imported_at=excluded.imported_at;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS import_batches_write ON trade_os_compat.import_batches;
CREATE TRIGGER import_batches_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.import_batches
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.import_batches_write();

CREATE OR REPLACE FUNCTION trade_os_compat.imported_activity_rows_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_user text := trosa.compat_current_user();
    v_id bigint;
    v_existing_id bigint;
    v_hash text;
    v_old_hash text;
    v_audit_id uuid;
    v_existing_audit_id uuid;
    v_audit_hash text;
    v_account uuid;
    v_activity uuid;
    v_batch uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM trade_os_compat.imported_activity_row_rows
         WHERE legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'imported activity identity is immutable';
    END IF;
    IF TG_OP='UPDATE' THEN
        v_id := OLD.id;
        v_old_hash := coalesce(
            nullif(btrim(OLD.activity_hash),''),
            trosa.compat_uuid('import-activity:'||v_user||':'||OLD.id::text)::text
        );
    ELSE
        v_id := CASE WHEN coalesce(NEW.id,0)<>0 THEN NEW.id
                     ELSE trosa.compat_next_id('imported_activity_rows',v_user) END;
    END IF;
    v_hash := coalesce(nullif(btrim(coalesce(NEW.activity_hash,'')),''),trosa.compat_uuid('import-activity:'||v_user||':'||v_id::text)::text);
    IF TG_OP='UPDATE' THEN
        IF v_hash IS DISTINCT FROM v_old_hash AND EXISTS (
            SELECT 1 FROM trade_os_compat.imported_activity_row_rows
             WHERE legacy_user_id=v_user AND activity_hash=v_hash AND id<>v_id
        ) THEN
            RAISE EXCEPTION 'imported activity hash already belongs to another row';
        END IF;
    ELSE
        SELECT id INTO v_existing_id FROM trade_os_compat.imported_activity_row_rows
         WHERE legacy_user_id=v_user AND activity_hash=v_hash LIMIT 1;
        IF v_existing_id IS NOT NULL THEN
            v_id := v_existing_id;
        ELSIF EXISTS (
            SELECT 1 FROM trade_os_compat.imported_activity_row_rows
             WHERE legacy_user_id=v_user AND id=v_id AND activity_hash<>v_hash
        ) THEN
            v_id := trosa.compat_next_id('imported_activity_rows',v_user);
        END IF;
    END IF;
    v_account := trosa.compat_customer_account(NEW.customer_id,v_user);
    SELECT lr.target_id INTO v_activity FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_user
       AND lr.table_name='follow_up_logs' AND lr.legacy_id=NEW.activity_id LIMIT 1;
    SELECT b.id INTO v_batch
      FROM audit.import_batches b
     WHERE b.id=trosa.compat_uuid('import-batch:'||v_user||':'||NEW.batch_id::text)
       AND b.organization_id=trosa.compat_org_id()
       AND coalesce(NEW.batch_id,0)<>0;
    IF TG_OP='UPDATE' THEN
        SELECT id INTO v_audit_id FROM audit.imported_activity_rows
         WHERE organization_id=trosa.compat_org_id()
           AND legacy_user_id=v_user AND activity_hash=v_user||':'||v_old_hash
         LIMIT 1;
    END IF;
    v_audit_id := coalesce(v_audit_id,trosa.compat_uuid('imported-activity:'||v_user||':'||v_hash));
    INSERT INTO trade_os_compat.imported_activity_row_rows
        (legacy_user_id,id,activity_hash,source_key,batch_id,customer_id,source_name,source_sheet,
         source_cell,source_header,activity_id,imported_at)
    VALUES
        (v_user,v_id,v_hash,coalesce(NEW.source_key,''),NEW.batch_id,coalesce(NEW.customer_id,0),
         coalesce(NEW.source_name,''),coalesce(NEW.source_sheet,''),coalesce(NEW.source_cell,''),
         coalesce(NEW.source_header,''),NEW.activity_id,coalesce(NEW.imported_at,''))
    ON CONFLICT DO UPDATE SET
        source_key=excluded.source_key,batch_id=excluded.batch_id,customer_id=excluded.customer_id,
        source_name=excluded.source_name,source_sheet=excluded.source_sheet,source_cell=excluded.source_cell,
        source_header=excluded.source_header,activity_id=excluded.activity_id,imported_at=excluded.imported_at;
    v_audit_hash := v_user||':'||v_hash;
    SELECT id INTO v_existing_audit_id
      FROM audit.imported_activity_rows
     WHERE organization_id=trosa.compat_org_id()
       AND activity_hash=v_audit_hash
     LIMIT 1;
    IF v_existing_audit_id IS NOT NULL AND v_existing_audit_id IS DISTINCT FROM v_audit_id THEN
        RAISE EXCEPTION 'imported activity audit hash already belongs to another row';
    END IF;
    v_audit_id := coalesce(v_existing_audit_id,v_audit_id);
    INSERT INTO audit.imported_activity_rows
        (id,organization_id,legacy_user_id,activity_hash,source_key,batch_id,account_id,source_name,
         source_sheet,source_cell,source_header,activity_id)
    VALUES
        (v_audit_id,trosa.compat_org_id(),v_user,
         v_audit_hash,coalesce(NEW.source_key,''),v_batch,v_account,coalesce(NEW.source_name,''),
         coalesce(NEW.source_sheet,''),coalesce(NEW.source_cell,''),coalesce(NEW.source_header,''),v_activity)
    ON CONFLICT (id) DO UPDATE SET
        activity_hash=excluded.activity_hash,source_key=excluded.source_key,batch_id=excluded.batch_id,
        account_id=excluded.account_id,source_name=excluded.source_name,source_sheet=excluded.source_sheet,
        source_cell=excluded.source_cell,source_header=excluded.source_header,activity_id=excluded.activity_id;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.import_unmatched_customers_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_user text := trosa.compat_current_user();
    v_id bigint;
    v_existing_id bigint;
    v_hash text;
    v_old_hash text;
    v_audit_id uuid;
    v_existing_audit_id uuid;
    v_audit_hash text;
    v_batch uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM trade_os_compat.import_unmatched_customer_rows
         WHERE legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'unmatched customer identity is immutable';
    END IF;
    IF TG_OP='UPDATE' THEN
        v_id := OLD.id;
        v_old_hash := coalesce(
            nullif(btrim(OLD.unmatched_hash),''),
            trosa.compat_uuid('unmatched:'||v_user||':'||OLD.id::text)::text
        );
    ELSE
        v_id := CASE WHEN coalesce(NEW.id,0)<>0 THEN NEW.id
                     ELSE trosa.compat_next_id('import_unmatched_customers',v_user) END;
    END IF;
    v_hash := coalesce(nullif(btrim(coalesce(NEW.unmatched_hash,'')),''),trosa.compat_uuid('unmatched:'||v_user||':'||v_id::text)::text);
    IF TG_OP='UPDATE' THEN
        IF v_hash IS DISTINCT FROM v_old_hash AND EXISTS (
            SELECT 1 FROM trade_os_compat.import_unmatched_customer_rows
             WHERE legacy_user_id=v_user AND unmatched_hash=v_hash AND id<>v_id
        ) THEN
            RAISE EXCEPTION 'unmatched customer hash already belongs to another row';
        END IF;
    ELSE
        SELECT id INTO v_existing_id FROM trade_os_compat.import_unmatched_customer_rows
         WHERE legacy_user_id=v_user AND unmatched_hash=v_hash LIMIT 1;
        IF v_existing_id IS NOT NULL THEN
            v_id := v_existing_id;
        ELSIF EXISTS (
            SELECT 1 FROM trade_os_compat.import_unmatched_customer_rows
             WHERE legacy_user_id=v_user AND id=v_id AND unmatched_hash<>v_hash
        ) THEN
            v_id := trosa.compat_next_id('import_unmatched_customers',v_user);
        END IF;
    END IF;
    SELECT b.id INTO v_batch
      FROM audit.import_batches b
     WHERE b.id=trosa.compat_uuid('import-batch:'||v_user||':'||NEW.batch_id::text)
       AND b.organization_id=trosa.compat_org_id()
       AND coalesce(NEW.batch_id,0)<>0;
    IF TG_OP='UPDATE' THEN
        SELECT id INTO v_audit_id FROM audit.import_unmatched_customers
         WHERE organization_id=trosa.compat_org_id()
           AND legacy_user_id=v_user AND unmatched_hash=v_user||':'||v_old_hash
         LIMIT 1;
    END IF;
    v_audit_id := coalesce(v_audit_id,trosa.compat_uuid('unmatched:'||v_user||':'||v_hash));
    INSERT INTO trade_os_compat.import_unmatched_customer_rows
        (legacy_user_id,id,unmatched_hash,batch_id,customer_name,country,website,source_sheet,
         source_row,reason,created_at)
    VALUES
        (v_user,v_id,v_hash,NEW.batch_id,coalesce(NEW.customer_name,''),coalesce(NEW.country,''),
         coalesce(NEW.website,''),coalesce(NEW.source_sheet,''),NEW.source_row,coalesce(NEW.reason,''),
         coalesce(NEW.created_at,''))
    ON CONFLICT DO UPDATE SET
        batch_id=excluded.batch_id,customer_name=excluded.customer_name,country=excluded.country,
        website=excluded.website,source_sheet=excluded.source_sheet,source_row=excluded.source_row,
        reason=excluded.reason,created_at=excluded.created_at;
    v_audit_hash := v_user||':'||v_hash;
    SELECT id INTO v_existing_audit_id
      FROM audit.import_unmatched_customers
     WHERE organization_id=trosa.compat_org_id()
       AND unmatched_hash=v_audit_hash
     LIMIT 1;
    IF v_existing_audit_id IS NOT NULL AND v_existing_audit_id IS DISTINCT FROM v_audit_id THEN
        RAISE EXCEPTION 'unmatched customer audit hash already belongs to another row';
    END IF;
    v_audit_id := coalesce(v_existing_audit_id,v_audit_id);
    INSERT INTO audit.import_unmatched_customers
        (id,organization_id,legacy_user_id,unmatched_hash,batch_id,customer_name,country,website,
         source_sheet,source_row,reason)
    VALUES
        (v_audit_id,trosa.compat_org_id(),v_user,v_audit_hash,
         v_batch,coalesce(NEW.customer_name,''),coalesce(NEW.country,''),coalesce(NEW.website,''),
         coalesce(NEW.source_sheet,''),NEW.source_row,coalesce(NEW.reason,''))
    ON CONFLICT (id) DO UPDATE SET
        unmatched_hash=excluded.unmatched_hash,batch_id=excluded.batch_id,customer_name=excluded.customer_name,
        country=excluded.country,website=excluded.website,source_sheet=excluded.source_sheet,
        source_row=excluded.source_row,reason=excluded.reason;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS imported_activity_rows_write ON trade_os_compat.imported_activity_rows;
CREATE TRIGGER imported_activity_rows_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.imported_activity_rows
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.imported_activity_rows_write();
DROP TRIGGER IF EXISTS import_unmatched_customers_write ON trade_os_compat.import_unmatched_customers;
CREATE TRIGGER import_unmatched_customers_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.import_unmatched_customers
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.import_unmatched_customers_write();

-- Preserve canonical facts that still have inbound references when a legacy
-- DELETE arrives.  The SQLite tables allowed those deletes, while PostgreSQL
-- foreign keys correctly refuse to remove a timeline/inbox/outreach row that
-- is already referenced by a source, receipt, or delivery event.
CREATE OR REPLACE FUNCTION trade_os_compat.inbox_items_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    u text := trosa.compat_current_user();
    lid bigint;
    target uuid;
    account uuid;
    raw_key text := btrim(coalesce(NEW.dedupe_key,''));
    stored_key text;
    existing_key text;
    existing_compat_key text;
    payload jsonb;
BEGIN
    IF TG_OP='DELETE' THEN
        SELECT lr.target_id INTO target FROM trosa.legacy_row_refs lr
         WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=u
           AND lr.table_name='inbox_items' AND lr.legacy_id=OLD.id;
        DELETE FROM trosa.legacy_row_refs
         WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u
           AND table_name='inbox_items' AND legacy_id=OLD.id;
        IF target IS NOT NULL AND EXISTS (
            SELECT 1 FROM trosa.email_message_receipts r
             WHERE r.organization_id=trosa.compat_org_id()
               AND r.legacy_user_id=u AND r.inbox_item_id=target
        ) THEN
            UPDATE trosa.inbox_items SET status='resolved',resolved_at=coalesce(resolved_at,now()),
                legacy_payload=legacy_payload||jsonb_build_object('is_deleted','1','deleted_at',now()::text)
             WHERE id=target;
        ELSIF target IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM trosa.legacy_row_refs WHERE target_id=target
        ) THEN
            DELETE FROM trosa.inbox_items WHERE id=target;
        END IF;
        RETURN OLD;
    END IF;
    account := trosa.compat_customer_account(NEW.customer_id,u);
    IF TG_OP='UPDATE' THEN
        IF NEW.id IS DISTINCT FROM OLD.id THEN
            RAISE EXCEPTION 'inbox item identity is immutable';
        END IF;
        lid:=NEW.id;
        SELECT lr.target_id,i.dedupe_key,i.legacy_payload->>'compat_dedupe_key'
          INTO target,existing_key,existing_compat_key
          FROM trosa.legacy_row_refs lr LEFT JOIN trosa.inbox_items i ON i.id=lr.target_id
         WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=u
           AND lr.table_name='inbox_items' AND lr.legacy_id=lid;
    ELSE
        IF coalesce(NEW.id,0)<>0 THEN
            lid:=NEW.id;
            SELECT lr.target_id,i.dedupe_key,i.legacy_payload->>'compat_dedupe_key'
              INTO target,existing_key,existing_compat_key
              FROM trosa.legacy_row_refs lr LEFT JOIN trosa.inbox_items i ON i.id=lr.target_id
             WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=u
               AND lr.table_name='inbox_items' AND lr.legacy_id=lid;
        END IF;
        IF target IS NULL AND raw_key<>'' THEN
            SELECT lr.legacy_id,lr.target_id,i.dedupe_key,i.legacy_payload->>'compat_dedupe_key'
              INTO lid,target,existing_key,existing_compat_key
              FROM trosa.legacy_row_refs lr JOIN trosa.inbox_items i ON i.id=lr.target_id
             WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=u
               AND lr.table_name='inbox_items'
               AND (i.legacy_payload->>'compat_dedupe_key'=raw_key
                    OR (coalesce(i.legacy_payload->>'compat_dedupe_key','')='' AND i.dedupe_key=raw_key))
             ORDER BY i.created_at,lr.legacy_id LIMIT 1;
        END IF;
        IF lid IS NULL THEN lid:=trosa.compat_next_id('inbox_items',u); END IF;
    END IF;
    target:=coalesce(target,trosa.compat_uuid('inbox:'||u||':'||lid::text));
    stored_key:=CASE WHEN raw_key='' THEN ''
        WHEN coalesce(existing_compat_key,existing_key)=raw_key THEN coalesce(existing_key,raw_key)
        ELSE 'compat:'||u||':'||raw_key END;
    payload:=coalesce(to_jsonb(NEW),'{}'::jsonb);
    IF raw_key<>'' THEN payload:=payload||jsonb_build_object('compat_dedupe_key',raw_key);
    ELSE payload:=payload-'compat_dedupe_key'; END IF;
    INSERT INTO trosa.inbox_items
        (id,account_id,item_type,title,content,dedupe_key,status,resolved_at,snoozed_until,
         resolution_reason,resolution_note,legacy_payload)
    VALUES
        (target,account,coalesce(NEW.item_type,''),coalesce(NEW.title,''),coalesce(NEW.content,''),stored_key,
         coalesce(NEW.status,'open'),trosa.compat_time(NEW.resolved_at),trosa.compat_time(NEW.snoozed_until),
         coalesce(NEW.resolution_reason,''),coalesce(NEW.resolution_note,''),payload)
    ON CONFLICT(id) DO UPDATE SET
        account_id=excluded.account_id,item_type=excluded.item_type,title=excluded.title,content=excluded.content,
        dedupe_key=excluded.dedupe_key,status=excluded.status,resolved_at=excluded.resolved_at,
        snoozed_until=excluded.snoozed_until,resolution_reason=excluded.resolution_reason,
        resolution_note=excluded.resolution_note,
        legacy_payload=(CASE WHEN raw_key='' THEN trosa.inbox_items.legacy_payload-'compat_dedupe_key'
                             ELSE trosa.inbox_items.legacy_payload END)||excluded.legacy_payload;
    INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id)
    VALUES(trosa.compat_org_id(),u,'inbox_items',lid,target)
    ON CONFLICT(organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id;
    PERFORM trosa.compat_set_lastrowid(lid);
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trosa.compat_follow_up_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    u text:=trosa.compat_current_user();
    lid bigint;
    account uuid;
    target uuid;
    contact uuid;
    keep_event boolean;
BEGIN
    IF TG_OP='DELETE' THEN
        SELECT lr.target_id INTO target FROM trosa.legacy_row_refs lr
         WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=u
           AND lr.table_name='follow_up_logs' AND lr.legacy_id=OLD.id;
        IF target IS NOT NULL THEN
            keep_event := EXISTS (SELECT 1 FROM trosa.legacy_row_refs WHERE target_id=target
                                  AND NOT (legacy_user_id=u AND table_name='follow_up_logs' AND legacy_id=OLD.id))
                       OR EXISTS (SELECT 1 FROM trosa.communication_sources WHERE timeline_event_id=target)
                       OR EXISTS (SELECT 1 FROM trosa.email_message_receipts WHERE timeline_event_id=target)
                       OR EXISTS (SELECT 1 FROM trosa.account_understandings WHERE source_timeline_event_id=target)
                       OR EXISTS (SELECT 1 FROM trosa.ai_recommendations WHERE source_timeline_event_id=target)
                       OR EXISTS (SELECT 1 FROM audit.imported_activity_rows WHERE activity_id=target);
            IF keep_event THEN
                UPDATE trosa.timeline_events SET payload=payload||jsonb_build_object(
                    'is_deleted','1','deleted_at',now()::text) WHERE id=target;
                DELETE FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id()
                  AND legacy_user_id=u AND table_name='follow_up_logs' AND legacy_id=OLD.id;
            ELSE
                DELETE FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id()
                  AND legacy_user_id=u AND table_name='follow_up_logs' AND legacy_id=OLD.id;
                DELETE FROM trosa.timeline_events WHERE id=target;
            END IF;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'communication identity is immutable';
    END IF;
    lid:=CASE WHEN TG_OP='INSERT' AND coalesce(NEW.id,0)=0
              THEN trosa.compat_next_id('follow_up_logs',u) ELSE NEW.id END;
    SELECT ar.account_id INTO account FROM trosa.account_legacy_refs ar
     WHERE ar.organization_id=trosa.compat_org_id() AND ar.legacy_user_id=u
       AND ar.legacy_customer_id=NEW.customer_id;
    IF account IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,u; END IF;
    SELECT cr.contact_method_id INTO contact FROM trosa.contact_legacy_refs cr
     WHERE cr.organization_id=trosa.compat_org_id() AND cr.legacy_user_id=u
       AND cr.legacy_contact_id=NEW.contact_id LIMIT 1;
    SELECT lr.target_id INTO target FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=u
       AND lr.table_name='follow_up_logs' AND lr.legacy_id=lid;
    target:=coalesce(target,trosa.compat_uuid('timeline:'||u||':'||lid::text));
    INSERT INTO trosa.timeline_events
        (id,account_id,contact_method_id,event_type,direction,content,result,next_plan,source_module,source_reference,occurred_at,payload)
    VALUES
        (target,account,contact,coalesce(NEW.activity_type,'follow_up'),coalesce(NEW.direction,'unknown'),
         coalesce(NEW.content,''),coalesce(NEW.result,''),coalesce(NEW.next_plan,''),'trosa',u||':'||lid::text,
         trosa.compat_time(NEW.follow_date),coalesce(to_jsonb(NEW),'{}'::jsonb))
    ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,event_type=excluded.event_type,direction=excluded.direction,
        contact_method_id=excluded.contact_method_id,content=excluded.content,result=excluded.result,next_plan=excluded.next_plan,
        occurred_at=excluded.occurred_at,payload=trosa.timeline_events.payload||excluded.payload;
    INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id)
    VALUES(trosa.compat_org_id(),u,'follow_up_logs',lid,target)
    ON CONFLICT(organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id;
    PERFORM trosa.compat_set_lastrowid(lid);
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trosa.compat_outreach_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    u text:=trosa.compat_current_user();
    lid bigint;
    account uuid;
    target uuid;
    contact uuid;
    keep_message boolean;
BEGIN
    IF TG_OP='DELETE' THEN
        SELECT lr.target_id INTO target FROM trosa.legacy_row_refs lr
         WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=u
           AND lr.table_name='outreach_emails' AND lr.legacy_id=OLD.id;
        IF target IS NOT NULL THEN
            keep_message := EXISTS (SELECT 1 FROM trosa.legacy_row_refs WHERE target_id=target
                                    AND NOT (legacy_user_id=u AND table_name='outreach_emails' AND legacy_id=OLD.id))
                         OR EXISTS (SELECT 1 FROM trosa.email_delivery_events WHERE outreach_message_id=target);
            IF keep_message THEN
                UPDATE trosa.outreach_messages SET legacy_payload=legacy_payload||jsonb_build_object(
                    'is_deleted','1','deleted_at',now()::text) WHERE id=target;
                DELETE FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id()
                  AND legacy_user_id=u AND table_name='outreach_emails' AND legacy_id=OLD.id;
            ELSE
                DELETE FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id()
                  AND legacy_user_id=u AND table_name='outreach_emails' AND legacy_id=OLD.id;
                DELETE FROM trosa.outreach_messages WHERE id=target;
            END IF;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'outreach identity is immutable';
    END IF;
    lid:=CASE WHEN TG_OP='INSERT' AND coalesce(NEW.id,0)=0
              THEN trosa.compat_next_id('outreach_emails',u) ELSE NEW.id END;
    SELECT ar.account_id INTO account FROM trosa.account_legacy_refs ar
     WHERE ar.organization_id=trosa.compat_org_id() AND ar.legacy_user_id=u
       AND ar.legacy_customer_id=NEW.customer_id;
    IF account IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,u; END IF;
    SELECT cr.contact_method_id INTO contact FROM trosa.contact_legacy_refs cr
     WHERE cr.organization_id=trosa.compat_org_id() AND cr.legacy_user_id=u
       AND cr.legacy_contact_id=NEW.contact_id LIMIT 1;
    SELECT lr.target_id INTO target FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=u
       AND lr.table_name='outreach_emails' AND lr.legacy_id=lid;
    target:=coalesce(target,trosa.compat_uuid('trosa-message:'||u||':'||lid::text));
    INSERT INTO trosa.outreach_messages
        (id,account_id,contact_method_id,subject,body,sent_at,reply_status,reply_content,reply_at,provider,provider_message_id,legacy_payload)
    VALUES
        (target,account,contact,coalesce(NEW.subject,''),coalesce(NEW.content,''),trosa.compat_time(NEW.sent_date),
         coalesce(NEW.reply_status,'pending'),coalesce(NEW.reply_content,''),trosa.compat_time(NEW.reply_date),'legacy',
         coalesce(nullif(NEW.message_id,''),nullif(NEW.external_id,''),''),coalesce(to_jsonb(NEW),'{}'::jsonb))
    ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,subject=excluded.subject,body=excluded.body,sent_at=excluded.sent_at,
        contact_method_id=excluded.contact_method_id,reply_status=excluded.reply_status,reply_content=excluded.reply_content,reply_at=excluded.reply_at,
        provider_message_id=excluded.provider_message_id,legacy_payload=trosa.outreach_messages.legacy_payload||excluded.legacy_payload;
    INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id)
    VALUES(trosa.compat_org_id(),u,'outreach_emails',lid,target)
    ON CONFLICT(organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id;
    PERFORM trosa.compat_set_lastrowid(lid);
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.customer_files_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    u text:=trosa.compat_current_user();
    lid bigint;
    account uuid;
    file_id uuid;
    storage_key text;
    old_file_id uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        SELECT file_object_id INTO old_file_id FROM trade_os_compat.customer_file_rows
         WHERE legacy_user_id=u AND id=OLD.id;
        UPDATE trade_os_compat.customer_file_rows SET is_deleted=1,deleted_at=now()::text
         WHERE legacy_user_id=u AND id=OLD.id;
        IF old_file_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM core.entity_files WHERE file_object_id=old_file_id
        ) THEN
            UPDATE core.file_objects SET deleted_at=now() WHERE id=old_file_id;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'customer file identity is immutable';
    END IF;
    lid:=CASE WHEN TG_OP='INSERT' AND coalesce(NEW.id,0)=0
              THEN trosa.compat_next_id('customer_files',u) ELSE NEW.id END;
    account:=trosa.compat_customer_account(NEW.customer_id,u);
    IF account IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,u; END IF;
    storage_key:=u||':'||CASE WHEN coalesce(NEW.file_path,'')<>'' THEN NEW.file_path
                              ELSE 'legacy-file:'||lid::text END;
    SELECT fo.id INTO file_id FROM core.file_objects fo
     WHERE fo.organization_id=trosa.compat_org_id() AND fo.storage_key=storage_key LIMIT 1;
    IF file_id IS NULL THEN
        SELECT file_object_id INTO old_file_id FROM trade_os_compat.customer_file_rows
         WHERE legacy_user_id=u AND id=lid LIMIT 1;
        file_id:=coalesce(old_file_id,trosa.compat_uuid('file:'||u||':'||lid::text));
    END IF;
    INSERT INTO core.file_objects
        (id,organization_id,storage_key,original_name,mime_type,size_bytes,sha256,uploaded_by_user_id,deleted_at)
    VALUES
        (file_id,trosa.compat_org_id(),storage_key,coalesce(NEW.original_name,''),coalesce(NEW.mime_type,''),
         greatest(coalesce(NEW.file_size,0),0),coalesce(NEW.sha256,''),
         (SELECT id FROM identity.users WHERE organization_id=trosa.compat_org_id()
            AND legacy_user_id=u LIMIT 1),
         CASE WHEN coalesce(NEW.is_deleted,0)<>0 THEN now() ELSE NULL END)
    ON CONFLICT(id) DO UPDATE SET storage_key=excluded.storage_key,original_name=excluded.original_name,
        mime_type=excluded.mime_type,size_bytes=excluded.size_bytes,sha256=excluded.sha256,
        uploaded_by_user_id=excluded.uploaded_by_user_id,deleted_at=excluded.deleted_at;
    INSERT INTO core.entity_files(id,file_object_id,account_id,relation_type)
    VALUES(trosa.compat_uuid('entity-file:'||u||':'||lid::text),file_id,account,'attachment')
    ON CONFLICT(id) DO UPDATE SET file_object_id=excluded.file_object_id,account_id=excluded.account_id,
        company_id=NULL,prospect_id=NULL,relation_type=excluded.relation_type;
    INSERT INTO trade_os_compat.customer_file_rows
        (legacy_user_id,id,customer_id,account_id,file_object_id,original_name,stored_name,file_path,file_size,
         mime_type,category,sha256,uploaded_by,is_deleted,deleted_at,created_at)
    VALUES
        (u,lid,NEW.customer_id,account,file_id,coalesce(NEW.original_name,''),coalesce(NEW.stored_name,''),
         coalesce(NEW.file_path,''),greatest(coalesce(NEW.file_size,0),0),coalesce(NEW.mime_type,''),
         coalesce(NEW.category,''),coalesce(NEW.sha256,''),coalesce(NEW.uploaded_by,''),coalesce(NEW.is_deleted,0),
         coalesce(NEW.deleted_at,''),coalesce(NEW.created_at,''))
    ON CONFLICT(legacy_user_id,id) DO UPDATE SET customer_id=excluded.customer_id,account_id=excluded.account_id,
        file_object_id=excluded.file_object_id,original_name=excluded.original_name,stored_name=excluded.stored_name,
        file_path=excluded.file_path,file_size=excluded.file_size,mime_type=excluded.mime_type,category=excluded.category,
        sha256=excluded.sha256,uploaded_by=excluded.uploaded_by,is_deleted=excluded.is_deleted,
        deleted_at=excluded.deleted_at,created_at=excluded.created_at;
    PERFORM trosa.compat_set_lastrowid(lid);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS inbox_items_write ON trade_os_compat.inbox_items;
CREATE TRIGGER inbox_items_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.inbox_items
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.inbox_items_write();
DROP TRIGGER IF EXISTS customer_files_write ON trade_os_compat.customer_files;
CREATE TRIGGER customer_files_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.customer_files
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.customer_files_write();

COMMIT;
