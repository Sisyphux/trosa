-- PostgreSQL runtime integrity hardening.
--
-- The first cutover deliberately kept the SQLite-shaped API alive through
-- INSTEAD OF triggers.  This forward migration closes the remaining gaps that
-- only appear in a shared database: old installations lacked a few columns
-- used by the compatibility views, per-user Inbox keys could collide with a
-- global canonical index, and malformed optional dates could abort a write.

BEGIN;

-- The first cutover migrations predate a few compatibility columns. These
-- ALTERs repair databases that already ran those files and also make a fresh
-- install converge on the same runtime shape without rewriting history.
ALTER TABLE identity.users ADD COLUMN IF NOT EXISTS username text NOT NULL DEFAULT '';
ALTER TABLE identity.users ADD COLUMN IF NOT EXISTS password_hash text NOT NULL DEFAULT '';
ALTER TABLE identity.users ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT 'member';
ALTER TABLE identity.users ADD COLUMN IF NOT EXISTS created_by text NOT NULL DEFAULT '';
ALTER TABLE identity.users ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;
ALTER TABLE identity.users ADD COLUMN IF NOT EXISTS legacy_payload jsonb NOT NULL DEFAULT '{}'::jsonb;
UPDATE identity.users
   SET username=coalesce(nullif(username,''), legacy_user_id),
       active=(status='active'),
       role=coalesce(nullif(role,''), coalesce((
           SELECT m.role FROM identity.memberships m
            WHERE m.organization_id=identity.users.organization_id
              AND m.user_id=identity.users.id
            LIMIT 1), 'member'))
 WHERE username='' OR username IS NULL OR active IS DISTINCT FROM (status='active') OR role='';

ALTER TABLE trosa.research_reports ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT '';
ALTER TABLE trosa.research_reports ADD COLUMN IF NOT EXISTS web_content text NOT NULL DEFAULT '';
ALTER TABLE trosa.research_reports ADD COLUMN IF NOT EXISTS web_fetched_at text NOT NULL DEFAULT '';
ALTER TABLE trosa.research_reports ADD COLUMN IF NOT EXISTS expires_at text NOT NULL DEFAULT '';

-- Legacy integer allocation is per user.  The advisory lock makes the
-- max(id)+1 compatibility strategy safe when two requests arrive together.
CREATE OR REPLACE FUNCTION trosa.compat_next_id(table_name text, legacy_user text)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE result bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('trade-os:compat-id:' || coalesce($1,'') || ':' || coalesce($2,'')));
    IF $1 IN ('reminders','follow_up_logs','outreach_emails',
              'research_reports','external_analysis_notes','customer_understandings',
              'ai_recommendations','inbox_items','web_monitor_logs') THEN
        SELECT coalesce(max(lr.legacy_id),0)+1 INTO result
          FROM trosa.legacy_row_refs lr
         WHERE lr.organization_id=trosa.compat_org_id()
           AND lr.legacy_user_id=$2
           AND lr.table_name=$1;
    ELSIF $1 = 'customers' THEN
        SELECT coalesce(max(ar.legacy_customer_id),0)+1 INTO result
          FROM trosa.account_legacy_refs ar
         WHERE ar.organization_id=trosa.compat_org_id()
           AND ar.legacy_user_id=$2;
    ELSIF $1 = 'contacts' THEN
        SELECT coalesce(max(cr.legacy_contact_id),0)+1 INTO result
          FROM trosa.contact_legacy_refs cr
         WHERE cr.organization_id=trosa.compat_org_id()
           AND cr.legacy_user_id=$2;
    ELSIF $1 = 'customer_files' THEN
        SELECT coalesce(max(id),0)+1 INTO result
          FROM trade_os_compat.customer_file_rows
         WHERE legacy_user_id=$2;
    ELSE
        result := 1;
    END IF;
    RETURN result;
END
$$;

