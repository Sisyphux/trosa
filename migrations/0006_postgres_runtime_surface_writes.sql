-- Complete the PostgreSQL runtime surface for the existing Trosa API.
--
-- 0003/0004 exposed the remaining SQLite-shaped relations as views.  This
-- forward migration makes every relation that the application actually
-- writes either update a canonical trosa/identity/audit row or its explicit
-- PostgreSQL compatibility ledger.  The ledgers retain legacy integer ids;
-- they are not a second database and do not contain duplicate company/person
-- facts.

BEGIN;

CREATE OR REPLACE FUNCTION trosa.compat_time(value text)
RETURNS timestamptz
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF NULLIF(trim(coalesce(value, '')), '') IS NULL THEN
        RETURN NULL;
    END IF;
    RETURN trim(value)::timestamptz;
EXCEPTION WHEN others THEN
    RETURN NULL;
END
$$;

-- User rows are the identity source for both applications.  The view keeps
-- the old username-shaped API while writes land in identity.users and its
-- membership row.
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
    IF v_username = '' THEN
        RAISE EXCEPTION 'username is required';
    END IF;

    SELECT u.id INTO v_user_id
      FROM identity.users u
     WHERE u.organization_id=v_org
       AND (u.username=v_username OR u.legacy_user_id=v_username)
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
        legacy_user_id=excluded.legacy_user_id,
        username=excluded.username,
        display_name=excluded.display_name,
        label=excluded.label,
        color=excluded.color,
        password_hash=excluded.password_hash,
        role=excluded.role,
        created_by=excluded.created_by,
        active=excluded.active,
        status=excluded.status,
        legacy_payload=identity.users.legacy_payload || excluded.legacy_payload,
        updated_at=now();
    INSERT INTO identity.memberships (organization_id, user_id, role, status)
    VALUES (v_org, v_user_id, v_role, CASE WHEN v_active THEN 'active' ELSE 'inactive' END)
    ON CONFLICT (organization_id, user_id) DO UPDATE SET role=excluded.role, status=excluded.status;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS users_write ON trade_os_compat.users;
CREATE TRIGGER users_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.users
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.users_write();

-- Email verification is a current-state cache, so the existing API's
-- upserts update trosa.email_verifications rather than a compatibility copy.
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
        DELETE FROM trosa.email_verifications
         WHERE organization_id=v_org AND legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF v_email = '' OR v_normalized = '' THEN
        RAISE EXCEPTION 'email is required';
    END IF;
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
    SELECT ev.id INTO v_id
      FROM trosa.email_verifications ev
     WHERE ev.organization_id=v_org AND ev.legacy_user_id=v_user AND ev.normalized_email=v_normalized
     LIMIT 1;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS email_verifications_write ON trade_os_compat.email_verifications;
CREATE TRIGGER email_verifications_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.email_verifications
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.email_verifications_write();

CREATE OR REPLACE FUNCTION trade_os_compat.email_verification_jobs_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_user text := trosa.compat_current_user();
    v_email text := lower(trim(coalesce(NEW.email, '')));
    v_id bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM trosa.email_verification_jobs
         WHERE organization_id=v_org AND legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
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
    SELECT j.id INTO v_id
      FROM trosa.email_verification_jobs j
     WHERE j.organization_id=v_org AND j.legacy_user_id=v_user AND j.email=v_email
     LIMIT 1;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS email_verification_jobs_write ON trade_os_compat.email_verification_jobs;
CREATE TRIGGER email_verification_jobs_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.email_verification_jobs
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.email_verification_jobs_write();

CREATE OR REPLACE FUNCTION trade_os_compat.email_domain_probes_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_user text := trosa.compat_current_user();
    v_domain text := lower(trim(coalesce(NEW.domain, '')));
    v_id bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM trosa.email_domain_probes
         WHERE organization_id=v_org AND legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
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
    SELECT p.id INTO v_id
      FROM trosa.email_domain_probes p
     WHERE p.organization_id=v_org AND p.legacy_user_id=v_user AND p.domain=v_domain
     LIMIT 1;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS email_domain_probes_write ON trade_os_compat.email_domain_probes;
CREATE TRIGGER email_domain_probes_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.email_domain_probes
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.email_domain_probes_write();

