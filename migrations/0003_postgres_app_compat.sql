-- PostgreSQL runtime compatibility surface for the existing Trosa HTTP API.
--
-- The application currently uses SQLite-shaped integer ids and table names.
-- These views are a projection of the unified core/module tables, scoped by
-- the current identity.  They are not a second store and never contain a
-- second company/person record.

BEGIN;

CREATE SCHEMA IF NOT EXISTS trade_os_compat;

ALTER TABLE identity.users ADD COLUMN IF NOT EXISTS username text;
ALTER TABLE identity.users ADD COLUMN IF NOT EXISTS password_hash text NOT NULL DEFAULT '';
ALTER TABLE identity.users ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT 'member';
ALTER TABLE identity.users ADD COLUMN IF NOT EXISTS created_by text NOT NULL DEFAULT '';
ALTER TABLE identity.users ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;
ALTER TABLE identity.users ADD COLUMN IF NOT EXISTS legacy_payload jsonb NOT NULL DEFAULT '{}'::jsonb;
UPDATE identity.users
   SET username=COALESCE(NULLIF(username,''), NULLIF(legacy_user_id,''), lower(regexp_replace(display_name,'[^a-zA-Z0-9_-]+','','g')))
 WHERE COALESCE(username,'')='';
CREATE UNIQUE INDEX IF NOT EXISTS identity_users_username_idx
    ON identity.users (organization_id, username);

