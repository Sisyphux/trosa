-- Remaining Trosa runtime surfaces.  Every object is in the same PostgreSQL
-- database; the *_rows tables are compatibility projections keyed by the
-- existing per-user integer ids, not duplicate core companies or people.

BEGIN;

CREATE TABLE IF NOT EXISTS trade_os_compat.operation_log_rows (
    legacy_user_id text NOT NULL, id bigint NOT NULL, action text NOT NULL,
    target_type text NOT NULL, target_id bigint, details text NOT NULL DEFAULT '',
    created_at text NOT NULL DEFAULT '', user_id text NOT NULL DEFAULT '',
    PRIMARY KEY (legacy_user_id,id)
);
CREATE TABLE IF NOT EXISTS trade_os_compat.agent_proposal_rows (
    legacy_user_id text NOT NULL, id bigint NOT NULL, proposal_type text NOT NULL,
    customer_id bigint NOT NULL, payload text NOT NULL DEFAULT '{}', proposal_action text NOT NULL DEFAULT '',
    source text NOT NULL DEFAULT '', source_reference text NOT NULL DEFAULT '', idempotency_key text NOT NULL DEFAULT '',
    request_sha256 text NOT NULL DEFAULT '', status text NOT NULL DEFAULT 'pending', created_at text NOT NULL DEFAULT '',
    confirmed_at text NOT NULL DEFAULT '', PRIMARY KEY (legacy_user_id,id)
);
CREATE TABLE IF NOT EXISTS trade_os_compat.agent_gateway_rows (
    legacy_user_id text NOT NULL, id bigint NOT NULL, action text NOT NULL, idempotency_key text NOT NULL,
    request_sha256 text NOT NULL, proposal_id bigint, response_json text NOT NULL DEFAULT '{}',
    created_at text NOT NULL DEFAULT '', updated_at text NOT NULL DEFAULT '', PRIMARY KEY (legacy_user_id,id),
    UNIQUE (legacy_user_id,action,idempotency_key)
);
CREATE TABLE IF NOT EXISTS trade_os_compat.agent_action_rows (
    legacy_user_id text NOT NULL, id bigint NOT NULL, action_id text NOT NULL, token_id text NOT NULL DEFAULT '',
    user_id text NOT NULL DEFAULT '', action_type text NOT NULL, customer_id bigint, related_type text NOT NULL DEFAULT '',
    related_id bigint, undo_token text NOT NULL, request_json text NOT NULL DEFAULT '{}', status text NOT NULL DEFAULT 'completed',
    created_at text NOT NULL DEFAULT '', undone_at text NOT NULL DEFAULT '', PRIMARY KEY (legacy_user_id,id),
    UNIQUE (legacy_user_id,action_id)
);
CREATE TABLE IF NOT EXISTS trade_os_compat.undo_action_rows (
    legacy_user_id text NOT NULL, id bigint NOT NULL, token text NOT NULL, operation text NOT NULL,
    target_type text NOT NULL, target_id bigint, description text NOT NULL DEFAULT '', entities text NOT NULL DEFAULT '[]',
    status text NOT NULL DEFAULT 'available', created_at text NOT NULL DEFAULT '', undone_at text NOT NULL DEFAULT '',
    PRIMARY KEY (legacy_user_id,id), UNIQUE (legacy_user_id,token)
);
CREATE TABLE IF NOT EXISTS trade_os_compat.import_batch_rows (
    legacy_user_id text NOT NULL, id bigint NOT NULL, source_name text NOT NULL, source_sha256 text NOT NULL DEFAULT '',
    imported_at text NOT NULL DEFAULT '', imported_count integer NOT NULL DEFAULT 0, skipped_count integer NOT NULL DEFAULT 0,
    created_customers integer NOT NULL DEFAULT 0, details text NOT NULL DEFAULT '', PRIMARY KEY (legacy_user_id,id)
);
CREATE TABLE IF NOT EXISTS trade_os_compat.imported_activity_row_rows (
    legacy_user_id text NOT NULL, id bigint NOT NULL, activity_hash text NOT NULL, source_key text NOT NULL DEFAULT '',
    batch_id bigint, customer_id bigint NOT NULL, source_name text NOT NULL, source_sheet text NOT NULL DEFAULT '',
    source_cell text NOT NULL DEFAULT '', source_header text NOT NULL DEFAULT '', activity_id bigint,
    imported_at text NOT NULL DEFAULT '', PRIMARY KEY (legacy_user_id,id), UNIQUE (legacy_user_id,activity_hash)
);
CREATE TABLE IF NOT EXISTS trade_os_compat.import_unmatched_customer_rows (
    legacy_user_id text NOT NULL, id bigint NOT NULL, unmatched_hash text NOT NULL DEFAULT '', batch_id bigint,
    customer_name text NOT NULL, country text NOT NULL DEFAULT '', website text NOT NULL DEFAULT '', source_sheet text NOT NULL DEFAULT '',
    source_row integer, reason text NOT NULL DEFAULT '', created_at text NOT NULL DEFAULT '', PRIMARY KEY (legacy_user_id,id),
    UNIQUE (legacy_user_id,unmatched_hash)
);
CREATE TABLE IF NOT EXISTS trade_os_compat.email_delivery_event_rows (
    legacy_user_id text NOT NULL, id bigint NOT NULL, email text NOT NULL, contact_id bigint,
    outreach_email_id bigint, event_type text NOT NULL, smtp_code text NOT NULL DEFAULT '', enhanced_status text NOT NULL DEFAULT '',
    diagnostic_text text NOT NULL DEFAULT '', remote_mta text NOT NULL DEFAULT '', message_id text NOT NULL DEFAULT '',
    source text NOT NULL DEFAULT 'manual', occurred_at text NOT NULL DEFAULT '', PRIMARY KEY (legacy_user_id,id)
);
CREATE TABLE IF NOT EXISTS trade_os_compat.gmail_message_state_rows (
    legacy_user_id text NOT NULL, id bigint NOT NULL, provider_message_id text NOT NULL, provider_thread_id text NOT NULL DEFAULT '',
    message_time text NOT NULL DEFAULT '', sender_email text NOT NULL DEFAULT '', recipient_emails text NOT NULL DEFAULT '[]',
    subject text NOT NULL DEFAULT '', customer_id bigint, contact_id bigint, match_status text NOT NULL DEFAULT 'unmatched',
    activity_id bigint, inbox_item_id bigint, raw_payload text NOT NULL DEFAULT '{}', last_error text NOT NULL DEFAULT '',
    created_at text NOT NULL DEFAULT '', updated_at text NOT NULL DEFAULT '', PRIMARY KEY (legacy_user_id,id),
    UNIQUE (legacy_user_id,provider_message_id)
);
CREATE TABLE IF NOT EXISTS trade_os_compat.communication_source_rows (
    legacy_user_id text NOT NULL, id bigint NOT NULL, activity_id bigint NOT NULL, channel text NOT NULL,
    source_url text NOT NULL DEFAULT '', account text NOT NULL DEFAULT '', conversation_identity text NOT NULL DEFAULT '',
    adapter_version text NOT NULL DEFAULT '', extraction_scope text NOT NULL DEFAULT '', warnings text NOT NULL DEFAULT '[]',
    raw_payload text NOT NULL DEFAULT '{}', cleaned_payload text NOT NULL DEFAULT '', captured_at text NOT NULL DEFAULT '',
    PRIMARY KEY (legacy_user_id,id), UNIQUE (legacy_user_id,activity_id)
);
CREATE TABLE IF NOT EXISTS trade_os_compat.communication_source_item_rows (
    legacy_user_id text NOT NULL, id bigint NOT NULL, source_fingerprint text NOT NULL, activity_id bigint NOT NULL,
    message_time text NOT NULL DEFAULT '', direction text NOT NULL DEFAULT 'unknown', raw_text text NOT NULL DEFAULT '',
    PRIMARY KEY (legacy_user_id,id), UNIQUE (legacy_user_id,source_fingerprint)
);