CREATE OR REPLACE FUNCTION trade_os_compat.email_logs_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_user text := trosa.compat_current_user();
    v_id bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM trosa.email_logs
         WHERE organization_id=v_org AND legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        UPDATE trosa.email_logs
           SET status=coalesce(NEW.status, ''), message=coalesce(NEW.message, ''),
               reminder_count=coalesce(NEW.reminder_count, 0), created_at=coalesce(NEW.created_at, '')
         WHERE organization_id=v_org AND legacy_user_id=v_user AND id=NEW.id;
        v_id := NEW.id;
    ELSE
        INSERT INTO trosa.email_logs
            (organization_id, legacy_user_id, status, message, reminder_count, created_at)
        VALUES (v_org, v_user, coalesce(NEW.status, ''), coalesce(NEW.message, ''),
                coalesce(NEW.reminder_count, 0), coalesce(NEW.created_at, ''))
        RETURNING id INTO v_id;
    END IF;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS email_logs_write ON trade_os_compat.email_logs;
CREATE TRIGGER email_logs_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.email_logs
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.email_logs_write();

-- Provider receipt, canonical communication source and source-item writes.
-- These are the rows consumed by Gmail sync and the browser-extension import.
CREATE OR REPLACE FUNCTION trade_os_compat.gmail_message_states_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_user text := trosa.compat_current_user();
    v_id bigint;
    v_provider text;
    v_receipt uuid;
    v_account uuid;
    v_contact uuid;
    v_timeline uuid;
    v_inbox uuid;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT provider_message_id INTO v_provider
          FROM trade_os_compat.gmail_message_state_rows
         WHERE legacy_user_id=v_user AND id=OLD.id;
        DELETE FROM trosa.email_message_receipts
         WHERE organization_id=v_org AND provider_message_id=coalesce(v_provider, '');
        DELETE FROM trade_os_compat.gmail_message_state_rows WHERE legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;

    v_provider := nullif(trim(coalesce(NEW.provider_message_id, '')), '');
    SELECT id INTO v_id
      FROM trade_os_compat.gmail_message_state_rows
     WHERE legacy_user_id=v_user AND provider_message_id=coalesce(v_provider, '')
     LIMIT 1;
    IF v_id IS NULL THEN
        v_id := CASE WHEN coalesce(NEW.id, 0) <> 0 THEN NEW.id
                     ELSE (SELECT coalesce(max(id), 0) + 1 FROM trade_os_compat.gmail_message_state_rows WHERE legacy_user_id=v_user)
                END;
    END IF;
    v_provider := coalesce(v_provider, 'legacy:' || v_user || ':' || v_id::text);
    v_account := trosa.compat_customer_account(NEW.customer_id, v_user);
    SELECT cr.contact_method_id INTO v_contact
      FROM trosa.contact_legacy_refs cr
     WHERE cr.organization_id=v_org AND cr.legacy_user_id=v_user AND cr.legacy_contact_id=NEW.contact_id
     LIMIT 1;
    SELECT lr.target_id INTO v_timeline
      FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=v_org AND lr.legacy_user_id=v_user
       AND lr.table_name='follow_up_logs' AND lr.legacy_id=NEW.activity_id
     LIMIT 1;
    SELECT lr.target_id INTO v_inbox
      FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=v_org AND lr.legacy_user_id=v_user
       AND lr.table_name='inbox_items' AND lr.legacy_id=NEW.inbox_item_id
     LIMIT 1;
    v_receipt := trosa.compat_uuid('gmail-receipt:' || v_user || ':' || v_provider);
    INSERT INTO trosa.email_message_receipts
        (id, organization_id, provider_message_id, provider_thread_id, message_time,
         sender_email, recipient_emails, subject, account_id, contact_method_id,
         timeline_event_id, inbox_item_id, match_status, raw_payload, last_error,
         created_at, updated_at)
    VALUES
        (v_receipt, v_org, v_provider, coalesce(NEW.provider_thread_id, ''),
         trosa.compat_time(NEW.message_time), coalesce(NEW.sender_email, ''),
         trosa.compat_jsonb(NEW.recipient_emails, '[]'::jsonb), coalesce(NEW.subject, ''),
         v_account, v_contact, v_timeline, v_inbox, coalesce(NEW.match_status, 'unmatched'),
         trosa.compat_jsonb(NEW.raw_payload, '{}'::jsonb), coalesce(NEW.last_error, ''),
         coalesce(trosa.compat_time(NEW.created_at), now()), coalesce(trosa.compat_time(NEW.updated_at), now()))
    ON CONFLICT (organization_id, provider_message_id) DO UPDATE SET
        provider_thread_id=excluded.provider_thread_id, message_time=excluded.message_time,
        sender_email=excluded.sender_email, recipient_emails=excluded.recipient_emails,
        subject=excluded.subject, account_id=excluded.account_id, contact_method_id=excluded.contact_method_id,
        timeline_event_id=excluded.timeline_event_id, inbox_item_id=excluded.inbox_item_id,
        match_status=excluded.match_status, raw_payload=excluded.raw_payload,
        last_error=excluded.last_error, updated_at=excluded.updated_at;
    INSERT INTO trade_os_compat.gmail_message_state_rows
        (legacy_user_id, id, provider_message_id, provider_thread_id, message_time,
         sender_email, recipient_emails, subject, customer_id, contact_id, match_status,
         activity_id, inbox_item_id, raw_payload, last_error, created_at, updated_at)
    VALUES
        (v_user, v_id, v_provider, coalesce(NEW.provider_thread_id, ''), coalesce(NEW.message_time, ''),
         coalesce(NEW.sender_email, ''), coalesce(NEW.recipient_emails, '[]'), coalesce(NEW.subject, ''),
         NEW.customer_id, NEW.contact_id, coalesce(NEW.match_status, 'unmatched'), NEW.activity_id,
         NEW.inbox_item_id, coalesce(NEW.raw_payload, '{}'), coalesce(NEW.last_error, ''),
         coalesce(NEW.created_at, ''), coalesce(NEW.updated_at, ''))
    ON CONFLICT (legacy_user_id, id) DO UPDATE SET
        provider_message_id=excluded.provider_message_id, provider_thread_id=excluded.provider_thread_id,
        message_time=excluded.message_time, sender_email=excluded.sender_email,
        recipient_emails=excluded.recipient_emails, subject=excluded.subject,
        customer_id=excluded.customer_id, contact_id=excluded.contact_id,
        match_status=excluded.match_status, activity_id=excluded.activity_id,
        inbox_item_id=excluded.inbox_item_id, raw_payload=excluded.raw_payload,
        last_error=excluded.last_error, updated_at=excluded.updated_at;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS gmail_message_states_write ON trade_os_compat.gmail_message_states;