ALTER TABLE trosa.research_reports ADD COLUMN IF NOT EXISTS legacy_payload jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE trosa.research_reports ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT '';
ALTER TABLE trosa.research_reports ADD COLUMN IF NOT EXISTS web_content text NOT NULL DEFAULT '';
ALTER TABLE trosa.research_reports ADD COLUMN IF NOT EXISTS web_fetched_at text NOT NULL DEFAULT '';
ALTER TABLE trosa.research_reports ADD COLUMN IF NOT EXISTS expires_at text NOT NULL DEFAULT '';
ALTER TABLE trosa.external_analysis_notes ADD COLUMN IF NOT EXISTS legacy_payload jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS trade_os_compat.app_settings (
    key text PRIMARY KEY,
    value text NOT NULL DEFAULT '',
    updated_at text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS trade_os_compat.customer_file_rows (
    legacy_user_id text NOT NULL,
    id bigint NOT NULL,
    customer_id bigint NOT NULL,
    account_id uuid REFERENCES trosa.accounts(id),
    file_object_id uuid REFERENCES core.file_objects(id),
    original_name text NOT NULL DEFAULT '',
    stored_name text NOT NULL DEFAULT '',
    file_path text NOT NULL DEFAULT '',
    file_size bigint NOT NULL DEFAULT 0,
    mime_type text NOT NULL DEFAULT '',
    category text NOT NULL DEFAULT '',
    sha256 text NOT NULL DEFAULT '',
    uploaded_by text NOT NULL DEFAULT '',
    is_deleted integer NOT NULL DEFAULT 0,
    deleted_at text NOT NULL DEFAULT '',
    created_at text NOT NULL DEFAULT '',
    PRIMARY KEY (legacy_user_id, id)
);

CREATE TABLE IF NOT EXISTS trade_os_compat.integration_sync_receipts (
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
    PRIMARY KEY (legacy_user_id, id),
    UNIQUE (legacy_user_id, integration, idempotency_key)
);

CREATE TABLE IF NOT EXISTS trade_os_compat.email_domain_probes (
    legacy_user_id text NOT NULL,
    id bigint NOT NULL,
    domain text NOT NULL,
    catchall_status text NOT NULL DEFAULT 'unknown',
    evidence text NOT NULL DEFAULT '[]',
    checked_at text NOT NULL DEFAULT '',
    next_check_at text NOT NULL DEFAULT '',
    PRIMARY KEY (legacy_user_id, id),
    UNIQUE (legacy_user_id, domain)
);

CREATE TABLE IF NOT EXISTS trade_os_compat.email_verifications (
    legacy_user_id text NOT NULL,
    id bigint NOT NULL,
    email text NOT NULL,
    normalized_email text NOT NULL DEFAULT '',
    domain text NOT NULL DEFAULT '',
    deliverability_status text NOT NULL DEFAULT 'unknown',
    confidence text NOT NULL DEFAULT 'low',
    address_type text NOT NULL DEFAULT 'person',
    risk_flags text NOT NULL DEFAULT '[]',
    evidence text NOT NULL DEFAULT '[]',
    mx_records text NOT NULL DEFAULT '[]',
    checked_at text NOT NULL DEFAULT '',
    expires_at text NOT NULL DEFAULT '',
    PRIMARY KEY (legacy_user_id, id),
    UNIQUE (legacy_user_id, email)
);

CREATE TABLE IF NOT EXISTS trade_os_compat.email_verification_jobs (
    legacy_user_id text NOT NULL,
    id bigint NOT NULL,
    email text NOT NULL,
    domain text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'queued',
    attempts integer NOT NULL DEFAULT 0,
    next_run_at text NOT NULL DEFAULT '',
    last_error text NOT NULL DEFAULT '',
    created_at text NOT NULL DEFAULT '',
    updated_at text NOT NULL DEFAULT '',
    PRIMARY KEY (legacy_user_id, id),
    UNIQUE (legacy_user_id, email)
);

CREATE TABLE IF NOT EXISTS trade_os_compat.email_logs (
    legacy_user_id text NOT NULL,
    id bigint NOT NULL,
    status text NOT NULL,
    message text NOT NULL DEFAULT '',
    reminder_count integer NOT NULL DEFAULT 0,
    created_at text NOT NULL DEFAULT '',
    PRIMARY KEY (legacy_user_id, id)
);

CREATE OR REPLACE VIEW trade_os_compat.users AS
SELECT u.legacy_user_id AS id,
       COALESCE(u.username,u.legacy_user_id) AS username,
       u.display_name AS name,
       u.label,
       u.color,
       u.password_hash,
       u.role,
       u.created_by,
       CASE WHEN u.active THEN 1 ELSE 0 END AS active,
       u.created_at::text AS created_at
  FROM identity.users u
 WHERE u.organization_id='859a998d-1b48-589b-8035-34dc65c01440'::uuid;

-- The first five projections already have write triggers in 0002.  Re-expose
-- them under the compatibility search path so the legacy SQL does not need a
-- module-specific branch.
CREATE OR REPLACE VIEW trade_os_compat.customers AS SELECT * FROM trosa.customers;
CREATE OR REPLACE VIEW trade_os_compat.contacts AS SELECT * FROM trosa.contacts;
CREATE OR REPLACE VIEW trade_os_compat.reminders AS SELECT * FROM trosa.reminders;
CREATE OR REPLACE VIEW trade_os_compat.follow_up_logs AS SELECT * FROM trosa.follow_up_logs;
CREATE OR REPLACE VIEW trade_os_compat.outreach_emails AS SELECT * FROM trosa.outreach_emails;

CREATE OR REPLACE VIEW trade_os_compat.research_reports AS
SELECT rrref.legacy_id AS id,
       aref.legacy_customer_id AS customer_id,
       rr.summary, rr.company_info, rr.key_findings, rr.needs_analysis,
       rr.cooperation_value, rr.raw_input,
       rr.created_at::text AS created_at, rr.updated_at::text AS updated_at,
       rr.source, rr.web_content, rr.web_fetched_at, rr.expires_at
  FROM trosa.research_reports rr
  JOIN trosa.legacy_row_refs rrref ON rrref.target_id=rr.id AND rrref.table_name='research_reports'
  JOIN trosa.account_legacy_refs aref ON aref.account_id=rr.account_id
       AND aref.organization_id=rrref.organization_id
       AND aref.legacy_user_id=rrref.legacy_user_id
 WHERE rrref.organization_id='859a998d-1b48-589b-8035-34dc65c01440'::uuid
   AND rrref.legacy_user_id=trosa.compat_current_user();

CREATE OR REPLACE VIEW trade_os_compat.external_analysis_notes AS
SELECT nref.legacy_id AS id,
       aref.legacy_customer_id AS customer_id,
       n.content, n.source, n.created_at::text AS created_at, n.updated_at::text AS updated_at
  FROM trosa.external_analysis_notes n
  JOIN trosa.legacy_row_refs nref ON nref.target_id=n.id AND nref.table_name='external_analysis_notes'
  JOIN trosa.account_legacy_refs aref ON aref.account_id=n.account_id
       AND aref.organization_id=nref.organization_id
       AND aref.legacy_user_id=nref.legacy_user_id
 WHERE nref.organization_id='859a998d-1b48-589b-8035-34dc65c01440'::uuid
   AND nref.legacy_user_id=trosa.compat_current_user();

CREATE OR REPLACE VIEW trade_os_compat.customer_understandings AS
SELECT uref.legacy_id AS id,
       aref.legacy_customer_id AS customer_id,
       u.current_summary, u.recent_change, u.open_loops::text AS open_loops,
       u.action_state, u.action_reason,
       COALESCE(tref.legacy_id, NULL::bigint) AS source_activity_id,
       u.version, u.created_at::text AS created_at, u.updated_at::text AS updated_at
  FROM trosa.account_understandings u
  JOIN trosa.legacy_row_refs uref ON uref.target_id=u.id AND uref.table_name='customer_understandings'
  JOIN trosa.account_legacy_refs aref ON aref.account_id=u.account_id
       AND aref.organization_id=uref.organization_id
       AND aref.legacy_user_id=uref.legacy_user_id
  LEFT JOIN trosa.legacy_row_refs tref ON tref.target_id=u.source_timeline_event_id
       AND tref.table_name='follow_up_logs'
       AND tref.organization_id=uref.organization_id
       AND tref.legacy_user_id=uref.legacy_user_id
 WHERE uref.organization_id='859a998d-1b48-589b-8035-34dc65c01440'::uuid
   AND uref.legacy_user_id=trosa.compat_current_user();

CREATE OR REPLACE VIEW trade_os_compat.ai_recommendations AS
SELECT aref.legacy_id AS id,
       ac.legacy_customer_id AS customer_id,
       a.understanding_version, a.content, a.reason,
       COALESCE(tref.legacy_id, NULL::bigint) AS source_activity_id,
       a.review_status, a.user_response, a.user_modified_content,
       a.executed_action, a.outcome,
       a.created_at::text AS created_at, a.updated_at::text AS updated_at
  FROM trosa.ai_recommendations a
  JOIN trosa.legacy_row_refs aref ON aref.target_id=a.id AND aref.table_name='ai_recommendations'
  JOIN trosa.account_legacy_refs ac ON ac.account_id=a.account_id
       AND ac.organization_id=aref.organization_id
       AND ac.legacy_user_id=aref.legacy_user_id
  LEFT JOIN trosa.legacy_row_refs tref ON tref.target_id=a.source_timeline_event_id
       AND tref.table_name='follow_up_logs'
       AND tref.organization_id=aref.organization_id
       AND tref.legacy_user_id=aref.legacy_user_id
 WHERE aref.organization_id='859a998d-1b48-589b-8035-34dc65c01440'::uuid
   AND aref.legacy_user_id=trosa.compat_current_user();

CREATE OR REPLACE VIEW trade_os_compat.inbox_items AS
SELECT iref.legacy_id AS id,
       CASE WHEN i.account_id IS NULL THEN NULL ELSE aref.legacy_customer_id END AS customer_id,
       i.item_type, i.title, i.content, i.dedupe_key, i.status,
       i.created_at::text AS created_at, COALESCE(i.resolved_at::text,'') AS resolved_at,
       COALESCE(i.snoozed_until::text,'') AS snoozed_until,
       i.resolution_reason, i.resolution_note
  FROM trosa.inbox_items i
  JOIN trosa.legacy_row_refs iref ON iref.target_id=i.id AND iref.table_name='inbox_items'
  LEFT JOIN trosa.account_legacy_refs aref ON aref.account_id=i.account_id
       AND aref.organization_id=iref.organization_id
       AND aref.legacy_user_id=iref.legacy_user_id
 WHERE iref.organization_id='859a998d-1b48-589b-8035-34dc65c01440'::uuid
   AND iref.legacy_user_id=trosa.compat_current_user();

CREATE OR REPLACE VIEW trade_os_compat.web_monitor_logs AS
SELECT wref.legacy_id AS id,
       aref.legacy_customer_id AS customer_id,
       w.url, w.status, w.content_hash, w.content_snippet, w.change_summary,
       w.checked_at::text AS checked_at,
       COALESCE(tref.legacy_id,NULL::bigint) AS reminder_id
  FROM trosa.web_monitor_observations w
  JOIN trosa.legacy_row_refs wref ON wref.target_id=w.id AND wref.table_name='web_monitor_logs'
  JOIN trosa.account_legacy_refs aref ON aref.account_id=w.account_id
       AND aref.organization_id=wref.organization_id
       AND aref.legacy_user_id=wref.legacy_user_id
  LEFT JOIN trosa.legacy_row_refs tref ON tref.target_id=w.task_id
       AND tref.table_name='reminders'
       AND tref.organization_id=wref.organization_id
       AND tref.legacy_user_id=wref.legacy_user_id
 WHERE wref.organization_id='859a998d-1b48-589b-8035-34dc65c01440'::uuid
   AND wref.legacy_user_id=trosa.compat_current_user();

DROP VIEW IF EXISTS trade_os_compat.customer_files CASCADE;
CREATE OR REPLACE VIEW trade_os_compat.customer_files AS
SELECT legacy_user_id, id, customer_id, original_name, stored_name, file_path,
       file_size, mime_type, category, sha256, uploaded_by, is_deleted,
       deleted_at, created_at
  FROM trade_os_compat.customer_file_rows
 WHERE legacy_user_id=trosa.compat_current_user();

-- The remaining views are added below in this migration in smaller blocks so
-- each API surface can be tested independently during the rehearsal.

CREATE OR REPLACE FUNCTION trosa.compat_org_id() RETURNS uuid
LANGUAGE sql IMMUTABLE AS $$
    SELECT '859a998d-1b48-589b-8035-34dc65c01440'::uuid
$$;

CREATE OR REPLACE FUNCTION trosa.compat_jsonb(value text, fallback jsonb DEFAULT '{}'::jsonb)
RETURNS jsonb LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF value IS NULL OR btrim(value)='' THEN RETURN fallback; END IF;
    RETURN value::jsonb;
EXCEPTION WHEN others THEN
    RETURN fallback;
END
$$;

CREATE OR REPLACE FUNCTION trosa.compat_next_id(table_name text, legacy_user text)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE result bigint;
BEGIN
    IF table_name IN ('research_reports','external_analysis_notes','customer_understandings',
                      'ai_recommendations','inbox_items','web_monitor_logs') THEN
        SELECT coalesce(max(lr.legacy_id),0)+1 INTO result
          FROM trosa.legacy_row_refs lr
         WHERE lr.organization_id=trosa.compat_org_id()
           AND lr.legacy_user_id=$2
           AND lr.table_name=$1;
    ELSE
        result := 1;
    END IF;
    RETURN result;
END
$$;

CREATE OR REPLACE FUNCTION trosa.compat_customer_account(customer_id bigint, legacy_user text)
RETURNS uuid LANGUAGE sql STABLE AS $$
    SELECT account_id FROM trosa.account_legacy_refs
     WHERE organization_id=trosa.compat_org_id()
       AND legacy_user_id=legacy_user AND legacy_customer_id=customer_id
     LIMIT 1
$$;

CREATE OR REPLACE FUNCTION trosa.compat_set_lastrowid(value bigint)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    PERFORM set_config('trade_os.lastrowid', coalesce(value,0)::text, true);
END
$$;

-- Bridge the existing Trosa projections under the compatibility search path.
DROP TRIGGER IF EXISTS compat_customers_bridge ON trade_os_compat.customers;
CREATE TRIGGER compat_customers_bridge
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.customers
FOR EACH ROW EXECUTE FUNCTION trosa.compat_customers_write();
DROP TRIGGER IF EXISTS compat_contacts_bridge ON trade_os_compat.contacts;
CREATE TRIGGER compat_contacts_bridge
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.contacts
FOR EACH ROW EXECUTE FUNCTION trosa.compat_contacts_write();
DROP TRIGGER IF EXISTS compat_reminders_bridge ON trade_os_compat.reminders;
CREATE TRIGGER compat_reminders_bridge
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.reminders
FOR EACH ROW EXECUTE FUNCTION trosa.compat_reminders_write();
DROP TRIGGER IF EXISTS compat_follow_up_bridge ON trade_os_compat.follow_up_logs;
CREATE TRIGGER compat_follow_up_bridge
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.follow_up_logs
FOR EACH ROW EXECUTE FUNCTION trosa.compat_follow_up_write();
DROP TRIGGER IF EXISTS compat_outreach_bridge ON trade_os_compat.outreach_emails;
CREATE TRIGGER compat_outreach_bridge
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.outreach_emails
FOR EACH ROW EXECUTE FUNCTION trosa.compat_outreach_write();

CREATE OR REPLACE FUNCTION trade_os_compat.research_reports_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE u text:=trosa.compat_current_user(); lid bigint; target uuid; account uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='research_reports' AND legacy_id=OLD.id;
        IF target IS NOT NULL THEN DELETE FROM trosa.research_reports WHERE id=target; DELETE FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='research_reports' AND legacy_id=OLD.id; END IF;
        RETURN OLD;
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
DROP TRIGGER IF EXISTS research_reports_write ON trade_os_compat.research_reports;
CREATE TRIGGER research_reports_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.research_reports FOR EACH ROW EXECUTE FUNCTION trade_os_compat.research_reports_write();

CREATE OR REPLACE FUNCTION trade_os_compat.external_analysis_notes_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE u text:=trosa.compat_current_user(); lid bigint; target uuid; account uuid;
BEGIN
    IF TG_OP='DELETE' THEN SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='external_analysis_notes' AND legacy_id=OLD.id; IF target IS NOT NULL THEN DELETE FROM trosa.external_analysis_notes WHERE id=target; DELETE FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='external_analysis_notes' AND legacy_id=OLD.id; END IF; RETURN OLD; END IF;
    lid:=CASE WHEN TG_OP='INSERT' AND coalesce(NEW.id,0)=0 THEN trosa.compat_next_id('external_analysis_notes',u) ELSE NEW.id END; account:=trosa.compat_customer_account(NEW.customer_id,u); IF account IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,u; END IF;
    SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='external_analysis_notes' AND legacy_id=lid; target:=coalesce(target,trosa.compat_uuid('external-note:'||u||':'||lid::text));
    INSERT INTO trosa.external_analysis_notes(id,account_id,content,source,legacy_payload,updated_at) VALUES(target,account,coalesce(NEW.content,''),coalesce(NEW.source,'external_model'),to_jsonb(NEW),now()) ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,content=excluded.content,source=excluded.source,legacy_payload=trosa.external_analysis_notes.legacy_payload||excluded.legacy_payload,updated_at=now();
    INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id) VALUES(trosa.compat_org_id(),u,'external_analysis_notes',lid,target) ON CONFLICT(organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id; PERFORM trosa.compat_set_lastrowid(lid); RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS external_analysis_notes_write ON trade_os_compat.external_analysis_notes;
CREATE TRIGGER external_analysis_notes_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.external_analysis_notes FOR EACH ROW EXECUTE FUNCTION trade_os_compat.external_analysis_notes_write();

CREATE OR REPLACE FUNCTION trade_os_compat.customer_understandings_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE u text:=trosa.compat_current_user(); lid bigint; target uuid; account uuid; source_event uuid;
BEGIN
    IF TG_OP='DELETE' THEN SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='customer_understandings' AND legacy_id=OLD.id; IF target IS NOT NULL THEN DELETE FROM trosa.account_understandings WHERE id=target; DELETE FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='customer_understandings' AND legacy_id=OLD.id; END IF; RETURN OLD; END IF;
    lid:=CASE WHEN TG_OP='INSERT' AND coalesce(NEW.id,0)=0 THEN trosa.compat_next_id('customer_understandings',u) ELSE NEW.id END; account:=trosa.compat_customer_account(NEW.customer_id,u); IF account IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,u; END IF;
    SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='customer_understandings' AND legacy_id=lid; SELECT target_id INTO source_event FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='follow_up_logs' AND legacy_id=NEW.source_activity_id; target:=coalesce(target,trosa.compat_uuid('understanding:'||account::text));
    INSERT INTO trosa.account_understandings(id,account_id,current_summary,recent_change,open_loops,action_state,action_reason,source_timeline_event_id,version,updated_at) VALUES(target,account,coalesce(NEW.current_summary,''),coalesce(NEW.recent_change,''),trosa.compat_jsonb(NEW.open_loops,'[]'::jsonb),coalesce(NEW.action_state,'hold'),coalesce(NEW.action_reason,''),source_event,coalesce(NEW.version,1),now()) ON CONFLICT(account_id) DO UPDATE SET current_summary=excluded.current_summary,recent_change=excluded.recent_change,open_loops=excluded.open_loops,action_state=excluded.action_state,action_reason=excluded.action_reason,source_timeline_event_id=excluded.source_timeline_event_id,version=excluded.version,updated_at=now();
    SELECT id INTO target FROM trosa.account_understandings WHERE account_id=account; INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id) VALUES(trosa.compat_org_id(),u,'customer_understandings',lid,target) ON CONFLICT(organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id; PERFORM trosa.compat_set_lastrowid(lid); RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS customer_understandings_write ON trade_os_compat.customer_understandings;
CREATE TRIGGER customer_understandings_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.customer_understandings FOR EACH ROW EXECUTE FUNCTION trade_os_compat.customer_understandings_write();

CREATE OR REPLACE FUNCTION trade_os_compat.ai_recommendations_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE u text:=trosa.compat_current_user(); lid bigint; target uuid; account uuid; source_event uuid;
BEGIN
    IF TG_OP='DELETE' THEN SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='ai_recommendations' AND legacy_id=OLD.id; IF target IS NOT NULL THEN DELETE FROM trosa.ai_recommendations WHERE id=target; DELETE FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='ai_recommendations' AND legacy_id=OLD.id; END IF; RETURN OLD; END IF;
    lid:=CASE WHEN TG_OP='INSERT' AND coalesce(NEW.id,0)=0 THEN trosa.compat_next_id('ai_recommendations',u) ELSE NEW.id END; account:=trosa.compat_customer_account(NEW.customer_id,u); IF account IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,u; END IF; SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='ai_recommendations' AND legacy_id=lid; SELECT target_id INTO source_event FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='follow_up_logs' AND legacy_id=NEW.source_activity_id; target:=coalesce(target,trosa.compat_uuid('recommendation:'||u||':'||lid::text));
    INSERT INTO trosa.ai_recommendations(id,account_id,understanding_version,content,reason,source_timeline_event_id,review_status,user_response,user_modified_content,executed_action,outcome,updated_at) VALUES(target,account,coalesce(NEW.understanding_version,0),coalesce(NEW.content,''),coalesce(NEW.reason,''),source_event,coalesce(NEW.review_status,'hold'),coalesce(NEW.user_response,''),coalesce(NEW.user_modified_content,''),coalesce(NEW.executed_action,''),coalesce(NEW.outcome,''),now()) ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,understanding_version=excluded.understanding_version,content=excluded.content,reason=excluded.reason,source_timeline_event_id=excluded.source_timeline_event_id,review_status=excluded.review_status,user_response=excluded.user_response,user_modified_content=excluded.user_modified_content,executed_action=excluded.executed_action,outcome=excluded.outcome,updated_at=now();
    INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id) VALUES(trosa.compat_org_id(),u,'ai_recommendations',lid,target) ON CONFLICT(organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id; PERFORM trosa.compat_set_lastrowid(lid); RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS ai_recommendations_write ON trade_os_compat.ai_recommendations;
CREATE TRIGGER ai_recommendations_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.ai_recommendations FOR EACH ROW EXECUTE FUNCTION trade_os_compat.ai_recommendations_write();

CREATE OR REPLACE FUNCTION trade_os_compat.inbox_items_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE u text:=trosa.compat_current_user(); lid bigint; target uuid; account uuid;
BEGIN
    IF TG_OP='DELETE' THEN SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='inbox_items' AND legacy_id=OLD.id; IF target IS NOT NULL THEN DELETE FROM trosa.inbox_items WHERE id=target; DELETE FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='inbox_items' AND legacy_id=OLD.id; END IF; RETURN OLD; END IF;
    lid:=CASE WHEN TG_OP='INSERT' AND coalesce(NEW.id,0)=0 THEN trosa.compat_next_id('inbox_items',u) ELSE NEW.id END; account:=trosa.compat_customer_account(NEW.customer_id,u); SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='inbox_items' AND legacy_id=lid; target:=coalesce(target,trosa.compat_uuid('inbox:'||u||':'||lid::text));
    INSERT INTO trosa.inbox_items(id,account_id,item_type,title,content,dedupe_key,status,resolved_at,snoozed_until,resolution_reason,resolution_note,legacy_payload) VALUES(target,account,coalesce(NEW.item_type,''),coalesce(NEW.title,''),coalesce(NEW.content,''),coalesce(NEW.dedupe_key,''),coalesce(NEW.status,'open'),NULLIF(NEW.resolved_at,'')::timestamptz,NULLIF(NEW.snoozed_until,'')::timestamptz,coalesce(NEW.resolution_reason,''),coalesce(NEW.resolution_note,''),to_jsonb(NEW)) ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,item_type=excluded.item_type,title=excluded.title,content=excluded.content,dedupe_key=excluded.dedupe_key,status=excluded.status,resolved_at=excluded.resolved_at,snoozed_until=excluded.snoozed_until,resolution_reason=excluded.resolution_reason,resolution_note=excluded.resolution_note,legacy_payload=trosa.inbox_items.legacy_payload||excluded.legacy_payload;
    INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id) VALUES(trosa.compat_org_id(),u,'inbox_items',lid,target) ON CONFLICT(organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id; PERFORM trosa.compat_set_lastrowid(lid); RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS inbox_items_write ON trade_os_compat.inbox_items;
CREATE TRIGGER inbox_items_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.inbox_items FOR EACH ROW EXECUTE FUNCTION trade_os_compat.inbox_items_write();

CREATE OR REPLACE FUNCTION trade_os_compat.web_monitor_logs_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE u text:=trosa.compat_current_user(); lid bigint; target uuid; account uuid; task uuid;
BEGIN
    IF TG_OP='DELETE' THEN SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='web_monitor_logs' AND legacy_id=OLD.id; IF target IS NOT NULL THEN DELETE FROM trosa.web_monitor_observations WHERE id=target; DELETE FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='web_monitor_logs' AND legacy_id=OLD.id; END IF; RETURN OLD; END IF;
    lid:=CASE WHEN TG_OP='INSERT' AND coalesce(NEW.id,0)=0 THEN trosa.compat_next_id('web_monitor_logs',u) ELSE NEW.id END; account:=trosa.compat_customer_account(NEW.customer_id,u); IF account IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,u; END IF; SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='web_monitor_logs' AND legacy_id=lid; SELECT target_id INTO task FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u AND table_name='reminders' AND legacy_id=NEW.reminder_id; target:=coalesce(target,trosa.compat_uuid('web-monitor:'||u||':'||lid::text));
    INSERT INTO trosa.web_monitor_observations(id,account_id,url,status,content_hash,content_snippet,change_summary,checked_at,task_id,legacy_payload) VALUES(target,account,coalesce(NEW.url,''),coalesce(NEW.status,'ok'),coalesce(NEW.content_hash,''),coalesce(NEW.content_snippet,''),coalesce(NEW.change_summary,''),coalesce(NULLIF(NEW.checked_at,'')::timestamptz,now()),task,to_jsonb(NEW)) ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,url=excluded.url,status=excluded.status,content_hash=excluded.content_hash,content_snippet=excluded.content_snippet,change_summary=excluded.change_summary,checked_at=excluded.checked_at,task_id=excluded.task_id,legacy_payload=trosa.web_monitor_observations.legacy_payload||excluded.legacy_payload;
    INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id) VALUES(trosa.compat_org_id(),u,'web_monitor_logs',lid,target) ON CONFLICT(organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id; PERFORM trosa.compat_set_lastrowid(lid); RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS web_monitor_logs_write ON trade_os_compat.web_monitor_logs;
CREATE TRIGGER web_monitor_logs_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.web_monitor_logs FOR EACH ROW EXECUTE FUNCTION trade_os_compat.web_monitor_logs_write();

-- Attachment metadata remains in the unified file layer while the existing
-- API keeps its relative storage path and integer attachment id.
-- The view was declared above with the legacy_user_id column retained for
-- DB-API compatibility.  The web code ignores that extra internal column.

CREATE OR REPLACE FUNCTION trade_os_compat.customer_files_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE u text:=trosa.compat_current_user(); lid bigint; account uuid; file_id uuid;
BEGIN
    IF TG_OP='DELETE' THEN UPDATE trade_os_compat.customer_file_rows SET is_deleted=1,deleted_at=now()::text WHERE legacy_user_id=u AND id=OLD.id; RETURN OLD; END IF;
    lid:=CASE WHEN TG_OP='INSERT' AND coalesce(NEW.id,0)=0 THEN (SELECT coalesce(max(id),0)+1 FROM trade_os_compat.customer_file_rows WHERE legacy_user_id=u) ELSE NEW.id END; account:=trosa.compat_customer_account(NEW.customer_id,u); IF account IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,u; END IF; file_id:=trosa.compat_uuid('file:'||u||':'||lid::text);
    INSERT INTO core.file_objects(id,organization_id,storage_key,original_name,mime_type,size_bytes,sha256,uploaded_by_user_id,deleted_at) VALUES(file_id,trosa.compat_org_id(),coalesce(NEW.file_path,''),coalesce(NEW.original_name,''),coalesce(NEW.mime_type,''),coalesce(NEW.file_size,0),coalesce(NEW.sha256,''),(SELECT id FROM identity.users WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u LIMIT 1),CASE WHEN coalesce(NEW.is_deleted,0)<>0 THEN now() ELSE NULL END) ON CONFLICT(id) DO UPDATE SET storage_key=excluded.storage_key,original_name=excluded.original_name,mime_type=excluded.mime_type,size_bytes=excluded.size_bytes,sha256=excluded.sha256,deleted_at=excluded.deleted_at;
    INSERT INTO core.entity_files(id,file_object_id,account_id,relation_type) VALUES(trosa.compat_uuid('entity-file:'||u||':'||lid::text),file_id,account,'attachment') ON CONFLICT DO NOTHING;
    INSERT INTO trade_os_compat.customer_file_rows(legacy_user_id,id,customer_id,account_id,file_object_id,original_name,stored_name,file_path,file_size,mime_type,category,sha256,uploaded_by,is_deleted,deleted_at,created_at) VALUES(u,lid,NEW.customer_id,account,file_id,coalesce(NEW.original_name,''),coalesce(NEW.stored_name,''),coalesce(NEW.file_path,''),coalesce(NEW.file_size,0),coalesce(NEW.mime_type,''),coalesce(NEW.category,''),coalesce(NEW.sha256,''),coalesce(NEW.uploaded_by,''),coalesce(NEW.is_deleted,0),coalesce(NEW.deleted_at,''),coalesce(NEW.created_at,'')) ON CONFLICT(legacy_user_id,id) DO UPDATE SET customer_id=excluded.customer_id,original_name=excluded.original_name,stored_name=excluded.stored_name,file_path=excluded.file_path,file_size=excluded.file_size,mime_type=excluded.mime_type,category=excluded.category,sha256=excluded.sha256,uploaded_by=excluded.uploaded_by,is_deleted=excluded.is_deleted,deleted_at=excluded.deleted_at;
    PERFORM trosa.compat_set_lastrowid(lid); RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS customer_files_write ON trade_os_compat.customer_files;
CREATE TRIGGER customer_files_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.customer_files FOR EACH ROW EXECUTE FUNCTION trade_os_compat.customer_files_write();

COMMIT;
