-- One active owner per Customer account.
--
-- The canonical company remains shareable, but an active Customer account may
-- belong to exactly one user.  Historical imports linked several legacy users
-- to one canonical account (the same company matched by domain/name), which is
-- the double-active ownership drift.  This migration:
--
--   1. drops the "shared account" freeze triggers that only existed to paper
--      over multi-owner accounts;
--   2. splits every multi-user account into one account per user, keeping the
--      shared company and moving each user's references, contacts, states,
--      tasks, timeline facts and outreach to that user's own account;
--   3. makes ``owner_user_id`` mandatory and the single source of ownership;
--   4. allows one account per (company, owner) instead of one per company;
--   5. rejects any reference whose user is not the account owner;
--   6. makes the compatibility customer writer resolve accounts per owner;
--   7. defines explicit transfer instead of implicit shared ownership.
--
-- It only splits accounts that already violate the invariant; a compliant
-- database is left untouched.
BEGIN;

-- 1. These triggers froze shared accounts/companies and encoded the very
--    multi-owner state this migration removes.
DROP TRIGGER IF EXISTS compat_keep_shared_account_canonical ON trosa.accounts;
DROP TRIGGER IF EXISTS compat_keep_shared_company_canonical ON core.companies;

-- The old uniqueness allowed only one account per company, so a second user
-- could never get their own account for the same shared company.
ALTER TABLE trosa.accounts
    DROP CONSTRAINT IF EXISTS accounts_organization_id_company_id_key;

-- ``(organization_id, source_db, legacy_customer_id)`` assumed the source
-- database identifies the owner.  Imported databases do, but runtime customers
-- share ``source_db='modern-trosa'`` while their integer ids are per user, so
-- two users could not both hold customer #1.  The primary key
-- ``(organization_id, legacy_user_id, legacy_customer_id)`` is the correct
-- ownership boundary.
ALTER TABLE trosa.account_legacy_refs
    DROP CONSTRAINT IF EXISTS account_legacy_refs_organization_id_source_db_legacy_custom_key;

-- Transfer re-keys the projection and the state rows together, so the composite
-- foreign key must be deferrable.
ALTER TABLE trosa.customer_states
    DROP CONSTRAINT IF EXISTS customer_states_organization_id_legacy_user_id_legacy_cust_fkey;
ALTER TABLE trosa.customer_states
    ADD CONSTRAINT customer_states_organization_id_legacy_user_id_legacy_cust_fkey
    FOREIGN KEY (organization_id, legacy_user_id, legacy_customer_id)
    REFERENCES trosa.account_legacy_refs(organization_id, legacy_user_id, legacy_customer_id)
    ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE;

-- 2. Split multi-user accounts.  Exposed as a function so the same heal can be
--    re-run and regression-tested against seeded drift.
CREATE OR REPLACE FUNCTION trosa.enforce_single_account_owner()
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    a record;
    u record;
    v_owner_legacy text;
    v_owner_uid uuid;
    v_new_account uuid;
    v_split int := 0;
