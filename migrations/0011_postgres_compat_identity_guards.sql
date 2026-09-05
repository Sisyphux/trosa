-- Close the last legacy-key mutation holes in compatibility writers.
--
-- The views intentionally expose SQLite-shaped ids and natural keys.  Those
-- values are references used by old code, not editable attributes.  Without
-- these guards an UPDATE that changed an id/email/domain could create a new
-- canonical row while leaving the original row behind or could update a
-- different natural-key row.  This is a forward migration so an already
-- applied installation never has to replay an earlier view migration.

BEGIN;

CREATE OR REPLACE FUNCTION trade_os_compat.research_reports_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE u text:=trosa.compat_current_user(); lid bigint; target uuid; account uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='research_reports' AND legacy_id=OLD.id;
        IF target IS NOT NULL THEN DELETE FROM trosa.research_reports WHERE id=target; DELETE FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='research_reports' AND legacy_id=OLD.id; END IF;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'research report identity is immutable';
    END IF;
    lid:=CASE WHEN TG_OP='INSERT' AND coalesce(NEW.id,0)=0 THEN trosa.compat_next_id('research_reports',u) ELSE NEW.id END;
    account:=trosa.compat_customer_account(NEW.customer_id,u);
    IF account IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,u; END IF;
    SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='research_reports' AND legacy_id=lid;
    target:=coalesce(target,trosa.compat_uuid('research:'||u||':'||lid::text));
    INSERT INTO trosa.research_reports(id,account_id,summary,company_info,key_findings,needs_analysis,cooperation_value,raw_input,source,web_content,web_fetched_at,expires_at,legacy_payload,updated_at)
    VALUES (target,account,coalesce(NEW.summary,''),coalesce(NEW.company_info,''),coalesce(NEW.key_findings,''),coalesce(NEW.needs_analysis,''),coalesce(NEW.cooperation_value,''),coalesce(NEW.raw_input,''),coalesce(NEW.source,''),coalesce(NEW.web_content,''),coalesce(NEW.web_fetched_at,''),coalesce(NEW.expires_at,''),to_jsonb(NEW),now())
    ON CONFLICT (id) DO UPDATE SET account_id=excluded.account_id,summary=excluded.summary,company_info=excluded.company_info,key_findings=excluded.key_findings,needs_analysis=excluded.needs_analysis,cooperation_value=excluded.cooperation_value,raw_input=excluded.raw_input,source=excluded.source,web_content=excluded.web_content,web_fetched_at=excluded.web_fetched_at,expires_at=excluded.expires_at,legacy_payload=trosa.research_reports.legacy_payload||excluded.legacy_payload,updated_at=now();
    INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id) VALUES(trosa.compat_org_id(),u,'research_reports',lid,target) ON CONFLICT(organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id;
    PERFORM trosa.compat_set_lastrowid(lid); RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.external_analysis_notes_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE u text:=trosa.compat_current_user(); lid bigint; target uuid; account uuid;
BEGIN
    IF TG_OP='DELETE' THEN SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='external_analysis_notes' AND legacy_id=OLD.id; IF target IS NOT NULL THEN DELETE FROM trosa.external_analysis_notes WHERE id=target; DELETE FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='external_analysis_notes' AND legacy_id=OLD.id; END IF; RETURN OLD; END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'external analysis note identity is immutable';
    END IF;
    lid:=CASE WHEN TG_OP='INSERT' AND coalesce(NEW.id,0)=0 THEN trosa.compat_next_id('external_analysis_notes',u) ELSE NEW.id END; account:=trosa.compat_customer_account(NEW.customer_id,u); IF account IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,u; END IF;
    SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='external_analysis_notes' AND legacy_id=lid; target:=coalesce(target,trosa.compat_uuid('external-note:'||u||':'||lid::text));
    INSERT INTO trosa.external_analysis_notes(id,account_id,content,source,legacy_payload,updated_at) VALUES(target,account,coalesce(NEW.content,''),coalesce(NEW.source,'external_model'),to_jsonb(NEW),now()) ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,content=excluded.content,source=excluded.source,legacy_payload=trosa.external_analysis_notes.legacy_payload||excluded.legacy_payload,updated_at=now();
    INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id) VALUES(trosa.compat_org_id(),u,'external_analysis_notes',lid,target) ON CONFLICT(organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id; PERFORM trosa.compat_set_lastrowid(lid); RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.customer_understandings_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE u text:=trosa.compat_current_user(); lid bigint; target uuid; account uuid; source_event uuid;
BEGIN
    IF TG_OP='DELETE' THEN SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='customer_understandings' AND legacy_id=OLD.id; IF target IS NOT NULL THEN DELETE FROM trosa.account_understandings WHERE id=target; DELETE FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='customer_understandings' AND legacy_id=OLD.id; END IF; RETURN OLD; END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'customer understanding identity is immutable';
    END IF;
    lid:=CASE WHEN TG_OP='INSERT' AND coalesce(NEW.id,0)=0 THEN trosa.compat_next_id('customer_understandings',u) ELSE NEW.id END; account:=trosa.compat_customer_account(NEW.customer_id,u); IF account IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,u; END IF;
    SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='customer_understandings' AND legacy_id=lid; SELECT target_id INTO source_event FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='follow_up_logs' AND legacy_id=NEW.source_activity_id; target:=coalesce(target,trosa.compat_uuid('understanding:'||account::text));
    INSERT INTO trosa.account_understandings(id,account_id,current_summary,recent_change,open_loops,action_state,action_reason,source_timeline_event_id,version,updated_at) VALUES(target,account,coalesce(NEW.current_summary,''),coalesce(NEW.recent_change,''),trosa.compat_jsonb(NEW.open_loops,'[]'::jsonb),coalesce(NEW.action_state,'hold'),coalesce(NEW.action_reason,''),source_event,coalesce(NEW.version,1),now()) ON CONFLICT(account_id) DO UPDATE SET current_summary=excluded.current_summary,recent_change=excluded.recent_change,open_loops=excluded.open_loops,action_state=excluded.action_state,action_reason=excluded.action_reason,source_timeline_event_id=excluded.source_timeline_event_id,version=excluded.version,updated_at=now();
    SELECT id INTO target FROM trosa.account_understandings WHERE account_id=account; INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id) VALUES(trosa.compat_org_id(),u,'customer_understandings',lid,target) ON CONFLICT(organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id; PERFORM trosa.compat_set_lastrowid(lid); RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.ai_recommendations_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE u text:=trosa.compat_current_user(); lid bigint; target uuid; account uuid; source_event uuid;
BEGIN
    IF TG_OP='DELETE' THEN SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='ai_recommendations' AND legacy_id=OLD.id; IF target IS NOT NULL THEN DELETE FROM trosa.ai_recommendations WHERE id=target; DELETE FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='ai_recommendations' AND legacy_id=OLD.id; END IF; RETURN OLD; END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'AI recommendation identity is immutable';
    END IF;
    lid:=CASE WHEN TG_OP='INSERT' AND coalesce(NEW.id,0)=0 THEN trosa.compat_next_id('ai_recommendations',u) ELSE NEW.id END; account:=trosa.compat_customer_account(NEW.customer_id,u); IF account IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,u; END IF; SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='ai_recommendations' AND legacy_id=lid; SELECT target_id INTO source_event FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='follow_up_logs' AND legacy_id=NEW.source_activity_id; target:=coalesce(target,trosa.compat_uuid('recommendation:'||u||':'||lid::text));
    INSERT INTO trosa.ai_recommendations(id,account_id,understanding_version,content,reason,source_timeline_event_id,review_status,user_response,user_modified_content,executed_action,outcome,updated_at) VALUES(target,account,coalesce(NEW.understanding_version,0),coalesce(NEW.content,''),coalesce(NEW.reason,''),source_event,coalesce(NEW.review_status,'hold'),coalesce(NEW.user_response,''),coalesce(NEW.user_modified_content,''),coalesce(NEW.executed_action,''),coalesce(NEW.outcome,''),now()) ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,understanding_version=excluded.understanding_version,content=excluded.content,reason=excluded.reason,source_timeline_event_id=excluded.source_timeline_event_id,review_status=excluded.review_status,user_response=excluded.user_response,user_modified_content=excluded.user_modified_content,executed_action=excluded.executed_action,outcome=excluded.outcome,updated_at=now();
    INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id) VALUES(trosa.compat_org_id(),u,'ai_recommendations',lid,target) ON CONFLICT(organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id; PERFORM trosa.compat_set_lastrowid(lid); RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.users_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_username text := lower(trim(coalesce(nullif(NEW.username, ''), nullif(NEW.id, ''), '')));
    v_user_id uuid;
    v_active boolean := coalesce(NEW.active, 1) <> 0;
    v_role text := coalesce(nullif(NEW.role, ''), 'member');
BEGIN
    IF TG_OP = 'DELETE' THEN
        UPDATE identity.users
           SET active=false, status='inactive', updated_at=now()
         WHERE organization_id=v_org AND (username=OLD.username OR legacy_user_id=OLD.id);
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND (
        NEW.id IS DISTINCT FROM OLD.id
        OR v_username IS DISTINCT FROM lower(trim(coalesce(nullif(OLD.username,''),nullif(OLD.id,''),'')))
    ) THEN
        RAISE EXCEPTION 'user identity is immutable';
    END IF;
    IF v_username = '' THEN RAISE EXCEPTION 'username is required'; END IF;
    SELECT u.id INTO v_user_id
      FROM identity.users u
     WHERE u.organization_id=v_org AND (u.username=v_username OR u.legacy_user_id=v_username)
     LIMIT 1;
    v_user_id := coalesce(v_user_id, trosa.compat_uuid('user:' || v_username));
    INSERT INTO identity.users
        (id, organization_id, legacy_user_id, username, display_name, label, color,
         password_hash, role, created_by, active, status, legacy_payload)
    VALUES
        (v_user_id, v_org, v_username, v_username,
         coalesce(nullif(NEW.name, ''), v_username), coalesce(NEW.label, ''), coalesce(NEW.color, ''),
         coalesce(NEW.password_hash, ''), v_role, coalesce(NEW.created_by, ''), v_active,
         CASE WHEN v_active THEN 'active' ELSE 'inactive' END, to_jsonb(NEW))
    ON CONFLICT (id) DO UPDATE SET
        legacy_user_id=excluded.legacy_user_id, username=excluded.username,
        display_name=excluded.display_name, label=excluded.label, color=excluded.color,
        password_hash=excluded.password_hash, role=excluded.role, created_by=excluded.created_by,
        active=excluded.active, status=excluded.status,
        legacy_payload=identity.users.legacy_payload || excluded.legacy_payload, updated_at=now();
    INSERT INTO identity.memberships (organization_id, user_id, role, status)
    VALUES (v_org, v_user_id, v_role, CASE WHEN v_active THEN 'active' ELSE 'inactive' END)
    ON CONFLICT (organization_id, user_id) DO UPDATE SET role=excluded.role, status=excluded.status;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.email_verifications_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_user text := trosa.compat_current_user();
    v_email text := lower(trim(coalesce(NEW.email, '')));
    v_normalized text := lower(trim(coalesce(nullif(NEW.normalized_email, ''), NEW.email, '')));
    v_id bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM trosa.email_verifications WHERE organization_id=v_org AND legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND (
        NEW.id IS DISTINCT FROM OLD.id
        OR v_normalized IS DISTINCT FROM lower(trim(coalesce(nullif(OLD.normalized_email,''),OLD.email,'')))
    ) THEN
        RAISE EXCEPTION 'email verification identity is immutable';
    END IF;
    IF v_email = '' OR v_normalized = '' THEN RAISE EXCEPTION 'email is required'; END IF;
    INSERT INTO trosa.email_verifications
        (organization_id, legacy_user_id, email, normalized_email, domain,
         deliverability_status, confidence, address_type, risk_flags, evidence,
         mx_records, checked_at, expires_at)
    VALUES
        (v_org, v_user, v_email, v_normalized, coalesce(NEW.domain, ''),
         coalesce(NEW.deliverability_status, 'unknown'), coalesce(NEW.confidence, 'low'),
         coalesce(NEW.address_type, 'person'), trosa.compat_jsonb(NEW.risk_flags, '[]'::jsonb),
         trosa.compat_jsonb(NEW.evidence, '[]'::jsonb), trosa.compat_jsonb(NEW.mx_records, '[]'::jsonb),
         coalesce(NEW.checked_at, ''), coalesce(NEW.expires_at, ''))
    ON CONFLICT (organization_id, legacy_user_id, normalized_email) DO UPDATE SET
        email=excluded.email, domain=excluded.domain,
        deliverability_status=excluded.deliverability_status, confidence=excluded.confidence,
        address_type=excluded.address_type, risk_flags=excluded.risk_flags, evidence=excluded.evidence,
        mx_records=excluded.mx_records, checked_at=excluded.checked_at, expires_at=excluded.expires_at;
    SELECT ev.id INTO v_id FROM trosa.email_verifications ev
     WHERE ev.organization_id=v_org AND ev.legacy_user_id=v_user AND ev.normalized_email=v_normalized LIMIT 1;
    PERFORM trosa.compat_set_lastrowid(v_id); RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.email_verification_jobs_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id(); v_user text := trosa.compat_current_user();
    v_email text := lower(trim(coalesce(NEW.email, ''))); v_id bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM trosa.email_verification_jobs WHERE organization_id=v_org AND legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND (
        NEW.id IS DISTINCT FROM OLD.id
        OR v_email IS DISTINCT FROM lower(trim(coalesce(OLD.email,'')))
    ) THEN
        RAISE EXCEPTION 'email verification job identity is immutable';
    END IF;
    IF v_email = '' THEN RAISE EXCEPTION 'email is required'; END IF;
    INSERT INTO trosa.email_verification_jobs
        (organization_id, legacy_user_id, email, domain, status, attempts,
         next_run_at, last_error, created_at, updated_at)
    VALUES
        (v_org, v_user, v_email, coalesce(NEW.domain, ''), coalesce(NEW.status, 'queued'),
         coalesce(NEW.attempts, 0), coalesce(NEW.next_run_at, ''), coalesce(NEW.last_error, ''),
         coalesce(NEW.created_at, ''), coalesce(NEW.updated_at, ''))
    ON CONFLICT (organization_id, legacy_user_id, email) DO UPDATE SET
        domain=excluded.domain, status=excluded.status, attempts=excluded.attempts,
        next_run_at=excluded.next_run_at, last_error=excluded.last_error, updated_at=excluded.updated_at;
    SELECT j.id INTO v_id FROM trosa.email_verification_jobs j
     WHERE j.organization_id=v_org AND j.legacy_user_id=v_user AND j.email=v_email LIMIT 1;
    PERFORM trosa.compat_set_lastrowid(v_id); RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.email_domain_probes_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id(); v_user text := trosa.compat_current_user();
    v_domain text := lower(trim(coalesce(NEW.domain, ''))); v_id bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM trosa.email_domain_probes WHERE organization_id=v_org AND legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND (
        NEW.id IS DISTINCT FROM OLD.id
        OR v_domain IS DISTINCT FROM lower(trim(coalesce(OLD.domain,'')))
    ) THEN
        RAISE EXCEPTION 'email domain probe identity is immutable';
    END IF;
    IF v_domain = '' THEN RAISE EXCEPTION 'domain is required'; END IF;
    INSERT INTO trosa.email_domain_probes
        (organization_id, legacy_user_id, domain, catchall_status, evidence, checked_at, next_check_at)
    VALUES
        (v_org, v_user, v_domain, coalesce(NEW.catchall_status, 'unknown'),
         trosa.compat_jsonb(NEW.evidence, '[]'::jsonb), coalesce(NEW.checked_at, ''), coalesce(NEW.next_check_at, ''))
    ON CONFLICT (organization_id, legacy_user_id, domain) DO UPDATE SET
        catchall_status=excluded.catchall_status, evidence=excluded.evidence,
        checked_at=excluded.checked_at, next_check_at=excluded.next_check_at;
    SELECT p.id INTO v_id FROM trosa.email_domain_probes p
     WHERE p.organization_id=v_org AND p.legacy_user_id=v_user AND p.domain=v_domain LIMIT 1;
    PERFORM trosa.compat_set_lastrowid(v_id); RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.email_logs_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id(); v_user text := trosa.compat_current_user(); v_id bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM trosa.email_logs WHERE organization_id=v_org AND legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.id IS DISTINCT FROM OLD.id THEN
            RAISE EXCEPTION 'email log identity is immutable';
        END IF;
        UPDATE trosa.email_logs
           SET status=coalesce(NEW.status, ''), message=coalesce(NEW.message, ''),
               reminder_count=coalesce(NEW.reminder_count, 0), created_at=coalesce(NEW.created_at, '')
         WHERE organization_id=v_org AND legacy_user_id=v_user AND id=OLD.id;
        IF NOT FOUND THEN RAISE EXCEPTION 'email log % is not visible for user %',OLD.id,v_user; END IF;
        v_id := OLD.id;
    ELSE
        INSERT INTO trosa.email_logs
            (organization_id, legacy_user_id, status, message, reminder_count, created_at)
        VALUES (v_org, v_user, coalesce(NEW.status, ''), coalesce(NEW.message, ''),
                coalesce(NEW.reminder_count, 0), coalesce(NEW.created_at, ''))
        RETURNING id INTO v_id;
    END IF;
    PERFORM trosa.compat_set_lastrowid(v_id); RETURN NEW;
END
$$;

COMMIT;