CREATE OR REPLACE VIEW trade_os_compat.operation_logs AS
SELECT id, action, target_type, target_id, details, created_at, user_id
  FROM trade_os_compat.operation_log_rows WHERE legacy_user_id=trosa.compat_current_user();
CREATE OR REPLACE VIEW trade_os_compat.agent_proposals AS
SELECT id, proposal_type, customer_id, payload, proposal_action, source, source_reference,
       idempotency_key, request_sha256, status, created_at, confirmed_at
  FROM trade_os_compat.agent_proposal_rows WHERE legacy_user_id=trosa.compat_current_user();
CREATE OR REPLACE VIEW trade_os_compat.agent_gateway_idempotency AS
SELECT id, action, idempotency_key, request_sha256, proposal_id, response_json, created_at, updated_at
  FROM trade_os_compat.agent_gateway_rows WHERE legacy_user_id=trosa.compat_current_user();
CREATE OR REPLACE VIEW trade_os_compat.agent_actions AS
SELECT id, action_id, token_id, user_id, action_type, customer_id, related_type, related_id,
       undo_token, request_json, status, created_at, undone_at
  FROM trade_os_compat.agent_action_rows WHERE legacy_user_id=trosa.compat_current_user();
CREATE OR REPLACE VIEW trade_os_compat.undo_actions AS
SELECT id, token, operation, target_type, target_id, description, entities, status, created_at, undone_at
  FROM trade_os_compat.undo_action_rows WHERE legacy_user_id=trosa.compat_current_user();