BEGIN
    FOR a IN
        SELECT account.id, account.organization_id, account.company_id, account.owner_user_id
          FROM trosa.accounts account
         WHERE (SELECT count(DISTINCT r.legacy_user_id)
                  FROM trosa.account_legacy_refs r
                 WHERE r.organization_id=account.organization_id
                   AND r.account_id=account.id) > 1
         ORDER BY account.id
    LOOP
        -- Keep the recorded owner when it really references this account,
        -- otherwise fall back to the earliest reference deterministically.
        SELECT usr.legacy_user_id INTO v_owner_legacy
          FROM identity.users usr
         WHERE usr.organization_id=a.organization_id
           AND usr.id=a.owner_user_id
           AND EXISTS (SELECT 1 FROM trosa.account_legacy_refs r
                        WHERE r.organization_id=a.organization_id AND r.account_id=a.id
                          AND r.legacy_user_id=usr.legacy_user_id)
         LIMIT 1;
        IF v_owner_legacy IS NULL THEN
            SELECT r.legacy_user_id INTO v_owner_legacy
              FROM trosa.account_legacy_refs r
             WHERE r.organization_id=a.organization_id AND r.account_id=a.id
             ORDER BY r.created_at, r.legacy_user_id
             LIMIT 1;
        END IF;
        SELECT usr.id INTO v_owner_uid
          FROM identity.users usr
         WHERE usr.organization_id=a.organization_id
           AND usr.legacy_user_id=v_owner_legacy;
        UPDATE trosa.accounts SET owner_user_id=v_owner_uid WHERE id=a.id;

        FOR u IN
            SELECT DISTINCT r.legacy_user_id
              FROM trosa.account_legacy_refs r
             WHERE r.organization_id=a.organization_id AND r.account_id=a.id
               AND r.legacy_user_id<>v_owner_legacy
             ORDER BY r.legacy_user_id
        LOOP
            v_new_account := trosa.compat_uuid('owner-split:'||a.id::text||':'||u.legacy_user_id);
            INSERT INTO trosa.accounts
                (id, organization_id, company_id, owner_user_id, display_name, account_status,
                 customer_type, channel_type, priority_level, profile, field, industry, company_size,
                 annual_revenue, tags, attention_state, attention_reason, attention_updated_at,
                 attention_review_date, last_contact_at, next_follow_up_at, is_pinned, pinned_order,
                 deleted_at, legacy_payload, created_at, updated_at)
            SELECT v_new_account, src.organization_id, src.company_id, usr.id, src.display_name,
                   src.account_status, src.customer_type, src.channel_type, src.priority_level,
                   src.profile, src.field, src.industry, src.company_size, src.annual_revenue,
                   src.tags, src.attention_state, src.attention_reason, src.attention_updated_at,
                   src.attention_review_date, src.last_contact_at, src.next_follow_up_at,
                   src.is_pinned, src.pinned_order, src.deleted_at, src.legacy_payload,
                   src.created_at, now()
              FROM trosa.accounts src
              JOIN identity.users usr ON usr.organization_id=src.organization_id
               AND usr.legacy_user_id=u.legacy_user_id
             WHERE src.id=a.id;

            UPDATE trosa.account_legacy_refs SET account_id=v_new_account
             WHERE organization_id=a.organization_id AND account_id=a.id
               AND legacy_user_id=u.legacy_user_id;
            UPDATE trosa.contact_legacy_refs SET account_id=v_new_account
             WHERE organization_id=a.organization_id AND account_id=a.id
               AND legacy_user_id=u.legacy_user_id;
            UPDATE trosa.customer_states SET account_id=v_new_account
             WHERE organization_id=a.organization_id AND account_id=a.id
               AND legacy_user_id=u.legacy_user_id;
            UPDATE trosa.email_message_receipts SET account_id=v_new_account
             WHERE organization_id=a.organization_id AND account_id=a.id
               AND legacy_user_id=u.legacy_user_id;

            UPDATE trosa.tasks t SET account_id=v_new_account
             WHERE t.account_id=a.id
               AND EXISTS (SELECT 1 FROM trosa.legacy_row_refs lr
                            WHERE lr.organization_id=a.organization_id
                              AND lr.legacy_user_id=u.legacy_user_id
                              AND lr.table_name='reminders' AND lr.target_id=t.id)
               AND NOT EXISTS (SELECT 1 FROM trosa.legacy_row_refs own
                                WHERE own.organization_id=a.organization_id
                                  AND own.legacy_user_id=v_owner_legacy
                                  AND own.table_name='reminders' AND own.target_id=t.id);
            UPDATE trosa.timeline_events e SET account_id=v_new_account
             WHERE e.account_id=a.id
               AND EXISTS (SELECT 1 FROM trosa.legacy_row_refs lr
                            WHERE lr.organization_id=a.organization_id
                              AND lr.legacy_user_id=u.legacy_user_id
                              AND lr.table_name='follow_up_logs' AND lr.target_id=e.id)
               AND NOT EXISTS (SELECT 1 FROM trosa.legacy_row_refs own
                                WHERE own.organization_id=a.organization_id
                                  AND own.legacy_user_id=v_owner_legacy
                                  AND own.table_name='follow_up_logs' AND own.target_id=e.id);
            UPDATE trosa.outreach_messages m SET account_id=v_new_account
             WHERE m.account_id=a.id
               AND EXISTS (SELECT 1 FROM trosa.legacy_row_refs lr
                            WHERE lr.organization_id=a.organization_id
                              AND lr.legacy_user_id=u.legacy_user_id
                              AND lr.table_name='outreach_emails' AND lr.target_id=m.id)
               AND NOT EXISTS (SELECT 1 FROM trosa.legacy_row_refs own
                                WHERE own.organization_id=a.organization_id
                                  AND own.legacy_user_id=v_owner_legacy
                                  AND own.table_name='outreach_emails' AND own.target_id=m.id);
            UPDATE trosa.inbox_items i SET account_id=v_new_account
             WHERE i.account_id=a.id
               AND EXISTS (SELECT 1 FROM trosa.legacy_row_refs lr
                            WHERE lr.organization_id=a.organization_id
                              AND lr.legacy_user_id=u.legacy_user_id
                              AND lr.table_name='inbox_items' AND lr.target_id=i.id)
               AND NOT EXISTS (SELECT 1 FROM trosa.legacy_row_refs own
                                WHERE own.organization_id=a.organization_id
                                  AND own.legacy_user_id=v_owner_legacy
                                  AND own.table_name='inbox_items' AND own.target_id=i.id);

            -- customer_details is account-scoped: give the split account its own
            -- copy rather than sharing one row.
            INSERT INTO trosa.customer_details
                (account_id, notes, system_notes, import_source, external_source, external_id,
                 pinned_at, manual_next_task, created_at, updated_at)
            SELECT v_new_account, notes, system_notes, import_source, external_source, external_id,
                   pinned_at, manual_next_task, created_at, now()
              FROM trosa.customer_details WHERE account_id=a.id
            ON CONFLICT (account_id) DO NOTHING;
        END LOOP;
        v_split := v_split + 1;
    END LOOP;
    RETURN v_split;
