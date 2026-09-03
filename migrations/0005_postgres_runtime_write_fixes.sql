-- Runtime compatibility fixes discovered by the non-production application
-- write rehearsal.  This is a forward-only schema migration: it changes
-- trigger function bodies and projections, never the legacy source files.

BEGIN;

CREATE OR REPLACE VIEW trosa.reminders AS
SELECT lr.legacy_id AS id,
       ar.legacy_customer_id AS customer_id,
       t.title,
       t.content,
       t.reason,
       t.due_at::text AS remind_date,
       CASE WHEN t.status='done' THEN 1 ELSE 0 END AS is_done,
       t.task_type AS reminder_type,
       COALESCE(t.completed_at::text, '') AS completed_at,
       NULLIF(t.source_activity_legacy_id, '')::bigint AS source_activity_id,
       t.manual_order,
       t.created_at::text AS created_at,
       t.updated_at::text AS updated_at
FROM trosa.legacy_row_refs lr
JOIN trosa.tasks t ON t.id=lr.target_id
JOIN trosa.account_legacy_refs ar ON ar.account_id=t.account_id
 AND ar.organization_id=lr.organization_id AND ar.legacy_user_id=lr.legacy_user_id
WHERE lr.organization_id=trosa.compat_org_id()
  AND lr.legacy_user_id=trosa.compat_current_user() AND lr.table_name='reminders';