CREATE OR REPLACE VIEW trade_os_compat.import_batches AS
SELECT id, source_name, source_sha256, imported_at, imported_count, skipped_count, created_customers, details
  FROM trade_os_compat.import_batch_rows WHERE legacy_user_id=trosa.compat_current_user();
CREATE OR REPLACE VIEW trade_os_compat.imported_activity_rows AS
SELECT id, activity_hash, source_key, batch_id, customer_id, source_name, source_sheet, source_cell,
       source_header, activity_id, imported_at
  FROM trade_os_compat.imported_activity_row_rows WHERE legacy_user_id=trosa.compat_current_user();
CREATE OR REPLACE VIEW trade_os_compat.import_unmatched_customers AS
SELECT id, unmatched_hash, batch_id, customer_name, country, website, source_sheet, source_row, reason, created_at
  FROM trade_os_compat.import_unmatched_customer_rows WHERE legacy_user_id=trosa.compat_current_user();
CREATE OR REPLACE VIEW trade_os_compat.email_delivery_events AS
SELECT id, email, contact_id, outreach_email_id, event_type, smtp_code, enhanced_status, diagnostic_text,
       remote_mta, message_id, source, occurred_at
  FROM trade_os_compat.email_delivery_event_rows WHERE legacy_user_id=trosa.compat_current_user();
CREATE OR REPLACE VIEW trade_os_compat.gmail_message_states AS
SELECT id, provider_message_id, provider_thread_id, message_time, sender_email, recipient_emails, subject,
       customer_id, contact_id, match_status, activity_id, inbox_item_id, raw_payload, last_error, created_at, updated_at
  FROM trade_os_compat.gmail_message_state_rows WHERE legacy_user_id=trosa.compat_current_user();
CREATE OR REPLACE VIEW trade_os_compat.communication_sources AS
SELECT id, activity_id, channel, source_url, account, conversation_identity, adapter_version, extraction_scope,
       warnings, raw_payload, cleaned_payload, captured_at
  FROM trade_os_compat.communication_source_rows WHERE legacy_user_id=trosa.compat_current_user();
CREATE OR REPLACE VIEW trade_os_compat.communication_source_items AS
SELECT id, source_fingerprint, activity_id, message_time, direction, raw_text
  FROM trade_os_compat.communication_source_item_rows WHERE legacy_user_id=trosa.compat_current_user();

CREATE OR REPLACE VIEW trade_os_compat.weekly_reports AS
SELECT id, legacy_user_id AS user_id, week_start, content, highlights, challenges, next_plan,
       status, created_at::text AS created_at, updated_at::text AS updated_at
  FROM trosa.weekly_reports
 WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=trosa.compat_current_user();

-- On the first application these objects are the compatibility tables from
-- 0003; on subsequent applications they are views created below.  Drop both
-- object kinds so schema application remains repeatable.
DROP VIEW IF EXISTS trade_os_compat.email_verifications CASCADE;
DROP VIEW IF EXISTS trade_os_compat.email_verification_jobs CASCADE;
DROP VIEW IF EXISTS trade_os_compat.email_domain_probes CASCADE;
DROP VIEW IF EXISTS trade_os_compat.email_logs CASCADE;
DROP TABLE IF EXISTS trade_os_compat.email_verifications CASCADE;
DROP TABLE IF EXISTS trade_os_compat.email_verification_jobs CASCADE;
DROP TABLE IF EXISTS trade_os_compat.email_domain_probes CASCADE;
DROP TABLE IF EXISTS trade_os_compat.email_logs CASCADE;
CREATE OR REPLACE VIEW trade_os_compat.email_verifications AS
SELECT id, email, normalized_email, domain, deliverability_status, confidence, address_type,
       risk_flags::text AS risk_flags, evidence::text AS evidence, mx_records::text AS mx_records,
       checked_at, expires_at
  FROM trosa.email_verifications
 WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=trosa.compat_current_user();
CREATE OR REPLACE VIEW trade_os_compat.email_verification_jobs AS
SELECT id, email, domain, status, attempts, next_run_at, last_error, created_at, updated_at
  FROM trosa.email_verification_jobs
 WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=trosa.compat_current_user();