END
$$;

SELECT trosa.enforce_single_account_owner();

-- 3. Backfill any remaining owner from the account's own reference, then from
--    the organisation as a last resort so the column can be made mandatory.
UPDATE trosa.accounts account
   SET owner_user_id = (
        SELECT usr.id
          FROM trosa.account_legacy_refs r
          JOIN identity.users usr ON usr.organization_id=r.organization_id
           AND usr.legacy_user_id=r.legacy_user_id
         WHERE r.organization_id=account.organization_id AND r.account_id=account.id
         ORDER BY r.created_at, r.legacy_user_id
         LIMIT 1)
 WHERE account.owner_user_id IS NULL;

UPDATE trosa.accounts account
   SET owner_user_id = (
        SELECT usr.id FROM identity.users usr
         WHERE usr.organization_id=account.organization_id
         ORDER BY usr.created_at, usr.id
         LIMIT 1)
 WHERE account.owner_user_id IS NULL;

-- 4. One account per company per owner; different owners may share a company.
ALTER TABLE trosa.accounts
    ADD CONSTRAINT accounts_org_company_owner_key
    UNIQUE (organization_id, company_id, owner_user_id);

ALTER TABLE trosa.accounts ALTER COLUMN owner_user_id SET NOT NULL;

-- 5. Every legacy reference must belong to the account owner.  This is the
--    database-level single-active-owner invariant.
CREATE OR REPLACE FUNCTION trosa.compat_account_owner_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_owner_legacy text;
BEGIN
    SELECT usr.legacy_user_id INTO v_owner_legacy
      FROM trosa.accounts account
      JOIN identity.users usr ON usr.id=account.owner_user_id
     WHERE account.id=NEW.account_id;
    IF v_owner_legacy IS NULL THEN
        RAISE EXCEPTION 'account % has no owner', NEW.account_id;
    END IF;
    IF v_owner_legacy <> NEW.legacy_user_id THEN
        RAISE EXCEPTION 'account % is owned by %, not %',
            NEW.account_id, v_owner_legacy, NEW.legacy_user_id;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trosa_account_legacy_refs_owner_guard ON trosa.account_legacy_refs;