CREATE OR REPLACE FUNCTION trosa.compat_next_id(table_name text, legacy_user text)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE result bigint;
BEGIN
    IF $1 IN ('research_reports','external_analysis_notes','customer_understandings',
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
        UPDATE trosa.accounts
           SET deleted_at=now(),updated_at=now()
         WHERE id=(SELECT ar.account_id FROM trosa.account_legacy_refs ar
                   WHERE ar.organization_id=trosa.compat_org_id()
                     AND ar.legacy_user_id=v_legacy_user AND ar.legacy_customer_id=OLD.id);
        RETURN OLD;
    END IF;

    v_legacy_customer_id := CASE WHEN TG_OP='INSERT' AND (NEW.id IS NULL OR NEW.id=0)
        THEN (SELECT coalesce(max(ar.legacy_customer_id),0)+1 FROM trosa.account_legacy_refs ar
              WHERE ar.organization_id=trosa.compat_org_id() AND ar.legacy_user_id=v_legacy_user)
        ELSE NEW.id END;
    v_company_name := coalesce(nullif(trim(NEW.company),''), nullif(trim(NEW.name),''), 'UNKNOWN');
    v_normalized_name := trosa.compat_normalized_name(v_company_name);
    v_country := coalesce(NEW.country,'');
    v_website := coalesce(NEW.website,'');
    v_domain := trosa.compat_domain(v_website);

    IF v_domain <> '' THEN
        SELECT d.company_id INTO v_company_id FROM core.company_domains d
         WHERE d.normalized_domain=v_domain LIMIT 1;
    ELSE
        SELECT c.id INTO v_company_id FROM core.companies c
         WHERE c.organization_id=trosa.compat_org_id()
           AND c.normalized_name=v_normalized_name AND c.country_code=v_country LIMIT 1;
    END IF;
    IF v_company_id IS NULL THEN
        v_company_id := trosa.compat_uuid(CASE WHEN v_domain<>'' THEN 'company:domain:'||v_domain
                                               ELSE 'company:name:'||v_normalized_name||'|'||trosa.compat_normalized_name(v_country) END);
        INSERT INTO core.companies(id,organization_id,canonical_name,normalized_name,website,country_code)
        VALUES (v_company_id,trosa.compat_org_id(),v_company_name,v_normalized_name,v_website,v_country)
        ON CONFLICT (id) DO UPDATE SET canonical_name=excluded.canonical_name,
          normalized_name=excluded.normalized_name,website=excluded.website,country_code=excluded.country_code,updated_at=now();
    ELSE
        UPDATE core.companies SET canonical_name=v_company_name,website=v_website,country_code=v_country,updated_at=now()
         WHERE id=v_company_id AND (TG_OP='INSERT' OR coalesce(NEW.company,'')<>'');
    END IF;
    IF v_domain<>'' THEN
        INSERT INTO core.company_domains(id,company_id,normalized_domain,source_url,is_primary,verification_status)
        VALUES (trosa.compat_uuid('company-domain:'||v_domain),v_company_id,v_domain,v_website,true,'imported')
        ON CONFLICT (company_id,normalized_domain) DO UPDATE SET source_url=excluded.source_url;
    END IF;
    -- Imported accounts use the canonical importer UUID namespace.  Reuse
    -- the account already attached to this company before falling back to a
    -- deterministic compatibility id for a genuinely new customer.
    SELECT a.id INTO v_account_id
      FROM trosa.accounts a
     WHERE a.organization_id=trosa.compat_org_id() AND a.company_id=v_company_id
     LIMIT 1;
    v_account_id := coalesce(v_account_id, trosa.compat_uuid('account:'||v_company_id::text));
    INSERT INTO trosa.accounts(id,organization_id,company_id,display_name,account_status,customer_type,channel_type,priority_level,profile,field,industry,company_size,annual_revenue,tags,attention_state,attention_reason,last_contact_at,next_follow_up_at,deleted_at,legacy_payload,updated_at)
    VALUES (v_account_id,trosa.compat_org_id(),v_company_id,coalesce(NEW.name,''),coalesce(NEW.status,''),coalesce(NEW.customer_type,'existing'),coalesce(NEW.type,''),coalesce(NEW.level,'C'),coalesce(NEW.profile,''),coalesce(NEW.field,''),coalesce(NEW.industry,''),coalesce(NEW.company_size,''),coalesce(NEW.annual_revenue,''),coalesce(NEW.tags,''),coalesce(NEW.attention_state,''),coalesce(NEW.attention_reason,''),NULLIF(NEW.last_contact,'')::timestamptz,NULLIF(NEW.next_follow_up,'')::timestamptz,CASE WHEN coalesce(NEW.is_deleted,0)=1 THEN now() ELSE NULL END,coalesce(to_jsonb(NEW),'{}'::jsonb),now())
    ON CONFLICT (organization_id,company_id) DO UPDATE SET display_name=excluded.display_name,account_status=excluded.account_status,customer_type=excluded.customer_type,channel_type=excluded.channel_type,priority_level=excluded.priority_level,profile=excluded.profile,field=excluded.field,industry=excluded.industry,company_size=excluded.company_size,annual_revenue=excluded.annual_revenue,tags=excluded.tags,attention_state=excluded.attention_state,attention_reason=excluded.attention_reason,last_contact_at=excluded.last_contact_at,next_follow_up_at=excluded.next_follow_up_at,deleted_at=excluded.deleted_at,legacy_payload=trosa.accounts.legacy_payload || excluded.legacy_payload,updated_at=now();
    INSERT INTO trosa.account_legacy_refs(organization_id,legacy_user_id,legacy_customer_id,account_id,source_db)
    VALUES (trosa.compat_org_id(),v_legacy_user,v_legacy_customer_id,v_account_id,v_legacy_user||'.db')
    ON CONFLICT (organization_id,legacy_user_id,legacy_customer_id) DO UPDATE SET account_id=excluded.account_id;
    PERFORM trosa.compat_set_lastrowid(v_legacy_customer_id);
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trosa.compat_contacts_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_legacy_user text := trosa.compat_current_user();
    v_contact_id bigint;
    v_account_id uuid;
    v_company_id uuid;
    v_person_id uuid;
    v_method_id uuid;
    email_value text := lower(trim(coalesce(NEW.email,'')));
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM trosa.contact_legacy_refs WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=v_legacy_user AND legacy_contact_id=OLD.id;
        RETURN OLD;
    END IF;
    v_contact_id := CASE WHEN TG_OP='INSERT' AND (NEW.id IS NULL OR NEW.id=0)
        THEN (SELECT coalesce(max(cr.legacy_contact_id),0)+1 FROM trosa.contact_legacy_refs cr WHERE cr.organization_id=trosa.compat_org_id() AND cr.legacy_user_id=v_legacy_user)
        ELSE NEW.id END;
    SELECT r.account_id,c.company_id INTO v_account_id,v_company_id
      FROM trosa.account_legacy_refs r JOIN trosa.accounts c ON c.id=r.account_id
     WHERE r.organization_id=trosa.compat_org_id() AND r.legacy_user_id=v_legacy_user AND r.legacy_customer_id=NEW.customer_id LIMIT 1;
    IF v_account_id IS NULL THEN
        RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,v_legacy_user;
    END IF;
    IF NEW.id IS NOT NULL AND NEW.id<>0 THEN
        SELECT cr.person_id INTO v_person_id
          FROM trosa.contact_legacy_refs cr
         WHERE cr.organization_id=trosa.compat_org_id()
           AND cr.legacy_user_id=v_legacy_user AND cr.legacy_contact_id=NEW.id;
    END IF;
    IF v_person_id IS NULL THEN
        IF email_value<>'' THEN v_person_id := trosa.compat_uuid('person:email:'||email_value); ELSE v_person_id := trosa.compat_uuid('person:'||v_legacy_user||':'||v_contact_id::text); END IF;
    END IF;
    INSERT INTO core.people(id,organization_id,full_name,normalized_name)
    VALUES (v_person_id,trosa.compat_org_id(),coalesce(nullif(trim(NEW.name),''),'UNKNOWN'),trosa.compat_normalized_name(NEW.name))
    ON CONFLICT (id) DO UPDATE SET full_name=CASE WHEN core.people.full_name IN ('','UNKNOWN') THEN excluded.full_name ELSE core.people.full_name END,updated_at=now();
    INSERT INTO core.company_people(id,company_id,person_id,title,source)
    VALUES (trosa.compat_uuid('company-person:'||v_company_id::text||':'||v_person_id::text||':'||coalesce(NEW.title,'')),v_company_id,v_person_id,coalesce(NEW.title,''),'trosa')
    ON CONFLICT DO NOTHING;
    IF email_value<>'' THEN
        INSERT INTO core.contact_methods(id,organization_id,person_id,kind,value,normalized_value,evidence)
        VALUES (trosa.compat_uuid('email:'||email_value),trosa.compat_org_id(),v_person_id,'email',email_value,email_value,'{}'::jsonb)
        ON CONFLICT (organization_id,kind,normalized_value) DO UPDATE SET person_id=coalesce(core.contact_methods.person_id,excluded.person_id),updated_at=now();
        SELECT cm.id INTO v_method_id FROM core.contact_methods cm WHERE cm.organization_id=trosa.compat_org_id() AND cm.kind='email' AND cm.normalized_value=email_value;
    END IF;
    INSERT INTO trosa.contact_legacy_refs(organization_id,legacy_user_id,legacy_contact_id,legacy_customer_id,account_id,person_id,contact_method_id,name,title,phone,whatsapp,linkedin,preferred_channel,contact_type,is_primary,notes,legacy_payload,updated_at)
    VALUES (trosa.compat_org_id(),v_legacy_user,v_contact_id,NEW.customer_id,v_account_id,v_person_id,v_method_id,coalesce(NEW.name,''),coalesce(NEW.title,''),coalesce(NEW.phone,''),coalesce(NEW.whatsapp,''),coalesce(NEW.linkedin,''),coalesce(NEW.preferred_channel,''),coalesce(NEW.contact_type,'person'),coalesce(NEW.is_primary,0)<>0,coalesce(NEW.notes,''),coalesce(to_jsonb(NEW),'{}'::jsonb),now())
    ON CONFLICT (organization_id,legacy_user_id,legacy_contact_id) DO UPDATE SET legacy_customer_id=excluded.legacy_customer_id,account_id=excluded.account_id,person_id=excluded.person_id,contact_method_id=excluded.contact_method_id,name=excluded.name,title=excluded.title,phone=excluded.phone,whatsapp=excluded.whatsapp,linkedin=excluded.linkedin,preferred_channel=excluded.preferred_channel,contact_type=excluded.contact_type,is_primary=excluded.is_primary,notes=excluded.notes,legacy_payload=excluded.legacy_payload,updated_at=now();
    PERFORM trosa.compat_set_lastrowid(v_contact_id);
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trosa.compat_reminders_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_legacy_user text := trosa.compat_current_user(); v_legacy_id bigint; v_account_id uuid; v_target_id uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM trosa.tasks WHERE id=(SELECT lr.target_id FROM trosa.legacy_row_refs lr WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_legacy_user AND lr.table_name='reminders' AND lr.legacy_id=OLD.id);
        RETURN OLD;
    END IF;
    v_legacy_id := CASE WHEN TG_OP='INSERT' AND (NEW.id IS NULL OR NEW.id=0) THEN (SELECT coalesce(max(lr.legacy_id),0)+1 FROM trosa.legacy_row_refs lr WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_legacy_user AND lr.table_name='reminders') ELSE NEW.id END;
    SELECT ar.account_id INTO v_account_id FROM trosa.account_legacy_refs ar WHERE ar.organization_id=trosa.compat_org_id() AND ar.legacy_user_id=v_legacy_user AND ar.legacy_customer_id=NEW.customer_id;
    IF v_account_id IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,v_legacy_user; END IF;
    SELECT lr.target_id INTO v_target_id
      FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_legacy_user
       AND lr.table_name='reminders' AND lr.legacy_id=v_legacy_id;
    v_target_id := coalesce(v_target_id, trosa.compat_uuid('task:'||v_legacy_user||':'||v_legacy_id::text));
    INSERT INTO trosa.tasks(id,account_id,title,content,reason,due_at,status,task_type,source_activity_legacy_id,manual_order,completed_at,legacy_payload,updated_at)
    VALUES (v_target_id,v_account_id,coalesce(NEW.title,''),coalesce(NEW.content,''),coalesce(NEW.reason,''),NULLIF(NEW.remind_date,'')::timestamptz,CASE WHEN coalesce(NEW.is_done,0)<>0 THEN 'done' ELSE 'open' END,coalesce(NEW.reminder_type,'follow_up'),coalesce(NEW.source_activity_id::text,''),coalesce(NEW.manual_order,0),NULLIF(NEW.completed_at,'')::timestamptz,coalesce(to_jsonb(NEW),'{}'::jsonb),now())
    ON CONFLICT (id) DO UPDATE SET title=excluded.title,content=excluded.content,reason=excluded.reason,due_at=excluded.due_at,status=excluded.status,task_type=excluded.task_type,source_activity_legacy_id=excluded.source_activity_legacy_id,manual_order=excluded.manual_order,completed_at=excluded.completed_at,legacy_payload=trosa.tasks.legacy_payload||excluded.legacy_payload,updated_at=now();
    INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id) VALUES (trosa.compat_org_id(),v_legacy_user,'reminders',v_legacy_id,v_target_id) ON CONFLICT (organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id;
    PERFORM trosa.compat_set_lastrowid(v_legacy_id);
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trosa.compat_follow_up_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_legacy_user text := trosa.compat_current_user(); v_legacy_id bigint; v_account_id uuid; v_target_id uuid;
BEGIN
    IF TG_OP='DELETE' THEN DELETE FROM trosa.timeline_events WHERE id=(SELECT lr.target_id FROM trosa.legacy_row_refs lr WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_legacy_user AND lr.table_name='follow_up_logs' AND lr.legacy_id=OLD.id); RETURN OLD; END IF;
    v_legacy_id := CASE WHEN TG_OP='INSERT' AND (NEW.id IS NULL OR NEW.id=0) THEN (SELECT coalesce(max(lr.legacy_id),0)+1 FROM trosa.legacy_row_refs lr WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_legacy_user AND lr.table_name='follow_up_logs') ELSE NEW.id END;
    SELECT ar.account_id INTO v_account_id FROM trosa.account_legacy_refs ar WHERE ar.organization_id=trosa.compat_org_id() AND ar.legacy_user_id=v_legacy_user AND ar.legacy_customer_id=NEW.customer_id;
    IF v_account_id IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,v_legacy_user; END IF;
    SELECT lr.target_id INTO v_target_id
      FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_legacy_user
       AND lr.table_name='follow_up_logs' AND lr.legacy_id=v_legacy_id;
    v_target_id := coalesce(v_target_id, trosa.compat_uuid('timeline:'||v_legacy_user||':'||v_legacy_id::text));
    INSERT INTO trosa.timeline_events(id,account_id,event_type,direction,content,result,next_plan,source_module,source_reference,occurred_at,payload)
    VALUES (v_target_id,v_account_id,coalesce(NEW.activity_type,'follow_up'),coalesce(NEW.direction,'unknown'),coalesce(NEW.content,''),coalesce(NEW.result,''),coalesce(NEW.next_plan,''),'trosa',v_legacy_user||':'||v_legacy_id::text,NULLIF(NEW.follow_date,'')::timestamptz,coalesce(to_jsonb(NEW),'{}'::jsonb))
    ON CONFLICT (id) DO UPDATE SET event_type=excluded.event_type,direction=excluded.direction,content=excluded.content,result=excluded.result,next_plan=excluded.next_plan,occurred_at=excluded.occurred_at,payload=trosa.timeline_events.payload||excluded.payload;
    INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id) VALUES (trosa.compat_org_id(),v_legacy_user,'follow_up_logs',v_legacy_id,v_target_id) ON CONFLICT (organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id;
    PERFORM trosa.compat_set_lastrowid(v_legacy_id);
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trosa.compat_outreach_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_legacy_user text := trosa.compat_current_user(); v_legacy_id bigint; v_account_id uuid; v_target_id uuid;
BEGIN
    IF TG_OP='DELETE' THEN DELETE FROM trosa.outreach_messages WHERE id=(SELECT lr.target_id FROM trosa.legacy_row_refs lr WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_legacy_user AND lr.table_name='outreach_emails' AND lr.legacy_id=OLD.id); RETURN OLD; END IF;
    v_legacy_id := CASE WHEN TG_OP='INSERT' AND (NEW.id IS NULL OR NEW.id=0) THEN (SELECT coalesce(max(lr.legacy_id),0)+1 FROM trosa.legacy_row_refs lr WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_legacy_user AND lr.table_name='outreach_emails') ELSE NEW.id END;
    SELECT ar.account_id INTO v_account_id FROM trosa.account_legacy_refs ar WHERE ar.organization_id=trosa.compat_org_id() AND ar.legacy_user_id=v_legacy_user AND ar.legacy_customer_id=NEW.customer_id;
    IF v_account_id IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,v_legacy_user; END IF;
    SELECT lr.target_id INTO v_target_id
      FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_legacy_user
       AND lr.table_name='outreach_emails' AND lr.legacy_id=v_legacy_id;
    v_target_id := coalesce(v_target_id, trosa.compat_uuid('trosa-message:'||v_legacy_user||':'||v_legacy_id::text));
    INSERT INTO trosa.outreach_messages(id,account_id,subject,body,sent_at,reply_status,reply_content,reply_at,provider,provider_message_id,legacy_payload)
    VALUES (v_target_id,v_account_id,coalesce(NEW.subject,''),coalesce(NEW.content,''),NULLIF(NEW.sent_date,'')::timestamptz,coalesce(NEW.reply_status,'pending'),coalesce(NEW.reply_content,''),NULLIF(NEW.reply_date,'')::timestamptz,'legacy',coalesce(nullif(NEW.message_id,''),nullif(NEW.external_id,'')),coalesce(to_jsonb(NEW),'{}'::jsonb))
    ON CONFLICT (id) DO UPDATE SET subject=excluded.subject,body=excluded.body,sent_at=excluded.sent_at,reply_status=excluded.reply_status,reply_content=excluded.reply_content,reply_at=excluded.reply_at,provider_message_id=excluded.provider_message_id,legacy_payload=trosa.outreach_messages.legacy_payload||excluded.legacy_payload;
    INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id) VALUES (trosa.compat_org_id(),v_legacy_user,'outreach_emails',v_legacy_id,v_target_id) ON CONFLICT (organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id;
    PERFORM trosa.compat_set_lastrowid(v_legacy_id);
    RETURN NEW;
END
$$;

COMMIT;