CREATE TRIGGER gmail_message_states_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.gmail_message_states
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.gmail_message_states_write();

CREATE OR REPLACE FUNCTION trade_os_compat.communication_sources_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_user text := trosa.compat_current_user();
    v_activity bigint := coalesce(NEW.activity_id, 0);
    v_timeline uuid;
    v_source uuid;
    v_id bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM trosa.communication_source_items
         WHERE communication_source_id=(SELECT cs.id FROM trosa.communication_sources cs
                                         JOIN trosa.legacy_row_refs lr ON lr.target_id=cs.timeline_event_id
                                        WHERE lr.organization_id=v_org AND lr.legacy_user_id=v_user
                                          AND lr.table_name='follow_up_logs' AND lr.legacy_id=OLD.activity_id);
        DELETE FROM trosa.communication_sources
         WHERE timeline_event_id=(SELECT lr.target_id FROM trosa.legacy_row_refs lr
                                  WHERE lr.organization_id=v_org AND lr.legacy_user_id=v_user
                                    AND lr.table_name='follow_up_logs' AND lr.legacy_id=OLD.activity_id);
        DELETE FROM trade_os_compat.communication_source_rows WHERE legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    SELECT lr.target_id INTO v_timeline
      FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=v_org AND lr.legacy_user_id=v_user
       AND lr.table_name='follow_up_logs' AND lr.legacy_id=v_activity
     LIMIT 1;
    IF v_timeline IS NULL THEN
        RAISE EXCEPTION 'follow_up_log % is not visible for user %', v_activity, v_user;
    END IF;
    SELECT cs.id INTO v_source FROM trosa.communication_sources cs WHERE cs.timeline_event_id=v_timeline;
    v_source := coalesce(v_source, trosa.compat_uuid('communication-source:' || v_user || ':' || v_activity::text));
    INSERT INTO trosa.communication_sources
        (id, timeline_event_id, channel, source_url, account, conversation_identity,
         adapter_version, extraction_scope, warnings, raw_payload, cleaned_payload, captured_at)
    VALUES
        (v_source, v_timeline, coalesce(NEW.channel, ''), coalesce(NEW.source_url, ''),
         coalesce(NEW.account, ''), coalesce(NEW.conversation_identity, ''), coalesce(NEW.adapter_version, ''),
         coalesce(NEW.extraction_scope, ''), trosa.compat_jsonb(NEW.warnings, '[]'::jsonb),
         trosa.compat_jsonb(NEW.raw_payload, '{}'::jsonb), coalesce(NEW.cleaned_payload, ''),
         trosa.compat_time(NEW.captured_at))
    ON CONFLICT (timeline_event_id) DO UPDATE SET
        channel=excluded.channel, source_url=excluded.source_url, account=excluded.account,
        conversation_identity=excluded.conversation_identity, adapter_version=excluded.adapter_version,
        extraction_scope=excluded.extraction_scope, warnings=excluded.warnings,
        raw_payload=excluded.raw_payload, cleaned_payload=excluded.cleaned_payload,
        captured_at=excluded.captured_at;
    SELECT cs.id INTO v_source FROM trosa.communication_sources cs WHERE cs.timeline_event_id=v_timeline;
    SELECT id INTO v_id FROM trade_os_compat.communication_source_rows
     WHERE legacy_user_id=v_user AND activity_id=v_activity LIMIT 1;
    v_id := coalesce(v_id, CASE WHEN coalesce(NEW.id, 0) <> 0 THEN NEW.id
                               ELSE (SELECT coalesce(max(id), 0)+1 FROM trade_os_compat.communication_source_rows WHERE legacy_user_id=v_user) END);
    INSERT INTO trade_os_compat.communication_source_rows
        (legacy_user_id, id, activity_id, channel, source_url, account, conversation_identity,
         adapter_version, extraction_scope, warnings, raw_payload, cleaned_payload, captured_at)
    VALUES
        (v_user, v_id, v_activity, coalesce(NEW.channel, ''), coalesce(NEW.source_url, ''), coalesce(NEW.account, ''),
         coalesce(NEW.conversation_identity, ''), coalesce(NEW.adapter_version, ''), coalesce(NEW.extraction_scope, ''),
         coalesce(NEW.warnings, '[]'), coalesce(NEW.raw_payload, '{}'), coalesce(NEW.cleaned_payload, ''), coalesce(NEW.captured_at, ''))
    ON CONFLICT (legacy_user_id, id) DO UPDATE SET
        activity_id=excluded.activity_id, channel=excluded.channel, source_url=excluded.source_url,
        account=excluded.account, conversation_identity=excluded.conversation_identity,
        adapter_version=excluded.adapter_version, extraction_scope=excluded.extraction_scope,
        warnings=excluded.warnings, raw_payload=excluded.raw_payload,
        cleaned_payload=excluded.cleaned_payload, captured_at=excluded.captured_at;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS communication_sources_write ON trade_os_compat.communication_sources;