CREATE TRIGGER trosa_account_legacy_refs_owner_guard
BEFORE INSERT OR UPDATE ON trosa.account_legacy_refs
FOR EACH ROW EXECUTE FUNCTION trosa.compat_account_owner_guard();

-- 6. Compatibility customer writer resolves the account for (company, owner)
--    and records the owner instead of merging into another user's account.
CREATE OR REPLACE FUNCTION trosa.compat_customers_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_legacy_user text := trosa.compat_current_user();
    v_owner_user_id uuid;
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
    SELECT usr.id INTO v_owner_user_id FROM identity.users usr
     WHERE usr.organization_id=trosa.compat_org_id() AND usr.legacy_user_id=v_legacy_user;
    IF v_owner_user_id IS NULL THEN
        RAISE EXCEPTION 'legacy user % is not registered', v_legacy_user;
    END IF;

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
            v_match_review := true;
        END IF;
    ELSIF v_company_id IS NULL THEN
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

    -- Reuse only this owner's account for the shared company.  A different
    -- user's account for the same company stays separate.
    IF v_account_id IS NULL THEN
        SELECT a.id INTO v_account_id FROM trosa.accounts a
         WHERE a.organization_id=trosa.compat_org_id() AND a.company_id=v_company_id
           AND a.owner_user_id=v_owner_user_id
         LIMIT 1;
    END IF;
    v_account_id := coalesce(
        v_account_id,
        trosa.compat_uuid('account:'||v_company_id::text||':'||v_legacy_user));
    INSERT INTO trosa.accounts
      (id,organization_id,company_id,owner_user_id,display_name,account_status,customer_type,channel_type,priority_level,
       profile,field,industry,company_size,annual_revenue,tags,attention_state,attention_reason,
       last_contact_at,next_follow_up_at,deleted_at,legacy_payload,updated_at)
    VALUES
      (v_account_id,trosa.compat_org_id(),v_company_id,v_owner_user_id,coalesce(NEW.name,''),coalesce(NEW.status,''),
       coalesce(NEW.customer_type,'existing'),coalesce(NEW.type,''),coalesce(NEW.level,'C'),
       coalesce(NEW.profile,''),coalesce(NEW.field,''),coalesce(NEW.industry,''),coalesce(NEW.company_size,''),
       coalesce(NEW.annual_revenue,''),coalesce(NEW.tags,''),coalesce(NEW.attention_state,''),
       coalesce(NEW.attention_reason,''),trosa.compat_time(NEW.last_contact),
       trosa.compat_time(NEW.next_follow_up),CASE WHEN coalesce(NEW.is_deleted,0)=1 THEN now() ELSE NULL END,
       coalesce(to_jsonb(NEW),'{}'::jsonb),now())
    ON CONFLICT(organization_id,company_id,owner_user_id) DO UPDATE SET display_name=excluded.display_name,
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

-- 7. Explicit transfer: move all of one owner's customer namespace, facts and
--    history to another user.  It refuses when the target already owns a
--    customer for the same company so the single-owner invariant is preserved.
CREATE OR REPLACE FUNCTION trosa.transfer_customer_account(p_customer_id bigint, p_to_user text)
RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    v_from_user text := trosa.compat_current_user();
    v_from_uid uuid;
    v_to_uid uuid;
    v_account uuid;
    v_company uuid;
    v_owner uuid;
    v_new_customer bigint;
    v_new_contact bigint;
    v_new_row bigint;
    v_ref_count int;
    r record;
