-- Sela may read organization-wide company ownership, but may not create a
-- second-owner prospect for a company already held by another member.
-- Human Trosa accounts remain owner-scoped and may share a canonical company.
BEGIN;

CREATE TABLE IF NOT EXISTS audit.sela_company_ownership_lookups (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    requested_by text NOT NULL,
    key_hash text NOT NULL,
    match_count integer NOT NULL DEFAULT 0,
    checked_owner_count integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_sela_company_ownership_rate_idx
    ON audit.sela_company_ownership_lookups (organization_id, requested_by, created_at DESC);

CREATE OR REPLACE FUNCTION trosa.sela_company_name_key(p_value text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT trim(regexp_replace(
        regexp_replace(
            lower(regexp_replace(coalesce(p_value,''), '[^[:alnum:]]+', ' ', 'g')),
            ' (s a s|s a de c v|s r l|s a|s l|sas|sarl|srl|sl|slu|gmbh|ag|kg|bv|nv|pty|pvt|plc|llc|ltd|limited|inc|incorporated|corp|corporation|company|co|ltda|fze|fzco|fzc|wll|oy|ab|aps|cv)+$',
            '', 'g'),
        ' +', ' ', 'g'));
$$;

CREATE OR REPLACE FUNCTION trosa.sela_company_domain_key(p_value text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT trim(both '.' from regexp_replace(
        split_part(split_part(regexp_replace(lower(trim(coalesce(p_value,''))), '^https?://', ''), '/', 1), ':', 1),
        '^www\.', ''));
$$;

CREATE OR REPLACE FUNCTION trosa.sela_company_website_domain_keys(p_value text)
RETURNS text[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT coalesce(array_agg(DISTINCT domain_key ORDER BY domain_key), ARRAY[]::text[])
      FROM (
            SELECT nullif(trosa.sela_company_domain_key(website_part.value), '') AS domain_key
              FROM regexp_split_to_table(coalesce(p_value, ''), '[[:space:],;，；]+') AS website_part(value)
      ) website_keys
     WHERE domain_key IS NOT NULL;
$$;

-- Every account claim and company-identity edit takes the same advisory lock
-- set. Sorting the complete key set prevents two multi-alias/multi-domain
-- edits from taking the same locks in opposite orders.
CREATE OR REPLACE FUNCTION trosa.sela_lock_identity_keys(
    p_organization_ids uuid[], p_name_keys text[], p_domains text[])
RETURNS void
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    v_lock_key text;
BEGIN
    FOR v_lock_key IN
        SELECT DISTINCT lock_key
          FROM (
                SELECT 'sela-company-name:' || org_id::text || ':' || name_key AS lock_key
                  FROM unnest(coalesce(p_organization_ids, ARRAY[]::uuid[])) AS orgs(org_id)
                  CROSS JOIN unnest(coalesce(p_name_keys, ARRAY[]::text[])) AS names(name_key)
                 WHERE nullif(name_key, '') IS NOT NULL
                UNION ALL
                SELECT 'sela-company-domain:' || org_id::text || ':' || domain_key AS lock_key
                  FROM unnest(coalesce(p_organization_ids, ARRAY[]::uuid[])) AS orgs(org_id)
                  CROSS JOIN unnest(coalesce(p_domains, ARRAY[]::text[])) AS domains(domain_key)
                 WHERE nullif(domain_key, '') IS NOT NULL
          ) identity_lock_keys
         ORDER BY lock_key
    LOOP
        PERFORM pg_advisory_xact_lock(hashtextextended(v_lock_key, 0));
    END LOOP;
END
$$;

CREATE OR REPLACE FUNCTION trosa.sela_company_name_identity_lock()
RETURNS trigger
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    v_org_ids uuid[] := ARRAY[]::uuid[];
    v_name_keys text[] := ARRAY[]::text[];
    v_domains text[] := ARRAY[]::text[];
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- New companies have no account claim yet. The account trigger takes
        -- one complete lock set after all aliases and domains are registered.
        RETURN NEW;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM trosa.accounts account WHERE account.company_id=OLD.id) THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
    IF TG_OP <> 'INSERT' THEN
        v_org_ids := array_append(v_org_ids, OLD.organization_id);
        v_name_keys := array_append(v_name_keys, trosa.sela_company_name_key(OLD.canonical_name));
        v_name_keys := array_append(v_name_keys, trosa.sela_company_name_key(OLD.normalized_name));
        v_domains := v_domains || trosa.sela_company_website_domain_keys(OLD.website);
    END IF;
    IF TG_OP <> 'DELETE' THEN
        v_org_ids := array_append(v_org_ids, NEW.organization_id);
        v_name_keys := array_append(v_name_keys, trosa.sela_company_name_key(NEW.canonical_name));
        v_name_keys := array_append(v_name_keys, trosa.sela_company_name_key(NEW.normalized_name));
        v_domains := v_domains || trosa.sela_company_website_domain_keys(NEW.website);
    END IF;
    PERFORM trosa.sela_lock_identity_keys(v_org_ids, v_name_keys, v_domains);
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trosa.sela_company_alias_identity_lock()
RETURNS trigger
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    v_company_ids uuid[] := ARRAY[]::uuid[];
    v_org_ids uuid[] := ARRAY[]::uuid[];
    v_name_keys text[] := ARRAY[]::text[];
BEGIN
    IF TG_OP <> 'INSERT' THEN
        v_company_ids := array_append(v_company_ids, OLD.company_id);
        v_name_keys := array_append(v_name_keys, trosa.sela_company_name_key(OLD.alias));
        v_name_keys := array_append(v_name_keys, trosa.sela_company_name_key(OLD.normalized_alias));
    END IF;
    IF TG_OP <> 'DELETE' THEN
        v_company_ids := array_append(v_company_ids, NEW.company_id);
        v_name_keys := array_append(v_name_keys, trosa.sela_company_name_key(NEW.alias));
        v_name_keys := array_append(v_name_keys, trosa.sela_company_name_key(NEW.normalized_alias));
    END IF;
    SELECT coalesce(array_agg(DISTINCT company.organization_id ORDER BY company.organization_id), ARRAY[]::uuid[])
      INTO v_org_ids
      FROM core.companies company
     WHERE company.id = ANY(v_company_ids)
       AND EXISTS (SELECT 1 FROM trosa.accounts account WHERE account.company_id=company.id);
    IF cardinality(v_org_ids)=0 THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
    PERFORM trosa.sela_lock_identity_keys(v_org_ids, v_name_keys, ARRAY[]::text[]);
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trosa.sela_company_domain_identity_lock()
RETURNS trigger
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    v_company_ids uuid[] := ARRAY[]::uuid[];
    v_org_ids uuid[] := ARRAY[]::uuid[];
    v_domains text[] := ARRAY[]::text[];
BEGIN
    IF TG_OP <> 'INSERT' THEN
        v_company_ids := array_append(v_company_ids, OLD.company_id);
        v_domains := array_append(v_domains, trosa.sela_company_domain_key(OLD.normalized_domain));
    END IF;
    IF TG_OP <> 'DELETE' THEN
        v_company_ids := array_append(v_company_ids, NEW.company_id);
        v_domains := array_append(v_domains, trosa.sela_company_domain_key(NEW.normalized_domain));
    END IF;
    SELECT coalesce(array_agg(DISTINCT company.organization_id ORDER BY company.organization_id), ARRAY[]::uuid[])
      INTO v_org_ids
      FROM core.companies company
     WHERE company.id = ANY(v_company_ids)
       AND EXISTS (SELECT 1 FROM trosa.accounts account WHERE account.company_id=company.id);
    IF cardinality(v_org_ids)=0 THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
    PERFORM trosa.sela_lock_identity_keys(v_org_ids, ARRAY[]::text[], v_domains);
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION trosa.sela_cross_owner_account_guard()
RETURNS trigger
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    candidate_name_keys text[] := ARRAY[]::text[];
    candidate_domains text[] := ARRAY[]::text[];
    v_conflict boolean := false;
    v_is_sela boolean;
BEGIN
    v_is_sela := coalesce(NEW.legacy_payload->>'import_source','')='sela-v2'
             AND coalesce(NEW.legacy_payload->>'external_source','')='sela';

    SELECT coalesce(array_agg(identity.name_key ORDER BY identity.name_key), ARRAY[]::text[])
      INTO candidate_name_keys
      FROM (
            SELECT DISTINCT nullif(trosa.sela_company_name_key(company.canonical_name), '') AS name_key
              FROM core.companies company
             WHERE company.id=NEW.company_id
               AND company.organization_id=NEW.organization_id
            UNION
            SELECT DISTINCT nullif(trosa.sela_company_name_key(company.normalized_name), '')
              FROM core.companies company
             WHERE company.id=NEW.company_id
               AND company.organization_id=NEW.organization_id
            UNION
            SELECT DISTINCT nullif(trosa.sela_company_name_key(alias.alias), '')
              FROM core.company_aliases alias
              JOIN core.companies company ON company.id=alias.company_id
             WHERE alias.company_id=NEW.company_id
               AND company.organization_id=NEW.organization_id
            UNION
            SELECT DISTINCT nullif(trosa.sela_company_name_key(alias.normalized_alias), '')
              FROM core.company_aliases alias
              JOIN core.companies company ON company.id=alias.company_id
             WHERE alias.company_id=NEW.company_id
               AND company.organization_id=NEW.organization_id
      ) identity
     WHERE identity.name_key IS NOT NULL;

    SELECT coalesce(array_agg(identity.domain_key ORDER BY identity.domain_key), ARRAY[]::text[])
      INTO candidate_domains
      FROM (
            SELECT website_key.domain_key
              FROM core.companies company
              CROSS JOIN unnest(trosa.sela_company_website_domain_keys(company.website)) AS website_key(domain_key)
             WHERE company.id=NEW.company_id
               AND company.organization_id=NEW.organization_id
            UNION
            SELECT DISTINCT nullif(trosa.sela_company_domain_key(domain.normalized_domain), '')
              FROM core.company_domains domain
             WHERE domain.company_id=NEW.company_id
      ) identity
     WHERE identity.domain_key IS NOT NULL;

    PERFORM trosa.sela_lock_identity_keys(
        ARRAY[NEW.organization_id], candidate_name_keys, candidate_domains);

    IF NOT v_is_sela THEN
        RETURN NEW;
    END IF;
    IF cardinality(candidate_name_keys)=0 AND cardinality(candidate_domains)=0 THEN
        RAISE EXCEPTION 'CROSS_OWNER_COMPANY: Sela company identity is incomplete'
            USING ERRCODE='23514', DETAIL='CROSS_OWNER_COMPANY';
    END IF;

    SELECT EXISTS (
        SELECT 1
          FROM trosa.accounts other_account
          JOIN core.companies other_company
            ON other_company.id=other_account.company_id
           AND other_company.organization_id=other_account.organization_id
         WHERE other_account.organization_id=NEW.organization_id
           AND other_account.owner_user_id IS DISTINCT FROM NEW.owner_user_id
           AND (
                other_account.company_id=NEW.company_id
                OR EXISTS (
                    SELECT 1
                      FROM unnest(candidate_name_keys) candidate_key(name_key)
                     WHERE candidate_key.name_key = trosa.sela_company_name_key(other_company.canonical_name)
                        OR candidate_key.name_key = trosa.sela_company_name_key(other_company.normalized_name)
                        OR EXISTS (
                            SELECT 1
                              FROM core.company_aliases other_alias
                             WHERE other_alias.company_id=other_company.id
                               AND (candidate_key.name_key=trosa.sela_company_name_key(other_alias.alias)
                                 OR candidate_key.name_key=trosa.sela_company_name_key(other_alias.normalized_alias))
                        )
                )
                OR EXISTS (
                    SELECT 1
                      FROM core.company_domains other_domain
                     WHERE other_domain.company_id=other_company.id
                       AND trosa.sela_company_domain_key(other_domain.normalized_domain) = ANY(candidate_domains)
                )
                OR trosa.sela_company_website_domain_keys(other_company.website) && candidate_domains
           )
    ) INTO v_conflict;

    IF v_conflict THEN
        RAISE EXCEPTION 'CROSS_OWNER_COMPANY: Sela cannot create a second-owner account'
            USING ERRCODE='23514', DETAIL='CROSS_OWNER_COMPANY';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trosa_sela_cross_owner_account_guard ON trosa.accounts;
CREATE TRIGGER trosa_sela_cross_owner_account_guard
BEFORE INSERT OR UPDATE OF organization_id, company_id, owner_user_id, legacy_payload
ON trosa.accounts
FOR EACH ROW EXECUTE FUNCTION trosa.sela_cross_owner_account_guard();

DROP TRIGGER IF EXISTS trosa_sela_company_name_identity_lock ON core.companies;
CREATE TRIGGER trosa_sela_company_name_identity_lock
BEFORE INSERT OR UPDATE OF organization_id, canonical_name, normalized_name, website OR DELETE
ON core.companies
FOR EACH ROW EXECUTE FUNCTION trosa.sela_company_name_identity_lock();

DROP TRIGGER IF EXISTS trosa_sela_company_alias_identity_lock ON core.company_aliases;
CREATE TRIGGER trosa_sela_company_alias_identity_lock
BEFORE INSERT OR UPDATE OR DELETE
ON core.company_aliases
FOR EACH ROW EXECUTE FUNCTION trosa.sela_company_alias_identity_lock();

DROP TRIGGER IF EXISTS trosa_sela_company_domain_identity_lock ON core.company_domains;
CREATE TRIGGER trosa_sela_company_domain_identity_lock
BEFORE INSERT OR UPDATE OR DELETE
ON core.company_domains
FOR EACH ROW EXECUTE FUNCTION trosa.sela_company_domain_identity_lock();

COMMIT;