CREATE TRIGGER communication_sources_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.communication_sources
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.communication_sources_write();

CREATE OR REPLACE FUNCTION trade_os_compat.communication_source_items_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_user text := trosa.compat_current_user();
    v_activity bigint := coalesce(NEW.activity_id, 0);
    v_timeline uuid;
    v_source uuid;
    v_item uuid;
    v_id bigint;
    v_fingerprint text := coalesce(NEW.source_fingerprint, '');
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM trosa.communication_source_items
         WHERE source_fingerprint=OLD.source_fingerprint;
        DELETE FROM trade_os_compat.communication_source_item_rows
         WHERE legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF v_fingerprint = '' THEN RAISE EXCEPTION 'source_fingerprint is required'; END IF;
    SELECT lr.target_id INTO v_timeline FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=v_org AND lr.legacy_user_id=v_user
       AND lr.table_name='follow_up_logs' AND lr.legacy_id=v_activity LIMIT 1;
    SELECT cs.id INTO v_source FROM trosa.communication_sources cs WHERE cs.timeline_event_id=v_timeline;
    IF v_source IS NULL THEN RAISE EXCEPTION 'communication source for activity % is missing', v_activity; END IF;
    SELECT id INTO v_item FROM trosa.communication_source_items WHERE source_fingerprint=v_fingerprint LIMIT 1;
    v_item := coalesce(v_item, trosa.compat_uuid('communication-item:' || v_fingerprint));
    INSERT INTO trosa.communication_source_items
        (id, communication_source_id, source_fingerprint, message_time, direction, raw_text)
    VALUES
        (v_item, v_source, v_fingerprint, trosa.compat_time(NEW.message_time), coalesce(NEW.direction, 'unknown'), coalesce(NEW.raw_text, ''))
    ON CONFLICT (source_fingerprint) DO UPDATE SET
        communication_source_id=excluded.communication_source_id, message_time=excluded.message_time,
        direction=excluded.direction, raw_text=excluded.raw_text;
    SELECT id INTO v_id FROM trade_os_compat.communication_source_item_rows
     WHERE legacy_user_id=v_user AND source_fingerprint=v_fingerprint LIMIT 1;
    v_id := coalesce(v_id, CASE WHEN coalesce(NEW.id, 0) <> 0 THEN NEW.id
                               ELSE (SELECT coalesce(max(id), 0)+1 FROM trade_os_compat.communication_source_item_rows WHERE legacy_user_id=v_user) END);
    INSERT INTO trade_os_compat.communication_source_item_rows
        (legacy_user_id, id, source_fingerprint, activity_id, message_time, direction, raw_text)
    VALUES (v_user, v_id, v_fingerprint, v_activity, coalesce(NEW.message_time, ''), coalesce(NEW.direction, 'unknown'), coalesce(NEW.raw_text, ''))
    ON CONFLICT (legacy_user_id, id) DO UPDATE SET
        source_fingerprint=excluded.source_fingerprint, activity_id=excluded.activity_id,
        message_time=excluded.message_time, direction=excluded.direction, raw_text=excluded.raw_text;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

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
    IF TG_OP = 'DELETE' THEN
        DELETE FROM trosa.email_delivery_events
         WHERE id=trosa.compat_uuid('email-delivery:' || v_user || ':' || OLD.id::text);
        DELETE FROM trade_os_compat.email_delivery_event_rows WHERE legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    v_id := CASE WHEN coalesce(NEW.id, 0) <> 0 THEN NEW.id
                 ELSE (SELECT coalesce(max(id), 0)+1 FROM trade_os_compat.email_delivery_event_rows WHERE legacy_user_id=v_user) END;
    SELECT cr.contact_method_id INTO v_contact FROM trosa.contact_legacy_refs cr
     WHERE cr.organization_id=v_org AND cr.legacy_user_id=v_user AND cr.legacy_contact_id=NEW.contact_id LIMIT 1;
    IF v_contact IS NULL AND nullif(lower(trim(coalesce(NEW.email, ''))), '') IS NOT NULL THEN
        SELECT cm.id INTO v_contact FROM core.contact_methods cm
         WHERE cm.organization_id=v_org AND cm.kind='email'
           AND cm.normalized_value=lower(trim(NEW.email)) LIMIT 1;
    END IF;
    SELECT lr.target_id INTO v_outreach FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=v_org AND lr.legacy_user_id=v_user
       AND lr.table_name='outreach_emails' AND lr.legacy_id=NEW.outreach_email_id LIMIT 1;
    INSERT INTO trosa.email_delivery_events
        (id, organization_id, contact_method_id, outreach_message_id, event_type, smtp_code,
         enhanced_status, diagnostic_text, remote_mta, provider_message_id, source, occurred_at, legacy_payload)
    VALUES
        (trosa.compat_uuid('email-delivery:' || v_user || ':' || v_id::text), v_org, v_contact, v_outreach,
         coalesce(NEW.event_type, ''), coalesce(NEW.smtp_code, ''), coalesce(NEW.enhanced_status, ''),
         coalesce(NEW.diagnostic_text, ''), coalesce(NEW.remote_mta, ''), coalesce(NEW.message_id, ''),
         coalesce(NEW.source, 'manual'), coalesce(trosa.compat_time(NEW.occurred_at), now()), to_jsonb(NEW))
    ON CONFLICT (id) DO UPDATE SET
        contact_method_id=excluded.contact_method_id, outreach_message_id=excluded.outreach_message_id,
        event_type=excluded.event_type, smtp_code=excluded.smtp_code, enhanced_status=excluded.enhanced_status,
        diagnostic_text=excluded.diagnostic_text, remote_mta=excluded.remote_mta,
        provider_message_id=excluded.provider_message_id, source=excluded.source,
        occurred_at=excluded.occurred_at, legacy_payload=excluded.legacy_payload;
    INSERT INTO trade_os_compat.email_delivery_event_rows
        (legacy_user_id, id, email, contact_id, outreach_email_id, event_type, smtp_code,
         enhanced_status, diagnostic_text, remote_mta, message_id, source, occurred_at)
    VALUES
        (v_user, v_id, coalesce(NEW.email, ''), NEW.contact_id, NEW.outreach_email_id, coalesce(NEW.event_type, ''),
         coalesce(NEW.smtp_code, ''), coalesce(NEW.enhanced_status, ''), coalesce(NEW.diagnostic_text, ''),
         coalesce(NEW.remote_mta, ''), coalesce(NEW.message_id, ''), coalesce(NEW.source, 'manual'), coalesce(NEW.occurred_at, ''))
    ON CONFLICT (legacy_user_id, id) DO UPDATE SET
        email=excluded.email, contact_id=excluded.contact_id, outreach_email_id=excluded.outreach_email_id,
        event_type=excluded.event_type, smtp_code=excluded.smtp_code, enhanced_status=excluded.enhanced_status,
        diagnostic_text=excluded.diagnostic_text, remote_mta=excluded.remote_mta, message_id=excluded.message_id,
        source=excluded.source, occurred_at=excluded.occurred_at;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS email_delivery_events_write ON trade_os_compat.email_delivery_events;
