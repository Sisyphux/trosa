-- Forward-only hardening: compatibility customer writes must obey the same
-- identity matching boundary as the unified importer.
--
-- A single exact domain is a safe automatic match.  A missing domain, or a
-- domain that resolves to multiple companies, is not sufficient evidence to
-- merge companies.  Those rows receive a source-scoped candidate company and
-- remain reviewable through core.companies.identity_status.
BEGIN;

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
    v_domain_match_count bigint := 0;
    v_domain_match_company uuid;
    v_match_review boolean := false;
    v_company_seed text;
    v_domain_seed text;
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

    -- Once a legacy customer has been linked, that link remains the source
    -- of truth for edits.  Do not move it by re-matching a changed website
    -- or name.  A newly supplied domain is still checked below so a
    -- conflicting domain can be kept as review-only evidence.
    SELECT ar.account_id,a.company_id INTO v_account_id,v_company_id
      FROM trosa.account_legacy_refs ar
      JOIN trosa.accounts a ON a.id=ar.account_id
     WHERE ar.organization_id=trosa.compat_org_id()
       AND ar.legacy_user_id=v_legacy_user
       AND ar.legacy_customer_id=v_legacy_customer_id
     LIMIT 1;

    IF v_domain <> '' THEN
        SELECT count(*) INTO v_domain_match_count
          FROM core.company_domains d
          JOIN core.companies c ON c.id=d.company_id
         WHERE c.organization_id=trosa.compat_org_id()
           AND d.normalized_domain=v_domain;
        IF v_domain_match_count = 1 THEN
            SELECT d.company_id INTO v_domain_match_company
              FROM core.company_domains d
              JOIN core.companies c ON c.id=d.company_id
             WHERE c.organization_id=trosa.compat_org_id()
               AND d.normalized_domain=v_domain
             LIMIT 1;
        END IF;
        IF v_company_id IS NULL THEN
            IF v_domain_match_count = 1 THEN
                v_company_id := v_domain_match_company;
            ELSE
                v_match_review := v_domain_match_count > 1;
            END IF;
        ELSIF v_domain_match_count > 1
           OR (v_domain_match_count = 1 AND v_domain_match_company IS DISTINCT FROM v_company_id) THEN
            -- Keep the existing account link, but do not attach a foreign
            -- or ambiguous domain as a verified primary identity.
            v_match_review := true;
        END IF;
    ELSIF v_company_id IS NULL THEN
        -- Name + country is a candidate signal, never an automatic merge key.
        v_match_review := true;
    END IF;

    IF v_company_id IS NULL THEN
        IF v_domain <> '' AND v_domain_match_count = 0 THEN
            v_company_seed := 'company:domain:'||v_domain;
        ELSE
            v_company_seed := 'company-candidate:'||v_legacy_user||':'||v_legacy_customer_id::text;
        END IF;
        v_company_id := trosa.compat_uuid(v_company_seed);
        INSERT INTO core.companies
          (id,organization_id,canonical_name,normalized_name,website,country_code,identity_status)
        VALUES
          (v_company_id,trosa.compat_org_id(),v_company_name,v_normalized_name,v_website,v_country,
           CASE WHEN v_match_review THEN 'review' ELSE 'confirmed' END)
        ON CONFLICT(id) DO UPDATE SET canonical_name=excluded.canonical_name,
          normalized_name=excluded.normalized_name,website=excluded.website,
          country_code=excluded.country_code,
          identity_status=CASE WHEN v_match_review THEN 'review' ELSE core.companies.identity_status END,
          updated_at=now();
    ELSE
        UPDATE core.companies SET canonical_name=v_company_name,website=v_website,
          country_code=v_country,updated_at=now()
         WHERE id=v_company_id AND (TG_OP='INSERT' OR coalesce(NEW.company,'')<>'');
        IF v_match_review THEN
            UPDATE core.companies SET identity_status='review',updated_at=now()
             WHERE id=v_company_id;
        END IF;
    END IF;

    IF v_domain<>'' THEN
        v_domain_seed := CASE WHEN v_match_review
            THEN 'company-domain-candidate:'||v_legacy_user||':'||v_legacy_customer_id::text||':'||v_domain
            ELSE 'company-domain:'||v_domain END;
        INSERT INTO core.company_domains
          (id,company_id,normalized_domain,source_url,is_primary,verification_status)
        VALUES
          (trosa.compat_uuid(v_domain_seed),v_company_id,v_domain,v_website,
           NOT v_match_review,CASE WHEN v_match_review THEN 'review' ELSE 'imported' END)
        ON CONFLICT(company_id,normalized_domain) DO UPDATE SET source_url=excluded.source_url,
          is_primary=CASE WHEN v_match_review THEN core.company_domains.is_primary ELSE excluded.is_primary END,
          verification_status=CASE WHEN v_match_review THEN 'review' ELSE excluded.verification_status END;
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

-- Existing imports may already contain a cross-user company merge that was
-- accepted by the earlier name/country heuristic.  Without an exact domain
-- there is not enough evidence to keep that merge in the confirmed set.  Do
-- not split or delete the rows automatically; mark only these recoverable
-- cases for human review so a later write cannot treat them as verified.
UPDATE core.companies c
   SET identity_status='review', updated_at=now()
 WHERE c.identity_status='confirmed'
   AND c.id IN (
       SELECT a.company_id
         FROM trosa.accounts a
         JOIN trosa.account_legacy_refs r ON r.account_id=a.id
        GROUP BY a.company_id
       HAVING count(DISTINCT r.legacy_user_id)>1
   )
   AND NOT EXISTS (
       SELECT 1 FROM core.company_domains d
        WHERE d.company_id=c.id AND btrim(d.normalized_domain)<>''
   );

COMMIT;
