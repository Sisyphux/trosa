-- Keep the SQLite-facing ids of the email verification surfaces stable after
-- import.  The canonical tables use a database-wide identity column, while
-- the legacy API expects ids scoped to the current user's SQLite ledger.
-- 0002 added legacy_id/legacy_key, but the original views and writers still
-- exposed/returned the physical PostgreSQL id.  This forward migration makes
-- the projection and its write path use the same compatibility identity.

BEGIN;

-- 0009 could not recover the owner of a pre-existing globally unique undo
-- token when the compatibility ledgers contain that token for multiple users.
-- Quarantine that canonical fact instead of leaving it silently attributed to
-- whichever user happened to sort first.
UPDATE audit.undo_snapshots s
   SET legacy_user_id='legacy:ambiguous:'||s.id::text
 WHERE EXISTS (
           SELECT 1
             FROM trade_os_compat.undo_action_rows r
            WHERE r.token=s.token
       )
   AND (
       SELECT count(DISTINCT r.legacy_user_id)
         FROM trade_os_compat.undo_action_rows r
        WHERE r.token=s.token
   ) > 1
   AND s.legacy_user_id <> 'legacy:ambiguous:'||s.id::text;

-- Continue the same locked per-user allocation contract for the four
-- generated-id email tables.
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
    ELSIF $1='email_verifications' THEN
        SELECT coalesce(max(legacy_id),0)+1 INTO result
          FROM trosa.email_verifications
         WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=$2;
    ELSIF $1='email_verification_jobs' THEN
        SELECT coalesce(max(legacy_id),0)+1 INTO result
          FROM trosa.email_verification_jobs
         WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=$2;
    ELSIF $1='email_domain_probes' THEN
        SELECT coalesce(max(legacy_id),0)+1 INTO result
          FROM trosa.email_domain_probes
         WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=$2;
    ELSIF $1='email_logs' THEN
        SELECT coalesce(max(CASE WHEN legacy_key ~ '^[0-9]+$'
                                 THEN legacy_key::bigint ELSE 0 END),0)+1 INTO result
          FROM trosa.email_logs
         WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=$2;
    ELSE
        result:=1;
    END IF;
    RETURN result;
END
$$;

CREATE OR REPLACE VIEW trade_os_compat.email_verifications AS
SELECT coalesce(legacy_id,id) AS id, email, normalized_email, domain,
       deliverability_status, confidence, address_type,
       risk_flags::text AS risk_flags, evidence::text AS evidence,
       mx_records::text AS mx_records, checked_at, expires_at
  FROM trosa.email_verifications
 WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=trosa.compat_current_user();

CREATE OR REPLACE VIEW trade_os_compat.email_verification_jobs AS
SELECT coalesce(legacy_id,id) AS id, email, domain, status, attempts,
       next_run_at, last_error, created_at, updated_at
  FROM trosa.email_verification_jobs
 WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=trosa.compat_current_user();

CREATE OR REPLACE VIEW trade_os_compat.email_domain_probes AS
SELECT coalesce(legacy_id,id) AS id, domain, catchall_status,
       evidence::text AS evidence, checked_at, next_check_at
  FROM trosa.email_domain_probes
 WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=trosa.compat_current_user();

CREATE OR REPLACE VIEW trade_os_compat.email_logs AS
SELECT CASE WHEN legacy_key ~ '^[0-9]+$' THEN legacy_key::bigint ELSE id END AS id,
       status, message, reminder_count, created_at
  FROM trosa.email_logs
 WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=trosa.compat_current_user();

CREATE OR REPLACE FUNCTION trade_os_compat.email_verifications_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_user text := trosa.compat_current_user();
    v_email text := lower(trim(coalesce(NEW.email,'')));
    v_normalized text := lower(trim(coalesce(nullif(NEW.normalized_email,''),NEW.email,'')));
    v_physical_id bigint;
    v_existing_legacy_id bigint;
    v_legacy_id bigint;
    v_view_id bigint;