CREATE TRIGGER email_delivery_events_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.email_delivery_events
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.email_delivery_events_write();

-- Weekly reports are not part of the generated weekly board today, but the
-- legacy API/table remains supported and is stored in the canonical trosa
-- module table when an older client writes one.
CREATE OR REPLACE FUNCTION trade_os_compat.weekly_reports_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_org uuid := trosa.compat_org_id();
    v_user text := trosa.compat_current_user();
    v_week text := coalesce(NEW.week_start, '');
    v_id bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM trosa.weekly_reports
         WHERE organization_id=v_org AND legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    INSERT INTO trosa.weekly_reports
        (organization_id, legacy_user_id, week_start, content, highlights, challenges, next_plan,
         status, created_at, updated_at)
    VALUES
        (v_org, coalesce(nullif(NEW.user_id, ''), v_user), v_week, coalesce(NEW.content, ''),
         coalesce(NEW.highlights, ''), coalesce(NEW.challenges, ''), coalesce(NEW.next_plan, ''),
         coalesce(NEW.status, 'draft'), coalesce(trosa.compat_time(NEW.created_at), now()),
         coalesce(trosa.compat_time(NEW.updated_at), now()))
    ON CONFLICT (organization_id, legacy_user_id, week_start) DO UPDATE SET
        content=excluded.content, highlights=excluded.highlights, challenges=excluded.challenges,
        next_plan=excluded.next_plan, status=excluded.status, updated_at=excluded.updated_at;
    SELECT wr.id INTO v_id FROM trosa.weekly_reports wr
     WHERE wr.organization_id=v_org AND wr.legacy_user_id=coalesce(nullif(NEW.user_id, ''), v_user)
       AND wr.week_start=v_week LIMIT 1;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS weekly_reports_write ON trade_os_compat.weekly_reports;