CREATE OR REPLACE VIEW trade_os_compat.email_domain_probes AS
SELECT id, domain, catchall_status, evidence::text AS evidence, checked_at, next_check_at
  FROM trosa.email_domain_probes
 WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=trosa.compat_current_user();
CREATE OR REPLACE VIEW trade_os_compat.email_logs AS
SELECT id, status, message, reminder_count, created_at
  FROM trosa.email_logs
 WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=trosa.compat_current_user();

-- The following small trigger helper makes the write-heavy audit and Inbox
-- support tables behave like their SQLite predecessors.  Reads remain
-- filtered by the PostgreSQL session identity.
CREATE OR REPLACE FUNCTION trade_os_compat.operation_logs_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE u text:=trosa.compat_current_user(); lid bigint:=coalesce(NEW.id,(SELECT coalesce(max(id),0)+1 FROM trade_os_compat.operation_log_rows WHERE legacy_user_id=u));
BEGIN
 IF TG_OP='DELETE' THEN DELETE FROM trade_os_compat.operation_log_rows WHERE legacy_user_id=u AND id=OLD.id; RETURN OLD; END IF;
 INSERT INTO trade_os_compat.operation_log_rows(legacy_user_id,id,action,target_type,target_id,details,created_at,user_id)
 VALUES(u,lid,coalesce(NEW.action,''),coalesce(NEW.target_type,''),NEW.target_id,coalesce(NEW.details,''),coalesce(NEW.created_at,''),coalesce(NEW.user_id,u))
 ON CONFLICT(legacy_user_id,id) DO UPDATE SET action=excluded.action,target_type=excluded.target_type,target_id=excluded.target_id,details=excluded.details,created_at=excluded.created_at,user_id=excluded.user_id;
 PERFORM trosa.compat_set_lastrowid(lid); RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS operation_logs_write ON trade_os_compat.operation_logs;
CREATE TRIGGER operation_logs_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.operation_logs FOR EACH ROW EXECUTE FUNCTION trade_os_compat.operation_logs_write();

CREATE OR REPLACE FUNCTION trade_os_compat.agent_proposals_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE u text:=trosa.compat_current_user(); lid bigint:=coalesce(NEW.id,(SELECT coalesce(max(id),0)+1 FROM trade_os_compat.agent_proposal_rows WHERE legacy_user_id=u));
BEGIN
 IF TG_OP='DELETE' THEN DELETE FROM trade_os_compat.agent_proposal_rows WHERE legacy_user_id=u AND id=OLD.id; RETURN OLD; END IF;
 INSERT INTO trade_os_compat.agent_proposal_rows(legacy_user_id,id,proposal_type,customer_id,payload,proposal_action,source,source_reference,idempotency_key,request_sha256,status,created_at,confirmed_at)
 VALUES(u,lid,coalesce(NEW.proposal_type,''),NEW.customer_id,coalesce(NEW.payload,'{}'),coalesce(NEW.proposal_action,''),coalesce(NEW.source,''),coalesce(NEW.source_reference,''),coalesce(NEW.idempotency_key,''),coalesce(NEW.request_sha256,''),coalesce(NEW.status,'pending'),coalesce(NEW.created_at,''),coalesce(NEW.confirmed_at,''))
 ON CONFLICT(legacy_user_id,id) DO UPDATE SET proposal_type=excluded.proposal_type,customer_id=excluded.customer_id,payload=excluded.payload,proposal_action=excluded.proposal_action,source=excluded.source,source_reference=excluded.source_reference,idempotency_key=excluded.idempotency_key,request_sha256=excluded.request_sha256,status=excluded.status,created_at=excluded.created_at,confirmed_at=excluded.confirmed_at;
 PERFORM trosa.compat_set_lastrowid(lid); RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS agent_proposals_write ON trade_os_compat.agent_proposals;
CREATE TRIGGER agent_proposals_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.agent_proposals FOR EACH ROW EXECUTE FUNCTION trade_os_compat.agent_proposals_write();