BEGIN
    IF TG_OP='DELETE' THEN
        SELECT ev.id INTO v_physical_id
          FROM trosa.email_verifications ev
         WHERE ev.organization_id=v_org AND ev.legacy_user_id=v_user
           AND (ev.legacy_id=OLD.id OR (ev.legacy_id IS NULL AND ev.id=OLD.id))
         ORDER BY CASE WHEN ev.legacy_id=OLD.id THEN 0 ELSE 1 END, ev.id
         LIMIT 1;
        IF v_physical_id IS NOT NULL THEN
            DELETE FROM trosa.email_verifications WHERE id=v_physical_id;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND (
        NEW.id IS DISTINCT FROM OLD.id
        OR v_normalized IS DISTINCT FROM lower(trim(coalesce(nullif(OLD.normalized_email,''),OLD.email,'')))
    ) THEN
        RAISE EXCEPTION 'email verification identity is immutable';
    END IF;
    IF v_email='' OR v_normalized='' THEN RAISE EXCEPTION 'email is required'; END IF;
    SELECT ev.id,ev.legacy_id INTO v_physical_id,v_existing_legacy_id
      FROM trosa.email_verifications ev
     WHERE ev.organization_id=v_org AND ev.legacy_user_id=v_user
       AND ev.normalized_email=v_normalized LIMIT 1;
    IF TG_OP='UPDATE' AND v_physical_id IS NULL THEN
        RAISE EXCEPTION 'email verification % is not visible for user %',OLD.id,v_user;
    END IF;
    v_legacy_id:=coalesce(v_existing_legacy_id,CASE WHEN TG_OP='UPDATE' THEN OLD.id ELSE nullif(NEW.id,0) END);
    IF v_legacy_id IS NULL THEN v_legacy_id:=trosa.compat_next_id('email_verifications',v_user); END IF;
    IF EXISTS (
        SELECT 1 FROM trosa.email_verifications ev
         WHERE ev.organization_id=v_org AND ev.legacy_user_id=v_user
           AND ev.legacy_id=v_legacy_id AND ev.id IS DISTINCT FROM v_physical_id
    ) THEN
        IF TG_OP='UPDATE' THEN v_legacy_id:=v_existing_legacy_id; ELSE v_legacy_id:=trosa.compat_next_id('email_verifications',v_user); END IF;
    END IF;
    INSERT INTO trosa.email_verifications
        (organization_id,legacy_user_id,legacy_id,email,normalized_email,domain,deliverability_status,
         confidence,address_type,risk_flags,evidence,mx_records,checked_at,expires_at)
    VALUES
        (v_org,v_user,v_legacy_id,v_email,v_normalized,coalesce(NEW.domain,''),
         coalesce(NEW.deliverability_status,'unknown'),coalesce(NEW.confidence,'low'),
         coalesce(NEW.address_type,'person'),trosa.compat_jsonb(NEW.risk_flags,'[]'::jsonb),
         trosa.compat_jsonb(NEW.evidence,'[]'::jsonb),trosa.compat_jsonb(NEW.mx_records,'[]'::jsonb),
         coalesce(NEW.checked_at,''),coalesce(NEW.expires_at,''))
    ON CONFLICT (organization_id,legacy_user_id,normalized_email) DO UPDATE SET
        legacy_id=coalesce(trosa.email_verifications.legacy_id,excluded.legacy_id),
        email=excluded.email,domain=excluded.domain,deliverability_status=excluded.deliverability_status,
        confidence=excluded.confidence,address_type=excluded.address_type,risk_flags=excluded.risk_flags,
        evidence=excluded.evidence,mx_records=excluded.mx_records,checked_at=excluded.checked_at,
        expires_at=excluded.expires_at;
    SELECT coalesce(ev.legacy_id,ev.id) INTO v_view_id
      FROM trosa.email_verifications ev
     WHERE ev.organization_id=v_org AND ev.legacy_user_id=v_user AND ev.normalized_email=v_normalized;
    PERFORM trosa.compat_set_lastrowid(v_view_id); RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.email_verification_jobs_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_user text := trosa.compat_current_user();
    v_email text := lower(trim(coalesce(NEW.email,'')));
    v_physical_id bigint;
    v_existing_legacy_id bigint;
    v_legacy_id bigint;
    v_view_id bigint;