CREATE TRIGGER weekly_reports_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.weekly_reports
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.weekly_reports_write();

-- The Excel history recovery ledger is a compatibility projection, while its
-- canonical audit rows make the import decision and source lineage durable.
CREATE OR REPLACE FUNCTION trade_os_compat.import_batches_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_user text := trosa.compat_current_user();
    v_id bigint;
    v_audit_id uuid;
    v_path text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM trade_os_compat.import_batch_rows WHERE legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    v_id := CASE WHEN coalesce(NEW.id, 0) <> 0 THEN NEW.id
                 ELSE (SELECT coalesce(max(id), 0)+1 FROM trade_os_compat.import_batch_rows WHERE legacy_user_id=v_user) END;
    INSERT INTO trade_os_compat.import_batch_rows
        (legacy_user_id, id, source_name, source_sha256, imported_at, imported_count,
         skipped_count, created_customers, details)
    VALUES
        (v_user, v_id, coalesce(NEW.source_name, ''), coalesce(NEW.source_sha256, ''), coalesce(NEW.imported_at, ''),
         coalesce(NEW.imported_count, 0), coalesce(NEW.skipped_count, 0), coalesce(NEW.created_customers, 0), coalesce(NEW.details, ''))
    ON CONFLICT (legacy_user_id, id) DO UPDATE SET
        source_name=excluded.source_name, source_sha256=excluded.source_sha256, imported_at=excluded.imported_at,
        imported_count=excluded.imported_count, skipped_count=excluded.skipped_count,
        created_customers=excluded.created_customers, details=excluded.details;
    v_audit_id := trosa.compat_uuid('import-batch:' || v_user || ':' || v_id::text);
    v_path := 'compat/' || v_user || '/' || coalesce(NEW.source_name, '') || '/' || v_id::text;
    INSERT INTO audit.import_batches
        (id, organization_id, source_name, source_path, source_sha256, source_rows, imported_at)
    VALUES
        (v_audit_id, trosa.compat_org_id(), coalesce(NEW.source_name, ''), v_path, coalesce(NEW.source_sha256, ''),
         greatest(0, coalesce(NEW.imported_count, 0) + coalesce(NEW.skipped_count, 0)),
         coalesce(trosa.compat_time(NEW.imported_at), now()))
    ON CONFLICT (id) DO UPDATE SET
        source_name=excluded.source_name, source_path=excluded.source_path, source_sha256=excluded.source_sha256,
        source_rows=excluded.source_rows, imported_at=excluded.imported_at;
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
    v_hash text;
    v_account uuid;
    v_activity uuid;
    v_batch uuid;
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM trade_os_compat.imported_activity_row_rows WHERE legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    v_id := CASE WHEN coalesce(NEW.id, 0) <> 0 THEN NEW.id
                 ELSE (SELECT coalesce(max(id), 0)+1 FROM trade_os_compat.imported_activity_row_rows WHERE legacy_user_id=v_user) END;
    v_hash := coalesce(nullif(NEW.activity_hash, ''), trosa.compat_uuid('import-activity:' || v_user || ':' || v_id::text)::text);
    v_account := trosa.compat_customer_account(NEW.customer_id, v_user);
    SELECT lr.target_id INTO v_activity FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_user
       AND lr.table_name='follow_up_logs' AND lr.legacy_id=NEW.activity_id LIMIT 1;
    SELECT trosa.compat_uuid('import-batch:' || v_user || ':' || NEW.batch_id::text) INTO v_batch
     WHERE coalesce(NEW.batch_id, 0) <> 0;
    INSERT INTO trade_os_compat.imported_activity_row_rows
        (legacy_user_id, id, activity_hash, source_key, batch_id, customer_id, source_name,
         source_sheet, source_cell, source_header, activity_id, imported_at)
    VALUES
        (v_user, v_id, v_hash, coalesce(NEW.source_key, ''), NEW.batch_id, coalesce(NEW.customer_id, 0),
         coalesce(NEW.source_name, ''), coalesce(NEW.source_sheet, ''), coalesce(NEW.source_cell, ''),
         coalesce(NEW.source_header, ''), NEW.activity_id, coalesce(NEW.imported_at, ''))
    ON CONFLICT (legacy_user_id, id) DO UPDATE SET
        activity_hash=excluded.activity_hash, source_key=excluded.source_key, batch_id=excluded.batch_id,
        customer_id=excluded.customer_id, source_name=excluded.source_name, source_sheet=excluded.source_sheet,
        source_cell=excluded.source_cell, source_header=excluded.source_header, activity_id=excluded.activity_id,
        imported_at=excluded.imported_at;
    INSERT INTO audit.imported_activity_rows
        (id, organization_id, legacy_user_id, activity_hash, source_key, batch_id, account_id,
         source_name, source_sheet, source_cell, source_header, activity_id)
    VALUES
        (trosa.compat_uuid('imported-activity:' || v_user || ':' || v_id::text), trosa.compat_org_id(), v_user,
         v_hash, coalesce(NEW.source_key, ''), v_batch, v_account, coalesce(NEW.source_name, ''),
         coalesce(NEW.source_sheet, ''), coalesce(NEW.source_cell, ''), coalesce(NEW.source_header, ''), v_activity)
    ON CONFLICT (organization_id, activity_hash) DO UPDATE SET
        source_key=excluded.source_key, batch_id=excluded.batch_id, account_id=excluded.account_id,
        source_name=excluded.source_name, source_sheet=excluded.source_sheet, source_cell=excluded.source_cell,
        source_header=excluded.source_header, activity_id=excluded.activity_id;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS imported_activity_rows_write ON trade_os_compat.imported_activity_rows;