-- The compatibility view exposes the original per-user key.  The canonical
-- row stores a namespaced key, and the raw key is retained in the payload so
-- existing Inbox URLs and dedupe checks remain unchanged to the application.
CREATE OR REPLACE VIEW trade_os_compat.inbox_items AS
SELECT iref.legacy_id AS id,
       CASE WHEN i.account_id IS NULL THEN NULL ELSE aref.legacy_customer_id END AS customer_id,
       i.item_type, i.title, i.content,
       coalesce(i.legacy_payload->>'compat_dedupe_key', i.dedupe_key) AS dedupe_key,
       i.status,
       i.created_at::text AS created_at, coalesce(i.resolved_at::text,'') AS resolved_at,
       coalesce(i.snoozed_until::text,'') AS snoozed_until,
       i.resolution_reason, i.resolution_note
  FROM trosa.inbox_items i
  JOIN trosa.legacy_row_refs iref ON iref.target_id=i.id AND iref.table_name='inbox_items'
  LEFT JOIN trosa.account_legacy_refs aref ON aref.account_id=i.account_id
       AND aref.organization_id=iref.organization_id
       AND aref.legacy_user_id=iref.legacy_user_id
 WHERE iref.organization_id=trosa.compat_org_id()
   AND iref.legacy_user_id=trosa.compat_current_user();

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
        SELECT lr.target_id INTO target
          FROM trosa.legacy_row_refs lr
         WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=u
           AND lr.table_name='inbox_items' AND lr.legacy_id=OLD.id;
        DELETE FROM trosa.legacy_row_refs
         WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u
           AND table_name='inbox_items' AND legacy_id=OLD.id;
        -- A malformed/old store may have shared a canonical row.  Do not
        -- delete data still referenced by another user's compatibility key.
        IF target IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM trosa.legacy_row_refs WHERE target_id=target
        ) THEN
            DELETE FROM trosa.inbox_items WHERE id=target;
        END IF;
        RETURN OLD;
    END IF;

    account := trosa.compat_customer_account(NEW.customer_id,u);
    IF TG_OP='UPDATE' THEN
        lid := NEW.id;
        SELECT lr.target_id, i.dedupe_key, i.legacy_payload->>'compat_dedupe_key'
          INTO target, existing_key, existing_compat_key
          FROM trosa.legacy_row_refs lr
          LEFT JOIN trosa.inbox_items i ON i.id=lr.target_id
         WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=u
           AND lr.table_name='inbox_items' AND lr.legacy_id=lid;
    ELSE
        IF coalesce(NEW.id,0) <> 0 THEN
            lid := NEW.id;
            SELECT lr.target_id, i.dedupe_key, i.legacy_payload->>'compat_dedupe_key'
              INTO target, existing_key, existing_compat_key
              FROM trosa.legacy_row_refs lr
              LEFT JOIN trosa.inbox_items i ON i.id=lr.target_id
             WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=u
               AND lr.table_name='inbox_items' AND lr.legacy_id=lid;
        END IF;
        IF target IS NULL AND raw_key <> '' THEN
            SELECT lr.legacy_id, lr.target_id, i.dedupe_key, i.legacy_payload->>'compat_dedupe_key'
              INTO lid, target, existing_key, existing_compat_key
              FROM trosa.legacy_row_refs lr
              JOIN trosa.inbox_items i ON i.id=lr.target_id
             WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=u
               AND lr.table_name='inbox_items'
               AND (i.legacy_payload->>'compat_dedupe_key'=raw_key
                    OR (coalesce(i.legacy_payload->>'compat_dedupe_key','')='' AND i.dedupe_key=raw_key))
             ORDER BY i.created_at, lr.legacy_id
             LIMIT 1;
        END IF;
        IF lid IS NULL THEN
            lid := trosa.compat_next_id('inbox_items',u);
        END IF;
    END IF;

    target := coalesce(target, trosa.compat_uuid('inbox:'||u||':'||lid::text));
    stored_key := CASE
        WHEN raw_key='' THEN ''
        WHEN coalesce(existing_compat_key,existing_key)=raw_key THEN coalesce(existing_key,raw_key)
        ELSE 'compat:'||u||':'||raw_key
    END;
    payload := coalesce(to_jsonb(NEW),'{}'::jsonb);
    IF raw_key <> '' THEN
        payload := payload || jsonb_build_object('compat_dedupe_key',raw_key);
    ELSE
        payload := payload - 'compat_dedupe_key';
    END IF;
    INSERT INTO trosa.inbox_items
        (id,account_id,item_type,title,content,dedupe_key,status,resolved_at,snoozed_until,
         resolution_reason,resolution_note,legacy_payload)
    VALUES
        (target,account,coalesce(NEW.item_type,''),coalesce(NEW.title,''),coalesce(NEW.content,''),
         stored_key,coalesce(NEW.status,'open'),trosa.compat_time(NEW.resolved_at),
         trosa.compat_time(NEW.snoozed_until),coalesce(NEW.resolution_reason,''),
         coalesce(NEW.resolution_note,''),payload)
    ON CONFLICT(id) DO UPDATE SET
        account_id=excluded.account_id,item_type=excluded.item_type,title=excluded.title,
        content=excluded.content,dedupe_key=excluded.dedupe_key,status=excluded.status,
        resolved_at=excluded.resolved_at,snoozed_until=excluded.snoozed_until,
        resolution_reason=excluded.resolution_reason,resolution_note=excluded.resolution_note,
        legacy_payload=(CASE WHEN raw_key='' THEN trosa.inbox_items.legacy_payload-'compat_dedupe_key'
                             ELSE trosa.inbox_items.legacy_payload END) || excluded.legacy_payload;
    INSERT INTO trosa.legacy_row_refs
        (organization_id,legacy_user_id,table_name,legacy_id,target_id)
    VALUES (trosa.compat_org_id(),u,'inbox_items',lid,target)
    ON CONFLICT(organization_id,legacy_user_id,table_name,legacy_id)
    DO UPDATE SET target_id=excluded.target_id;
    PERFORM trosa.compat_set_lastrowid(lid);
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS inbox_items_write ON trade_os_compat.inbox_items;
CREATE TRIGGER inbox_items_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.inbox_items
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.inbox_items_write();