BEGIN
    IF TG_OP='DELETE' THEN
        SELECT j.id INTO v_physical_id
          FROM trosa.email_verification_jobs j
         WHERE j.organization_id=v_org AND j.legacy_user_id=v_user
           AND (j.legacy_id=OLD.id OR (j.legacy_id IS NULL AND j.id=OLD.id))
         ORDER BY CASE WHEN j.legacy_id=OLD.id THEN 0 ELSE 1 END, j.id
         LIMIT 1;
        IF v_physical_id IS NOT NULL THEN DELETE FROM trosa.email_verification_jobs WHERE id=v_physical_id; END IF;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND (
        NEW.id IS DISTINCT FROM OLD.id
        OR v_email IS DISTINCT FROM lower(trim(coalesce(OLD.email,'')))
    ) THEN
        RAISE EXCEPTION 'email verification job identity is immutable';
    END IF;
    IF v_email='' THEN RAISE EXCEPTION 'email is required'; END IF;
    SELECT j.id,j.legacy_id INTO v_physical_id,v_existing_legacy_id
      FROM trosa.email_verification_jobs j
     WHERE j.organization_id=v_org AND j.legacy_user_id=v_user AND j.email=v_email LIMIT 1;
    IF TG_OP='UPDATE' AND v_physical_id IS NULL THEN
        RAISE EXCEPTION 'email verification job % is not visible for user %',OLD.id,v_user;
    END IF;
    v_legacy_id:=coalesce(v_existing_legacy_id,CASE WHEN TG_OP='UPDATE' THEN OLD.id ELSE nullif(NEW.id,0) END);
    IF v_legacy_id IS NULL THEN v_legacy_id:=trosa.compat_next_id('email_verification_jobs',v_user); END IF;
    IF EXISTS (
        SELECT 1 FROM trosa.email_verification_jobs j
         WHERE j.organization_id=v_org AND j.legacy_user_id=v_user
           AND j.legacy_id=v_legacy_id AND j.id IS DISTINCT FROM v_physical_id
    ) THEN
        IF TG_OP='UPDATE' THEN v_legacy_id:=v_existing_legacy_id; ELSE v_legacy_id:=trosa.compat_next_id('email_verification_jobs',v_user); END IF;
    END IF;
    INSERT INTO trosa.email_verification_jobs
        (organization_id,legacy_user_id,legacy_id,email,domain,status,attempts,next_run_at,last_error,created_at,updated_at)
    VALUES
        (v_org,v_user,v_legacy_id,v_email,coalesce(NEW.domain,''),coalesce(NEW.status,'queued'),
         coalesce(NEW.attempts,0),coalesce(NEW.next_run_at,''),coalesce(NEW.last_error,''),
         coalesce(NEW.created_at,''),coalesce(NEW.updated_at,''))
    ON CONFLICT (organization_id,legacy_user_id,email) DO UPDATE SET
        legacy_id=coalesce(trosa.email_verification_jobs.legacy_id,excluded.legacy_id),
        domain=excluded.domain,status=excluded.status,attempts=excluded.attempts,
        next_run_at=excluded.next_run_at,last_error=excluded.last_error,updated_at=excluded.updated_at;
    SELECT coalesce(j.legacy_id,j.id) INTO v_view_id
      FROM trosa.email_verification_jobs j
     WHERE j.organization_id=v_org AND j.legacy_user_id=v_user AND j.email=v_email;
    PERFORM trosa.compat_set_lastrowid(v_view_id); RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.email_domain_probes_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_user text := trosa.compat_current_user();
    v_domain text := lower(trim(coalesce(NEW.domain,'')));
    v_physical_id bigint;
    v_existing_legacy_id bigint;
    v_legacy_id bigint;
    v_view_id bigint;
BEGIN
    IF TG_OP='DELETE' THEN
        SELECT p.id INTO v_physical_id
          FROM trosa.email_domain_probes p
         WHERE p.organization_id=v_org AND p.legacy_user_id=v_user
           AND (p.legacy_id=OLD.id OR (p.legacy_id IS NULL AND p.id=OLD.id))
         ORDER BY CASE WHEN p.legacy_id=OLD.id THEN 0 ELSE 1 END, p.id
         LIMIT 1;
        IF v_physical_id IS NOT NULL THEN DELETE FROM trosa.email_domain_probes WHERE id=v_physical_id; END IF;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND (
        NEW.id IS DISTINCT FROM OLD.id
        OR v_domain IS DISTINCT FROM lower(trim(coalesce(OLD.domain,'')))
    ) THEN
        RAISE EXCEPTION 'email domain probe identity is immutable';
    END IF;
    IF v_domain='' THEN RAISE EXCEPTION 'domain is required'; END IF;
    SELECT p.id,p.legacy_id INTO v_physical_id,v_existing_legacy_id
      FROM trosa.email_domain_probes p
     WHERE p.organization_id=v_org AND p.legacy_user_id=v_user AND p.domain=v_domain LIMIT 1;
    IF TG_OP='UPDATE' AND v_physical_id IS NULL THEN
        RAISE EXCEPTION 'email domain probe % is not visible for user %',OLD.id,v_user;
    END IF;
    v_legacy_id:=coalesce(v_existing_legacy_id,CASE WHEN TG_OP='UPDATE' THEN OLD.id ELSE nullif(NEW.id,0) END);
    IF v_legacy_id IS NULL THEN v_legacy_id:=trosa.compat_next_id('email_domain_probes',v_user); END IF;
    IF EXISTS (
        SELECT 1 FROM trosa.email_domain_probes p
         WHERE p.organization_id=v_org AND p.legacy_user_id=v_user
           AND p.legacy_id=v_legacy_id AND p.id IS DISTINCT FROM v_physical_id
    ) THEN
        IF TG_OP='UPDATE' THEN v_legacy_id:=v_existing_legacy_id; ELSE v_legacy_id:=trosa.compat_next_id('email_domain_probes',v_user); END IF;
    END IF;
    INSERT INTO trosa.email_domain_probes
        (organization_id,legacy_user_id,legacy_id,domain,catchall_status,evidence,checked_at,next_check_at)
    VALUES
        (v_org,v_user,v_legacy_id,v_domain,coalesce(NEW.catchall_status,'unknown'),
         trosa.compat_jsonb(NEW.evidence,'[]'::jsonb),coalesce(NEW.checked_at,''),coalesce(NEW.next_check_at,''))
    ON CONFLICT (organization_id,legacy_user_id,domain) DO UPDATE SET
        legacy_id=coalesce(trosa.email_domain_probes.legacy_id,excluded.legacy_id),
        catchall_status=excluded.catchall_status,evidence=excluded.evidence,
        checked_at=excluded.checked_at,next_check_at=excluded.next_check_at;
    SELECT coalesce(p.legacy_id,p.id) INTO v_view_id
      FROM trosa.email_domain_probes p
     WHERE p.organization_id=v_org AND p.legacy_user_id=v_user AND p.domain=v_domain;
    PERFORM trosa.compat_set_lastrowid(v_view_id); RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.email_logs_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_user text := trosa.compat_current_user();
    v_physical_id bigint;
    v_key text;
    v_legacy_id bigint;
    v_view_id bigint;
