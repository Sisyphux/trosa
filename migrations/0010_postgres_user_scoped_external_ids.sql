-- Scope external-provider and browser-capture identities to the legacy
-- mailbox/user that produced them.  Gmail message ids are only unique within
-- a mailbox, and browser fingerprints are only unique within a user's local
-- SQLite database; organization-wide unique keys silently moved one user's
-- fact to another user's timeline.

BEGIN;

ALTER TABLE trosa.email_message_receipts
    ADD COLUMN IF NOT EXISTS legacy_user_id text;
ALTER TABLE trosa.email_message_receipts
    ALTER COLUMN legacy_user_id SET DEFAULT 'legacy';

ALTER TABLE trosa.communication_source_items
    ADD COLUMN IF NOT EXISTS organization_id uuid;
ALTER TABLE trosa.communication_source_items
    ADD COLUMN IF NOT EXISTS legacy_user_id text;
ALTER TABLE trosa.communication_source_items
    ALTER COLUMN organization_id SET DEFAULT '859a998d-1b48-589b-8035-34dc65c01440'::uuid;
ALTER TABLE trosa.communication_source_items
    ALTER COLUMN legacy_user_id SET DEFAULT 'legacy';

-- The original UNIQUE clauses created these names on PostgreSQL.  Remove the
-- organization-wide keys before adding their user-scoped replacements.
ALTER TABLE trosa.email_message_receipts
    DROP CONSTRAINT IF EXISTS email_message_receipts_organization_id_provider_message_id_key;
ALTER TABLE trosa.communication_source_items
    DROP CONSTRAINT IF EXISTS communication_source_items_source_fingerprint_key;
DROP INDEX IF EXISTS trosa.email_message_receipts_organization_id_provider_message_id_key;
DROP INDEX IF EXISTS trosa.communication_source_items_source_fingerprint_key;

-- Recover the owner from the compatibility references whenever the old
-- canonical row was created before this scope existed.  A provider receipt
-- can be attached to either a timeline or an Inbox row; account ownership is
-- the final fallback for rows that were never attached.
UPDATE trosa.email_message_receipts r
   SET legacy_user_id=COALESCE(
       (SELECT lr.legacy_user_id
          FROM trosa.legacy_row_refs lr
         WHERE lr.organization_id=r.organization_id
           AND lr.target_id=r.timeline_event_id
           AND lr.table_name='follow_up_logs'
         ORDER BY lr.legacy_user_id LIMIT 1),
       (SELECT lr.legacy_user_id
          FROM trosa.legacy_row_refs lr
         WHERE lr.organization_id=r.organization_id
           AND lr.target_id=r.inbox_item_id
           AND lr.table_name='inbox_items'
         ORDER BY lr.legacy_user_id LIMIT 1),
       (SELECT u.legacy_user_id
          FROM trosa.accounts a
          JOIN identity.users u ON u.id=a.owner_user_id
         WHERE a.organization_id=r.organization_id
           AND a.id=r.account_id
         LIMIT 1),
       'legacy')
 WHERE NULLIF(btrim(r.legacy_user_id),'') IS NULL
    OR r.legacy_user_id='legacy';

UPDATE trosa.communication_source_items i
   SET organization_id=COALESCE(
           (SELECT a.organization_id
              FROM trosa.communication_sources s
              JOIN trosa.timeline_events e ON e.id=s.timeline_event_id
              JOIN trosa.accounts a ON a.id=e.account_id
             WHERE s.id=i.communication_source_id
             LIMIT 1),
           '859a998d-1b48-589b-8035-34dc65c01440'::uuid),
       legacy_user_id=COALESCE(
           (SELECT lr.legacy_user_id
              FROM trosa.communication_sources s
              JOIN trosa.legacy_row_refs lr
                ON lr.target_id=s.timeline_event_id
               AND lr.table_name='follow_up_logs'
             WHERE s.id=i.communication_source_id
             ORDER BY lr.legacy_user_id LIMIT 1),
           (SELECT u.legacy_user_id
              FROM trosa.communication_sources s
              JOIN trosa.timeline_events e ON e.id=s.timeline_event_id
              JOIN trosa.accounts a ON a.id=e.account_id
              JOIN identity.users u ON u.id=a.owner_user_id
             WHERE s.id=i.communication_source_id
             LIMIT 1),
           'legacy')
 WHERE i.organization_id IS NULL
    OR NULLIF(btrim(i.legacy_user_id),'') IS NULL
    OR i.legacy_user_id='legacy';

-- A failed/hand-edited rehearsal may already contain duplicate keys after the
-- old organization-wide constraint was removed.  Keep every canonical fact,
-- but quarantine later duplicates under an explicit owner marker so creating
-- the new scoped unique indexes cannot silently discard one of them.
WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY organization_id,legacy_user_id,provider_message_id
               ORDER BY id
           ) AS duplicate_number
      FROM trosa.email_message_receipts
)
UPDATE trosa.email_message_receipts r
   SET legacy_user_id=r.legacy_user_id||':duplicate:'||r.id::text,
       raw_payload=coalesce(r.raw_payload,'{}'::jsonb)||jsonb_build_object(
           'migration_scope_collision',true,'original_legacy_user_id',r.legacy_user_id)
  FROM ranked d
 WHERE r.id=d.id AND d.duplicate_number>1;

WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY organization_id,legacy_user_id,source_fingerprint
               ORDER BY id
           ) AS duplicate_number
      FROM trosa.communication_source_items
)
UPDATE trosa.communication_source_items i
   SET legacy_user_id=i.legacy_user_id||':duplicate:'||i.id::text
  FROM ranked d
 WHERE i.id=d.id AND d.duplicate_number>1;

ALTER TABLE trosa.email_message_receipts
    ALTER COLUMN legacy_user_id SET NOT NULL;
ALTER TABLE trosa.communication_source_items
    ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE trosa.communication_source_items
    ALTER COLUMN legacy_user_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS trosa_email_receipts_org_user_provider_idx
    ON trosa.email_message_receipts (organization_id,legacy_user_id,provider_message_id);
CREATE UNIQUE INDEX IF NOT EXISTS trosa_communication_items_org_user_fp_idx
    ON trosa.communication_source_items (organization_id,legacy_user_id,source_fingerprint);

-- Rebind Gmail receipts to the mailbox/user scope.  The provider id remains
-- the natural key inside that scope, while the canonical UUID is stable for
-- retries and can no longer move a receipt between users.
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
         WHERE organization_id=v_org AND legacy_user_id=v_user
           AND provider_message_id=coalesce(v_provider,'');
        DELETE FROM trade_os_compat.gmail_message_state_rows
         WHERE legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    v_provider := nullif(btrim(coalesce(NEW.provider_message_id,'')), '');
    IF TG_OP='UPDATE' THEN
        IF NEW.id IS DISTINCT FROM OLD.id THEN
            RAISE EXCEPTION 'provider message identity is immutable';
        END IF;
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
    IF TG_OP='UPDATE' AND v_old_provider IS NOT NULL
       AND v_old_provider IS DISTINCT FROM v_provider THEN
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
        (id,organization_id,legacy_user_id,provider_message_id,provider_thread_id,message_time,sender_email,
         recipient_emails,subject,account_id,contact_method_id,timeline_event_id,inbox_item_id,
         match_status,raw_payload,last_error,created_at,updated_at)
    VALUES
        (v_receipt,v_org,v_user,v_provider,coalesce(NEW.provider_thread_id,''),trosa.compat_time(NEW.message_time),
         coalesce(NEW.sender_email,''),trosa.compat_jsonb(NEW.recipient_emails,'[]'::jsonb),
         coalesce(NEW.subject,''),v_account,v_contact,v_timeline,v_inbox,coalesce(NEW.match_status,'unmatched'),
         trosa.compat_jsonb(NEW.raw_payload,'{}'::jsonb),coalesce(NEW.last_error,''),
         coalesce(trosa.compat_time(NEW.created_at),now()),coalesce(trosa.compat_time(NEW.updated_at),now()))
    ON CONFLICT (organization_id,legacy_user_id,provider_message_id) DO UPDATE SET
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

-- Browser/Gmail message fingerprints are also scoped to the legacy user.  A
-- shared canonical table must not let an identical-looking message in a
-- second mailbox rewrite the first mailbox's source relation.
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
        DELETE FROM trosa.communication_source_items
         WHERE organization_id=v_org AND legacy_user_id=v_user
           AND source_fingerprint=OLD.source_fingerprint;
        DELETE FROM trade_os_compat.communication_source_item_rows
         WHERE legacy_user_id=v_user AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF v_fingerprint='' THEN RAISE EXCEPTION 'source_fingerprint is required'; END IF;
    IF TG_OP='UPDATE' AND (
        NEW.id IS DISTINCT FROM OLD.id
        OR
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
     WHERE organization_id=v_org AND legacy_user_id=v_user
       AND source_fingerprint=v_fingerprint LIMIT 1;
    v_item := coalesce(v_item,trosa.compat_uuid('communication-item:'||v_user||':'||v_fingerprint));
    INSERT INTO trosa.communication_source_items
        (id,organization_id,legacy_user_id,communication_source_id,source_fingerprint,message_time,direction,raw_text)
    VALUES
        (v_item,v_org,v_user,v_source,v_fingerprint,trosa.compat_time(NEW.message_time),
         coalesce(NEW.direction,'unknown'),coalesce(NEW.raw_text,''))
    ON CONFLICT (organization_id,legacy_user_id,source_fingerprint) DO UPDATE SET
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
        (v_user,v_id,v_fingerprint,v_activity,coalesce(NEW.message_time,''),
         coalesce(NEW.direction,'unknown'),coalesce(NEW.raw_text,''))
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
DROP TRIGGER IF EXISTS communication_source_items_write ON trade_os_compat.communication_source_items;
CREATE TRIGGER communication_source_items_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.communication_source_items
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.communication_source_items_write();

COMMIT;