-- Optional date fields must never turn a malformed legacy value into a 500.
-- Required dates are still validated by the importer/application before they
-- reach these functions.
CREATE OR REPLACE FUNCTION trosa.compat_customers_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_legacy_user text := trosa.compat_current_user();
    v_legacy_customer_id bigint;
    v_company_id uuid;
    v_account_id uuid;
    v_company_name text;
    v_normalized_name text;
    v_country text;
    v_website text;
    v_domain text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        UPDATE trosa.accounts SET deleted_at=now(),updated_at=now()
         WHERE id=(SELECT ar.account_id FROM trosa.account_legacy_refs ar
                    WHERE ar.organization_id=trosa.compat_org_id()
                      AND ar.legacy_user_id=v_legacy_user AND ar.legacy_customer_id=OLD.id);
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'customer identity is immutable';
    END IF;
    IF TG_OP='INSERT' AND (NEW.id IS NULL OR NEW.id=0) THEN
        v_legacy_customer_id := trosa.compat_next_id('customers',v_legacy_user);
    ELSE
        v_legacy_customer_id := NEW.id;
    END IF;
    v_company_name := coalesce(nullif(trim(NEW.company),''), nullif(trim(NEW.name),''), 'UNKNOWN');
    v_normalized_name := trosa.compat_normalized_name(v_company_name);
    v_country := coalesce(NEW.country,'');
    v_website := coalesce(NEW.website,'');
    v_domain := trosa.compat_domain(v_website);
    -- Once a legacy customer has been linked, that link is the source of
    -- truth for subsequent edits.  Re-matching by a newly edited website or
    -- name would silently move the customer to another account and leave the
    -- old account orphaned.
    SELECT ar.account_id,a.company_id INTO v_account_id,v_company_id
      FROM trosa.account_legacy_refs ar
      JOIN trosa.accounts a ON a.id=ar.account_id
     WHERE ar.organization_id=trosa.compat_org_id()
       AND ar.legacy_user_id=v_legacy_user
       AND ar.legacy_customer_id=v_legacy_customer_id
     LIMIT 1;
    IF v_company_id IS NULL AND v_domain <> '' THEN
        SELECT d.company_id INTO v_company_id
          FROM core.company_domains d
          JOIN core.companies c ON c.id=d.company_id
         WHERE c.organization_id=trosa.compat_org_id()
           AND d.normalized_domain=v_domain
         ORDER BY d.is_primary DESC, d.created_at ASC
         LIMIT 1;
    ELSIF v_company_id IS NULL THEN
        SELECT c.id INTO v_company_id FROM core.companies c
         WHERE c.organization_id=trosa.compat_org_id() AND c.normalized_name=v_normalized_name
           AND c.country_code=v_country LIMIT 1;
    END IF;
    IF v_company_id IS NULL THEN
        v_company_id := trosa.compat_uuid(CASE WHEN v_domain<>'' THEN 'company:domain:'||v_domain
                                               ELSE 'company:name:'||v_normalized_name||'|'||trosa.compat_normalized_name(v_country) END);
        INSERT INTO core.companies(id,organization_id,canonical_name,normalized_name,website,country_code)
        VALUES (v_company_id,trosa.compat_org_id(),v_company_name,v_normalized_name,v_website,v_country)
        ON CONFLICT(id) DO UPDATE SET canonical_name=excluded.canonical_name,
          normalized_name=excluded.normalized_name,website=excluded.website,
          country_code=excluded.country_code,updated_at=now();
    ELSE
        UPDATE core.companies SET canonical_name=v_company_name,website=v_website,
          country_code=v_country,updated_at=now()
         WHERE id=v_company_id AND (TG_OP='INSERT' OR coalesce(NEW.company,'')<>'');
    END IF;
    IF v_domain<>'' THEN
        INSERT INTO core.company_domains(id,company_id,normalized_domain,source_url,is_primary,verification_status)
        VALUES (trosa.compat_uuid('company-domain:'||v_domain),v_company_id,v_domain,v_website,true,'imported')
        ON CONFLICT(company_id,normalized_domain) DO UPDATE SET source_url=excluded.source_url;
    END IF;
    IF v_account_id IS NULL THEN
        SELECT a.id INTO v_account_id FROM trosa.accounts a
         WHERE a.organization_id=trosa.compat_org_id() AND a.company_id=v_company_id LIMIT 1;
    END IF;
    v_account_id := coalesce(v_account_id, trosa.compat_uuid('account:'||v_company_id::text));
    INSERT INTO trosa.accounts
      (id,organization_id,company_id,display_name,account_status,customer_type,channel_type,priority_level,
       profile,field,industry,company_size,annual_revenue,tags,attention_state,attention_reason,
       last_contact_at,next_follow_up_at,deleted_at,legacy_payload,updated_at)
    VALUES
      (v_account_id,trosa.compat_org_id(),v_company_id,coalesce(NEW.name,''),coalesce(NEW.status,''),
       coalesce(NEW.customer_type,'existing'),coalesce(NEW.type,''),coalesce(NEW.level,'C'),
       coalesce(NEW.profile,''),coalesce(NEW.field,''),coalesce(NEW.industry,''),coalesce(NEW.company_size,''),
       coalesce(NEW.annual_revenue,''),coalesce(NEW.tags,''),coalesce(NEW.attention_state,''),
       coalesce(NEW.attention_reason,''),trosa.compat_time(NEW.last_contact),
       trosa.compat_time(NEW.next_follow_up),CASE WHEN coalesce(NEW.is_deleted,0)=1 THEN now() ELSE NULL END,
       coalesce(to_jsonb(NEW),'{}'::jsonb),now())
    ON CONFLICT(organization_id,company_id) DO UPDATE SET display_name=excluded.display_name,
      account_status=excluded.account_status,customer_type=excluded.customer_type,
      channel_type=excluded.channel_type,priority_level=excluded.priority_level,profile=excluded.profile,
      field=excluded.field,industry=excluded.industry,company_size=excluded.company_size,
      annual_revenue=excluded.annual_revenue,tags=excluded.tags,attention_state=excluded.attention_state,
      attention_reason=excluded.attention_reason,last_contact_at=excluded.last_contact_at,
      next_follow_up_at=excluded.next_follow_up_at,deleted_at=excluded.deleted_at,
      legacy_payload=trosa.accounts.legacy_payload||excluded.legacy_payload,updated_at=now();
    INSERT INTO trosa.account_legacy_refs
      (organization_id,legacy_user_id,legacy_customer_id,account_id,source_db)
    VALUES (trosa.compat_org_id(),v_legacy_user,v_legacy_customer_id,v_account_id,v_legacy_user||'.db')
    ON CONFLICT(organization_id,legacy_user_id,legacy_customer_id) DO UPDATE SET account_id=excluded.account_id;
    PERFORM trosa.compat_set_lastrowid(v_legacy_customer_id);
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trosa.compat_contacts_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_legacy_user text:=trosa.compat_current_user();
    v_contact_id bigint;
    v_account_id uuid;
    v_company_id uuid;
    v_person_id uuid;
    v_method_id uuid;
    v_email_person_id uuid;
    email_value text:=lower(trim(coalesce(NEW.email,'')));
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM trosa.contact_legacy_refs
         WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=v_legacy_user
           AND legacy_contact_id=OLD.id;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'contact identity is immutable';
    END IF;
    IF TG_OP='INSERT' AND (NEW.id IS NULL OR NEW.id=0) THEN
        v_contact_id:=trosa.compat_next_id('contacts',v_legacy_user);
    ELSE
        v_contact_id:=NEW.id;
    END IF;
    SELECT r.account_id,c.company_id INTO v_account_id,v_company_id
      FROM trosa.account_legacy_refs r JOIN trosa.accounts c ON c.id=r.account_id
     WHERE r.organization_id=trosa.compat_org_id() AND r.legacy_user_id=v_legacy_user
       AND r.legacy_customer_id=NEW.customer_id LIMIT 1;
    IF v_account_id IS NULL THEN
        RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,v_legacy_user;
    END IF;
    IF NEW.id IS NOT NULL AND NEW.id<>0 THEN
        SELECT cr.person_id INTO v_person_id FROM trosa.contact_legacy_refs cr
         WHERE cr.organization_id=trosa.compat_org_id() AND cr.legacy_user_id=v_legacy_user
           AND cr.legacy_contact_id=NEW.id;
    END IF;
    IF email_value<>'' THEN
        -- Exact normalized email is the only automatic cross-row person
        -- match.  Reuse its current person before deriving a new UUID so
        -- a contact imported with a different historical UUID cannot split
        -- into two people when first edited at runtime.  This intentionally
        -- also handles an existing legacy contact whose email was changed.
        SELECT cm.person_id INTO v_email_person_id
          FROM core.contact_methods cm
         WHERE cm.organization_id=trosa.compat_org_id()
           AND cm.kind='email' AND cm.normalized_value=email_value
         LIMIT 1;
    END IF;
    IF v_email_person_id IS NOT NULL THEN
        v_person_id:=v_email_person_id;
    ELSIF v_person_id IS NULL THEN
        IF email_value<>'' THEN
            v_person_id:=trosa.compat_uuid('person:email:'||email_value);
        ELSE
            v_person_id:=trosa.compat_uuid('person:'||v_legacy_user||':'||v_contact_id::text);
        END IF;
    END IF;
    INSERT INTO core.people(id,organization_id,full_name,normalized_name)
    VALUES(v_person_id,trosa.compat_org_id(),coalesce(nullif(trim(NEW.name),''),'UNKNOWN'),
      trosa.compat_normalized_name(NEW.name))
    ON CONFLICT(id) DO UPDATE SET full_name=CASE WHEN core.people.full_name IN ('','UNKNOWN')
      THEN excluded.full_name ELSE core.people.full_name END,updated_at=now();
    INSERT INTO core.company_people(id,company_id,person_id,title,source)
    VALUES(trosa.compat_uuid('company-person:'||v_company_id::text||':'||v_person_id::text||':'||coalesce(NEW.title,'')),
      v_company_id,v_person_id,coalesce(NEW.title,''),'trosa') ON CONFLICT DO NOTHING;
    IF email_value<>'' THEN
        INSERT INTO core.contact_methods(id,organization_id,person_id,kind,value,normalized_value,evidence)
        VALUES(trosa.compat_uuid('email:'||email_value),trosa.compat_org_id(),v_person_id,'email',email_value,email_value,'{}'::jsonb)
        ON CONFLICT(organization_id,kind,normalized_value) DO UPDATE SET
          person_id=coalesce(core.contact_methods.person_id,excluded.person_id),
          company_id=CASE WHEN coalesce(core.contact_methods.person_id,excluded.person_id) IS NULL
                          THEN core.contact_methods.company_id ELSE NULL END,
          updated_at=now();
        SELECT cm.id INTO v_method_id FROM core.contact_methods cm
         WHERE cm.organization_id=trosa.compat_org_id() AND cm.kind='email'
           AND cm.normalized_value=email_value;
    END IF;
    INSERT INTO trosa.contact_legacy_refs
      (organization_id,legacy_user_id,legacy_contact_id,legacy_customer_id,account_id,person_id,contact_method_id,
       name,title,phone,whatsapp,linkedin,preferred_channel,contact_type,is_primary,notes,legacy_payload,updated_at)
    VALUES(trosa.compat_org_id(),v_legacy_user,v_contact_id,NEW.customer_id,v_account_id,v_person_id,v_method_id,
      coalesce(NEW.name,''),coalesce(NEW.title,''),coalesce(NEW.phone,''),coalesce(NEW.whatsapp,''),
      coalesce(NEW.linkedin,''),coalesce(NEW.preferred_channel,''),coalesce(NEW.contact_type,'person'),
      coalesce(NEW.is_primary,0)<>0,coalesce(NEW.notes,''),coalesce(to_jsonb(NEW),'{}'::jsonb),now())
    ON CONFLICT(organization_id,legacy_user_id,legacy_contact_id) DO UPDATE SET
      legacy_customer_id=excluded.legacy_customer_id,account_id=excluded.account_id,person_id=excluded.person_id,
      contact_method_id=excluded.contact_method_id,name=excluded.name,title=excluded.title,phone=excluded.phone,
      whatsapp=excluded.whatsapp,linkedin=excluded.linkedin,preferred_channel=excluded.preferred_channel,
      contact_type=excluded.contact_type,is_primary=excluded.is_primary,notes=excluded.notes,
      legacy_payload=excluded.legacy_payload,updated_at=now();
    PERFORM trosa.compat_set_lastrowid(v_contact_id); RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trosa.compat_reminders_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_legacy_user text:=trosa.compat_current_user();
    v_legacy_id bigint;
    v_account_id uuid;
    v_target_id uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        SELECT lr.target_id INTO v_target_id FROM trosa.legacy_row_refs lr
          WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_legacy_user
            AND lr.table_name='reminders' AND lr.legacy_id=OLD.id;
        DELETE FROM trosa.legacy_row_refs
         WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=v_legacy_user
           AND table_name='reminders' AND legacy_id=OLD.id;
        IF v_target_id IS NOT NULL AND (
            EXISTS (SELECT 1 FROM trosa.web_monitor_observations WHERE task_id=v_target_id)
            OR EXISTS (SELECT 1 FROM trosa.legacy_row_refs WHERE target_id=v_target_id)
        ) THEN
            -- web_monitor_observations.task_id is a NO ACTION foreign key.
            -- Retain a completed tombstone when another canonical row still
            -- points at the task, while hiding it from the legacy view.
            UPDATE trosa.tasks
               SET status='done', completed_at=coalesce(completed_at,now()),
                   legacy_payload=coalesce(legacy_payload,'{}'::jsonb)||jsonb_build_object(
                       'is_deleted','1','deleted_at',now()::text), updated_at=now()
             WHERE id=v_target_id;
        ELSIF v_target_id IS NOT NULL THEN
            DELETE FROM trosa.tasks WHERE id=v_target_id;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'reminder identity is immutable';
    END IF;
    v_legacy_id:=CASE WHEN TG_OP='INSERT' AND (NEW.id IS NULL OR NEW.id=0)
      THEN trosa.compat_next_id('reminders',v_legacy_user) ELSE NEW.id END;
    SELECT ar.account_id INTO v_account_id FROM trosa.account_legacy_refs ar
     WHERE ar.organization_id=trosa.compat_org_id() AND ar.legacy_user_id=v_legacy_user
       AND ar.legacy_customer_id=NEW.customer_id;
    IF v_account_id IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,v_legacy_user; END IF;
    SELECT lr.target_id INTO v_target_id FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_legacy_user
       AND lr.table_name='reminders' AND lr.legacy_id=v_legacy_id;
    v_target_id:=coalesce(v_target_id,trosa.compat_uuid('task:'||v_legacy_user||':'||v_legacy_id::text));
    INSERT INTO trosa.tasks(id,account_id,title,content,reason,due_at,status,task_type,source_activity_legacy_id,manual_order,completed_at,legacy_payload,updated_at)
    VALUES (v_target_id,v_account_id,coalesce(NEW.title,''),coalesce(NEW.content,''),coalesce(NEW.reason,''),
      trosa.compat_time(NEW.remind_date),CASE WHEN coalesce(NEW.is_done,0)<>0 THEN 'done' ELSE 'open' END,
      coalesce(NEW.reminder_type,'follow_up'),coalesce(NEW.source_activity_id::text,''),coalesce(NEW.manual_order,0),
      trosa.compat_time(NEW.completed_at),coalesce(to_jsonb(NEW),'{}'::jsonb),now())
    ON CONFLICT(id) DO UPDATE SET title=excluded.title,content=excluded.content,reason=excluded.reason,
      due_at=excluded.due_at,status=excluded.status,task_type=excluded.task_type,
      source_activity_legacy_id=excluded.source_activity_legacy_id,manual_order=excluded.manual_order,
      completed_at=excluded.completed_at,legacy_payload=trosa.tasks.legacy_payload||excluded.legacy_payload,updated_at=now();
    INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id)
    VALUES(trosa.compat_org_id(),v_legacy_user,'reminders',v_legacy_id,v_target_id)
    ON CONFLICT(organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id;
    PERFORM trosa.compat_set_lastrowid(v_legacy_id); RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trosa.compat_follow_up_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_legacy_user text:=trosa.compat_current_user(); v_legacy_id bigint; v_account_id uuid; v_target_id uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM trosa.timeline_events WHERE id=(SELECT lr.target_id FROM trosa.legacy_row_refs lr
          WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_legacy_user
            AND lr.table_name='follow_up_logs' AND lr.legacy_id=OLD.id); RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'communication identity is immutable';
    END IF;
    v_legacy_id:=CASE WHEN TG_OP='INSERT' AND (NEW.id IS NULL OR NEW.id=0)
      THEN trosa.compat_next_id('follow_up_logs',v_legacy_user) ELSE NEW.id END;
    SELECT ar.account_id INTO v_account_id FROM trosa.account_legacy_refs ar
     WHERE ar.organization_id=trosa.compat_org_id() AND ar.legacy_user_id=v_legacy_user
       AND ar.legacy_customer_id=NEW.customer_id;
    IF v_account_id IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,v_legacy_user; END IF;
    SELECT lr.target_id INTO v_target_id FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_legacy_user
       AND lr.table_name='follow_up_logs' AND lr.legacy_id=v_legacy_id;
    v_target_id:=coalesce(v_target_id,trosa.compat_uuid('timeline:'||v_legacy_user||':'||v_legacy_id::text));
    INSERT INTO trosa.timeline_events(id,account_id,event_type,direction,content,result,next_plan,source_module,source_reference,occurred_at,payload)
    VALUES(v_target_id,v_account_id,coalesce(NEW.activity_type,'follow_up'),coalesce(NEW.direction,'unknown'),
      coalesce(NEW.content,''),coalesce(NEW.result,''),coalesce(NEW.next_plan,''),'trosa',
      v_legacy_user||':'||v_legacy_id::text,trosa.compat_time(NEW.follow_date),coalesce(to_jsonb(NEW),'{}'::jsonb))
    ON CONFLICT(id) DO UPDATE SET event_type=excluded.event_type,direction=excluded.direction,
      content=excluded.content,result=excluded.result,next_plan=excluded.next_plan,
      occurred_at=excluded.occurred_at,payload=trosa.timeline_events.payload||excluded.payload;
    INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id)
    VALUES(trosa.compat_org_id(),v_legacy_user,'follow_up_logs',v_legacy_id,v_target_id)
    ON CONFLICT(organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id;
    PERFORM trosa.compat_set_lastrowid(v_legacy_id); RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trosa.compat_outreach_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_legacy_user text:=trosa.compat_current_user(); v_legacy_id bigint; v_account_id uuid; v_target_id uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM trosa.outreach_messages WHERE id=(SELECT lr.target_id FROM trosa.legacy_row_refs lr
          WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_legacy_user
            AND lr.table_name='outreach_emails' AND lr.legacy_id=OLD.id); RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'outreach identity is immutable';
    END IF;
    v_legacy_id:=CASE WHEN TG_OP='INSERT' AND (NEW.id IS NULL OR NEW.id=0)
      THEN trosa.compat_next_id('outreach_emails',v_legacy_user) ELSE NEW.id END;
    SELECT ar.account_id INTO v_account_id FROM trosa.account_legacy_refs ar
     WHERE ar.organization_id=trosa.compat_org_id() AND ar.legacy_user_id=v_legacy_user
       AND ar.legacy_customer_id=NEW.customer_id;
    IF v_account_id IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,v_legacy_user; END IF;
    SELECT lr.target_id INTO v_target_id FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_legacy_user
       AND lr.table_name='outreach_emails' AND lr.legacy_id=v_legacy_id;
    v_target_id:=coalesce(v_target_id,trosa.compat_uuid('trosa-message:'||v_legacy_user||':'||v_legacy_id::text));
    INSERT INTO trosa.outreach_messages(id,account_id,subject,body,sent_at,reply_status,reply_content,reply_at,provider,provider_message_id,legacy_payload)
    VALUES(v_target_id,v_account_id,coalesce(NEW.subject,''),coalesce(NEW.content,''),trosa.compat_time(NEW.sent_date),
      coalesce(NEW.reply_status,'pending'),coalesce(NEW.reply_content,''),trosa.compat_time(NEW.reply_date),'legacy',
      coalesce(nullif(NEW.message_id,''),nullif(NEW.external_id,'')),coalesce(to_jsonb(NEW),'{}'::jsonb))
    ON CONFLICT(id) DO UPDATE SET subject=excluded.subject,body=excluded.body,sent_at=excluded.sent_at,
      reply_status=excluded.reply_status,reply_content=excluded.reply_content,reply_at=excluded.reply_at,
      provider_message_id=excluded.provider_message_id,legacy_payload=trosa.outreach_messages.legacy_payload||excluded.legacy_payload;
    INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id)
    VALUES(trosa.compat_org_id(),v_legacy_user,'outreach_emails',v_legacy_id,v_target_id)
    ON CONFLICT(organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id;
    PERFORM trosa.compat_set_lastrowid(v_legacy_id); RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trade_os_compat.web_monitor_logs_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE u text:=trosa.compat_current_user(); lid bigint; target uuid; account uuid; task uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id()
          AND legacy_user_id=u AND table_name='web_monitor_logs' AND legacy_id=OLD.id;
        IF target IS NOT NULL THEN DELETE FROM trosa.web_monitor_observations WHERE id=target;
          DELETE FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u
            AND table_name='web_monitor_logs' AND legacy_id=OLD.id; END IF;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'web monitor identity is immutable';
    END IF;
    lid:=CASE WHEN TG_OP='INSERT' AND coalesce(NEW.id,0)=0 THEN trosa.compat_next_id('web_monitor_logs',u) ELSE NEW.id END;
    account:=trosa.compat_customer_account(NEW.customer_id,u);
    IF account IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,u; END IF;
    SELECT target_id INTO target FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id()
      AND legacy_user_id=u AND table_name='web_monitor_logs' AND legacy_id=lid;
    SELECT target_id INTO task FROM trosa.legacy_row_refs WHERE organization_id=trosa.compat_org_id()
      AND legacy_user_id=u AND table_name='reminders' AND legacy_id=NEW.reminder_id;
    target:=coalesce(target,trosa.compat_uuid('web-monitor:'||u||':'||lid::text));
    INSERT INTO trosa.web_monitor_observations(id,account_id,url,status,content_hash,content_snippet,change_summary,checked_at,task_id,legacy_payload)
    VALUES(target,account,coalesce(NEW.url,''),coalesce(NEW.status,'ok'),coalesce(NEW.content_hash,''),
      coalesce(NEW.content_snippet,''),coalesce(NEW.change_summary,''),coalesce(trosa.compat_time(NEW.checked_at),now()),
      task,to_jsonb(NEW))
    ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,url=excluded.url,status=excluded.status,
      content_hash=excluded.content_hash,content_snippet=excluded.content_snippet,change_summary=excluded.change_summary,
      checked_at=excluded.checked_at,task_id=excluded.task_id,legacy_payload=trosa.web_monitor_observations.legacy_payload||excluded.legacy_payload;
    INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id)
    VALUES(trosa.compat_org_id(),u,'web_monitor_logs',lid,target)
    ON CONFLICT(organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id;
    PERFORM trosa.compat_set_lastrowid(lid); RETURN NEW;
END
$$;

-- File paths are relative to separate SQLite stores, so the same path may be
-- valid for several legacy users.  Namespace the canonical storage key while
-- keeping the old path in the compatibility ledger.
CREATE OR REPLACE FUNCTION trade_os_compat.customer_files_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE u text:=trosa.compat_current_user(); lid bigint; account uuid; file_id uuid; storage_key text;
BEGIN
    IF TG_OP='DELETE' THEN
        UPDATE core.file_objects SET deleted_at=now()
         WHERE id=(SELECT file_object_id FROM trade_os_compat.customer_file_rows WHERE legacy_user_id=u AND id=OLD.id);
        UPDATE trade_os_compat.customer_file_rows SET is_deleted=1,deleted_at=now()::text
         WHERE legacy_user_id=u AND id=OLD.id;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'customer file identity is immutable';
    END IF;
    IF TG_OP='INSERT' AND coalesce(NEW.id,0)=0 THEN
        lid:=trosa.compat_next_id('customer_files',u);
    ELSE
        lid:=NEW.id;
    END IF;
    account:=trosa.compat_customer_account(NEW.customer_id,u);
    IF account IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,u; END IF;
    storage_key:=u||':'||CASE WHEN coalesce(NEW.file_path,'')<>'' THEN NEW.file_path
                              ELSE 'legacy-file:'||lid::text END;
    file_id:=trosa.compat_uuid('file:'||u||':'||lid::text);
    INSERT INTO core.file_objects
      (id,organization_id,storage_key,original_name,mime_type,size_bytes,sha256,uploaded_by_user_id,deleted_at)
    VALUES(file_id,trosa.compat_org_id(),storage_key,coalesce(NEW.original_name,''),coalesce(NEW.mime_type,''),
      coalesce(NEW.file_size,0),coalesce(NEW.sha256,''),
      (SELECT id FROM identity.users WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=u LIMIT 1),
      CASE WHEN coalesce(NEW.is_deleted,0)<>0 THEN now() ELSE NULL END)
    ON CONFLICT(id) DO UPDATE SET storage_key=excluded.storage_key,original_name=excluded.original_name,
      mime_type=excluded.mime_type,size_bytes=excluded.size_bytes,sha256=excluded.sha256,deleted_at=excluded.deleted_at;
    INSERT INTO core.entity_files(id,file_object_id,account_id,relation_type)
    VALUES(trosa.compat_uuid('entity-file:'||u||':'||lid::text),file_id,account,'attachment')
    ON CONFLICT(id) DO UPDATE SET file_object_id=excluded.file_object_id,account_id=excluded.account_id,
      company_id=NULL,prospect_id=NULL,relation_type=excluded.relation_type;
    INSERT INTO trade_os_compat.customer_file_rows
      (legacy_user_id,id,customer_id,account_id,file_object_id,original_name,stored_name,file_path,file_size,
       mime_type,category,sha256,uploaded_by,is_deleted,deleted_at,created_at)
    VALUES(u,lid,NEW.customer_id,account,file_id,coalesce(NEW.original_name,''),coalesce(NEW.stored_name,''),
      coalesce(NEW.file_path,''),coalesce(NEW.file_size,0),coalesce(NEW.mime_type,''),coalesce(NEW.category,''),
      coalesce(NEW.sha256,''),coalesce(NEW.uploaded_by,''),coalesce(NEW.is_deleted,0),coalesce(NEW.deleted_at,''),
      coalesce(NEW.created_at,''))
    ON CONFLICT(legacy_user_id,id) DO UPDATE SET customer_id=excluded.customer_id,account_id=excluded.account_id,
      file_object_id=excluded.file_object_id,original_name=excluded.original_name,stored_name=excluded.stored_name,
      file_path=excluded.file_path,file_size=excluded.file_size,mime_type=excluded.mime_type,category=excluded.category,
      sha256=excluded.sha256,uploaded_by=excluded.uploaded_by,is_deleted=excluded.is_deleted,
      deleted_at=excluded.deleted_at,created_at=excluded.created_at;
    PERFORM trosa.compat_set_lastrowid(lid); RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS customer_files_write ON trade_os_compat.customer_files;
CREATE TRIGGER customer_files_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.customer_files
FOR EACH ROW EXECUTE FUNCTION trade_os_compat.customer_files_write();

COMMIT;