BEGIN
    IF TG_OP='DELETE' THEN
        SELECT l.id INTO v_physical_id
          FROM trosa.email_logs l
         WHERE l.organization_id=v_org AND l.legacy_user_id=v_user
           AND (l.id=OLD.id OR (CASE WHEN l.legacy_key ~ '^[0-9]+$' THEN l.legacy_key::bigint END=OLD.id))
         ORDER BY CASE WHEN (CASE WHEN l.legacy_key ~ '^[0-9]+$' THEN l.legacy_key::bigint END)=OLD.id THEN 0 ELSE 1 END, l.id
         LIMIT 1;
        IF v_physical_id IS NOT NULL THEN DELETE FROM trosa.email_logs WHERE id=v_physical_id; END IF;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' THEN
        IF NEW.id IS DISTINCT FROM OLD.id THEN RAISE EXCEPTION 'email log identity is immutable'; END IF;
        SELECT l.id,l.legacy_key INTO v_physical_id,v_key
          FROM trosa.email_logs l
         WHERE l.organization_id=v_org AND l.legacy_user_id=v_user
           AND (l.id=OLD.id OR (CASE WHEN l.legacy_key ~ '^[0-9]+$' THEN l.legacy_key::bigint END=OLD.id))
         ORDER BY CASE WHEN (CASE WHEN l.legacy_key ~ '^[0-9]+$' THEN l.legacy_key::bigint END)=OLD.id THEN 0 ELSE 1 END, l.id
         LIMIT 1;
        IF v_physical_id IS NULL THEN RAISE EXCEPTION 'email log % is not visible for user %',OLD.id,v_user; END IF;
        UPDATE trosa.email_logs
           SET status=coalesce(NEW.status,''),message=coalesce(NEW.message,''),
               reminder_count=coalesce(NEW.reminder_count,0),created_at=coalesce(NEW.created_at,'')
         WHERE id=v_physical_id;
        v_view_id:=CASE WHEN v_key ~ '^[0-9]+$' THEN v_key::bigint ELSE v_physical_id END;
    ELSE
        v_legacy_id:=coalesce(nullif(NEW.id,0),trosa.compat_next_id('email_logs',v_user));
        IF EXISTS (SELECT 1 FROM trosa.email_logs l WHERE l.organization_id=v_org AND l.legacy_user_id=v_user AND l.legacy_key=v_legacy_id::text) THEN
            v_legacy_id:=trosa.compat_next_id('email_logs',v_user);
        END IF;
        v_key:=v_legacy_id::text;
        INSERT INTO trosa.email_logs(organization_id,legacy_user_id,legacy_key,status,message,reminder_count,created_at)
        VALUES(v_org,v_user,v_key,coalesce(NEW.status,''),coalesce(NEW.message,''),coalesce(NEW.reminder_count,0),coalesce(NEW.created_at,''))
        RETURNING id INTO v_physical_id;
        v_view_id:=v_legacy_id;
    END IF;
    PERFORM trosa.compat_set_lastrowid(v_view_id); RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS email_verifications_write ON trade_os_compat.email_verifications;
CREATE TRIGGER email_verifications_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.email_verifications
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.email_verifications_write();
DROP TRIGGER IF EXISTS email_verification_jobs_write ON trade_os_compat.email_verification_jobs;
CREATE TRIGGER email_verification_jobs_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.email_verification_jobs
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.email_verification_jobs_write();
DROP TRIGGER IF EXISTS email_domain_probes_write ON trade_os_compat.email_domain_probes;
CREATE TRIGGER email_domain_probes_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.email_domain_probes
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.email_domain_probes_write();
DROP TRIGGER IF EXISTS email_logs_write ON trade_os_compat.email_logs;
CREATE TRIGGER email_logs_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.email_logs
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.email_logs_write();

COMMIT;