BEGIN
    IF p_to_user IS NULL OR p_to_user = v_from_user THEN
        RAISE EXCEPTION 'target user must differ from the current owner';
    END IF;
    SELECT usr.id INTO v_from_uid FROM identity.users usr
     WHERE usr.organization_id=trosa.compat_org_id() AND usr.legacy_user_id=v_from_user;
    SELECT usr.id INTO v_to_uid FROM identity.users usr
     WHERE usr.organization_id=trosa.compat_org_id() AND usr.legacy_user_id=p_to_user;
    IF v_to_uid IS NULL THEN
        RAISE EXCEPTION 'target user % does not exist', p_to_user;
    END IF;
    SELECT ar.account_id,a.company_id,a.owner_user_id
      INTO v_account,v_company,v_owner
      FROM trosa.account_legacy_refs ar
      JOIN trosa.accounts a ON a.id=ar.account_id
     WHERE ar.organization_id=trosa.compat_org_id()
       AND ar.legacy_user_id=v_from_user
       AND ar.legacy_customer_id=p_customer_id;
    IF v_account IS NULL THEN
        RAISE EXCEPTION 'customer % is not visible to user %', p_customer_id, v_from_user;
    END IF;
    IF v_owner IS DISTINCT FROM v_from_uid THEN
        RAISE EXCEPTION 'user % does not own customer %', v_from_user, p_customer_id;
    END IF;
    SELECT count(*) INTO v_ref_count FROM trosa.account_legacy_refs ar
     WHERE ar.organization_id=trosa.compat_org_id() AND ar.account_id=v_account
       AND ar.legacy_user_id=v_from_user;
    IF v_ref_count <> 1 THEN
        RAISE EXCEPTION 'customer % has % merged aliases; resolve them before transfer',
            p_customer_id, v_ref_count;
    END IF;
    IF EXISTS (SELECT 1 FROM trosa.accounts a2
                WHERE a2.organization_id=trosa.compat_org_id() AND a2.company_id=v_company
                  AND a2.owner_user_id=v_to_uid AND a2.id<>v_account) THEN
        RAISE EXCEPTION 'target user % already owns a customer for this company', p_to_user;
    END IF;

    -- Ownership first so the reference re-keys pass the owner guard.
    UPDATE trosa.accounts SET owner_user_id=v_to_uid, updated_at=now() WHERE id=v_account;

    -- The projection and its state row move together.
    EXECUTE 'SET CONSTRAINTS customer_states_organization_id_legacy_user_id_legacy_cust_fkey DEFERRED';

    v_new_customer := trosa.compat_next_id('customers', p_to_user);
    -- Move the projection first: customer_states has a composite foreign key
    -- back to (organization_id, legacy_user_id, legacy_customer_id).
    UPDATE trosa.account_legacy_refs
       SET legacy_user_id=p_to_user, legacy_customer_id=v_new_customer,
           source_db=coalesce(source_db,'')||'+transfer'
     WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=v_from_user
       AND legacy_customer_id=p_customer_id;
    UPDATE trosa.customer_states
       SET legacy_user_id=p_to_user, legacy_customer_id=v_new_customer
     WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=v_from_user
       AND legacy_customer_id=p_customer_id;

    FOR r IN SELECT cr.legacy_contact_id FROM trosa.contact_legacy_refs cr
              WHERE cr.organization_id=trosa.compat_org_id() AND cr.legacy_user_id=v_from_user
                AND cr.legacy_customer_id=p_customer_id
              ORDER BY cr.legacy_contact_id
    LOOP
        v_new_contact := trosa.compat_next_id('contacts', p_to_user);
        UPDATE trosa.contact_legacy_refs
           SET legacy_user_id=p_to_user, legacy_contact_id=v_new_contact, legacy_customer_id=v_new_customer
         WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=v_from_user
           AND legacy_contact_id=r.legacy_contact_id;
    END LOOP;

    FOR r IN
        SELECT lr.table_name, lr.legacy_id FROM trosa.legacy_row_refs lr
         WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_from_user
           AND lr.target_id IN (
               SELECT id FROM trosa.tasks WHERE account_id=v_account
               UNION ALL SELECT id FROM trosa.timeline_events WHERE account_id=v_account
               UNION ALL SELECT id FROM trosa.outreach_messages WHERE account_id=v_account
               UNION ALL SELECT id FROM trosa.inbox_items WHERE account_id=v_account)
         ORDER BY lr.table_name, lr.legacy_id
    LOOP
        v_new_row := trosa.compat_next_id(r.table_name, p_to_user);
        UPDATE trosa.legacy_row_refs
           SET legacy_user_id=p_to_user, legacy_id=v_new_row
         WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=v_from_user
           AND table_name=r.table_name AND legacy_id=r.legacy_id;
    END LOOP;

    RETURN v_new_customer;
END
$$;

COMMIT;