CREATE TRIGGER imported_activity_rows_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.imported_activity_rows
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.imported_activity_rows_write();

CREATE OR REPLACE FUNCTION trade_os_compat.import_unmatched_customers_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_user text := trosa.compat_current_user();
    v_id bigint;
    v_hash text;
    v_batch uuid;
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM trade_os_compat.import_unmatched_customer_rows WHERE legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    v_id := CASE WHEN coalesce(NEW.id, 0) <> 0 THEN NEW.id
                 ELSE (SELECT coalesce(max(id), 0)+1 FROM trade_os_compat.import_unmatched_customer_rows WHERE legacy_user_id=v_user) END;
    v_hash := coalesce(nullif(NEW.unmatched_hash, ''), trosa.compat_uuid('unmatched:' || v_user || ':' || v_id::text)::text);
    SELECT trosa.compat_uuid('import-batch:' || v_user || ':' || NEW.batch_id::text) INTO v_batch
     WHERE coalesce(NEW.batch_id, 0) <> 0;
    INSERT INTO trade_os_compat.import_unmatched_customer_rows
        (legacy_user_id, id, unmatched_hash, batch_id, customer_name, country, website,
         source_sheet, source_row, reason, created_at)
    VALUES
        (v_user, v_id, v_hash, NEW.batch_id, coalesce(NEW.customer_name, ''), coalesce(NEW.country, ''),
         coalesce(NEW.website, ''), coalesce(NEW.source_sheet, ''), NEW.source_row, coalesce(NEW.reason, ''), coalesce(NEW.created_at, ''))
    ON CONFLICT (legacy_user_id, id) DO UPDATE SET
        unmatched_hash=excluded.unmatched_hash, batch_id=excluded.batch_id, customer_name=excluded.customer_name,
        country=excluded.country, website=excluded.website, source_sheet=excluded.source_sheet,
        source_row=excluded.source_row, reason=excluded.reason, created_at=excluded.created_at;
    INSERT INTO audit.import_unmatched_customers
        (id, organization_id, legacy_user_id, unmatched_hash, batch_id, customer_name, country, website, source_sheet, source_row, reason)
    VALUES
        (trosa.compat_uuid('unmatched:' || v_user || ':' || v_id::text), trosa.compat_org_id(), v_user, v_hash, v_batch,
         coalesce(NEW.customer_name, ''), coalesce(NEW.country, ''), coalesce(NEW.website, ''), coalesce(NEW.source_sheet, ''),
         NEW.source_row, coalesce(NEW.reason, ''))
    ON CONFLICT (organization_id, unmatched_hash) DO UPDATE SET
        customer_name=excluded.customer_name, country=excluded.country, website=excluded.website,
        source_sheet=excluded.source_sheet, source_row=excluded.source_row, reason=excluded.reason;
    PERFORM trosa.compat_set_lastrowid(v_id);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS import_unmatched_customers_write ON trade_os_compat.import_unmatched_customers;
CREATE TRIGGER import_unmatched_customers_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.import_unmatched_customers
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.import_unmatched_customers_write();

COMMIT;