CREATE OR REPLACE FUNCTION trade_os_compat.agent_gateway_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE u text:=trosa.compat_current_user(); lid bigint:=coalesce(NEW.id,(SELECT coalesce(max(id),0)+1 FROM trade_os_compat.agent_gateway_rows WHERE legacy_user_id=u));
BEGIN
 IF TG_OP='DELETE' THEN DELETE FROM trade_os_compat.agent_gateway_rows WHERE legacy_user_id=u AND id=OLD.id; RETURN OLD; END IF;
 INSERT INTO trade_os_compat.agent_gateway_rows(legacy_user_id,id,action,idempotency_key,request_sha256,proposal_id,response_json,created_at,updated_at)
 VALUES(u,lid,coalesce(NEW.action,''),coalesce(NEW.idempotency_key,''),coalesce(NEW.request_sha256,''),NEW.proposal_id,coalesce(NEW.response_json,'{}'),coalesce(NEW.created_at,''),coalesce(NEW.updated_at,''))
 ON CONFLICT(legacy_user_id,id) DO UPDATE SET action=excluded.action,idempotency_key=excluded.idempotency_key,request_sha256=excluded.request_sha256,proposal_id=excluded.proposal_id,response_json=excluded.response_json,created_at=excluded.created_at,updated_at=excluded.updated_at;
 PERFORM trosa.compat_set_lastrowid(lid); RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS agent_gateway_write ON trade_os_compat.agent_gateway_idempotency;
CREATE TRIGGER agent_gateway_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.agent_gateway_idempotency FOR EACH ROW EXECUTE FUNCTION trade_os_compat.agent_gateway_write();

CREATE OR REPLACE FUNCTION trade_os_compat.agent_actions_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE u text:=trosa.compat_current_user(); lid bigint:=coalesce(NEW.id,(SELECT coalesce(max(id),0)+1 FROM trade_os_compat.agent_action_rows WHERE legacy_user_id=u));
BEGIN
 IF TG_OP='DELETE' THEN DELETE FROM trade_os_compat.agent_action_rows WHERE legacy_user_id=u AND id=OLD.id; RETURN OLD; END IF;
 INSERT INTO trade_os_compat.agent_action_rows(legacy_user_id,id,action_id,token_id,user_id,action_type,customer_id,related_type,related_id,undo_token,request_json,status,created_at,undone_at)
 VALUES(u,lid,coalesce(NEW.action_id,''),coalesce(NEW.token_id,''),coalesce(NEW.user_id,u),coalesce(NEW.action_type,''),NEW.customer_id,coalesce(NEW.related_type,''),NEW.related_id,coalesce(NEW.undo_token,''),coalesce(NEW.request_json,'{}'),coalesce(NEW.status,'completed'),coalesce(NEW.created_at,''),coalesce(NEW.undone_at,''))
 ON CONFLICT(legacy_user_id,id) DO UPDATE SET action_id=excluded.action_id,token_id=excluded.token_id,user_id=excluded.user_id,action_type=excluded.action_type,customer_id=excluded.customer_id,related_type=excluded.related_type,related_id=excluded.related_id,undo_token=excluded.undo_token,request_json=excluded.request_json,status=excluded.status,created_at=excluded.created_at,undone_at=excluded.undone_at;
 PERFORM trosa.compat_set_lastrowid(lid); RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS agent_actions_write ON trade_os_compat.agent_actions;
CREATE TRIGGER agent_actions_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.agent_actions FOR EACH ROW EXECUTE FUNCTION trade_os_compat.agent_actions_write();

CREATE OR REPLACE FUNCTION trade_os_compat.undo_actions_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE u text:=trosa.compat_current_user(); lid bigint:=coalesce(NEW.id,(SELECT coalesce(max(id),0)+1 FROM trade_os_compat.undo_action_rows WHERE legacy_user_id=u));
BEGIN
 IF TG_OP='DELETE' THEN DELETE FROM trade_os_compat.undo_action_rows WHERE legacy_user_id=u AND id=OLD.id; RETURN OLD; END IF;
 INSERT INTO trade_os_compat.undo_action_rows(legacy_user_id,id,token,operation,target_type,target_id,description,entities,status,created_at,undone_at)
 VALUES(u,lid,coalesce(NEW.token,''),coalesce(NEW.operation,''),coalesce(NEW.target_type,''),NEW.target_id,coalesce(NEW.description,''),coalesce(NEW.entities,'[]'),coalesce(NEW.status,'available'),coalesce(NEW.created_at,''),coalesce(NEW.undone_at,''))
 ON CONFLICT(legacy_user_id,id) DO UPDATE SET token=excluded.token,operation=excluded.operation,target_type=excluded.target_type,target_id=excluded.target_id,description=excluded.description,entities=excluded.entities,status=excluded.status,created_at=excluded.created_at,undone_at=excluded.undone_at;
 PERFORM trosa.compat_set_lastrowid(lid); RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS undo_actions_write ON trade_os_compat.undo_actions;
CREATE TRIGGER undo_actions_write INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.undo_actions FOR EACH ROW EXECUTE FUNCTION trade_os_compat.undo_actions_write();

COMMIT;
