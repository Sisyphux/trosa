-- Keep legacy customer fields user-scoped even when an earlier import linked
-- several legacy rows to one canonical account.  A customer edit must never
-- rewrite another user's name, company, website, or status through that
-- shared account.
--
-- This migration is deliberately forward-only.  It preserves the existing
-- canonical account and all history, while retaining each legacy customer's
-- source payload on its own compatibility reference.  A later identity
-- review can still split canonical accounts without losing those facts.

BEGIN;

ALTER TABLE trosa.account_legacy_refs
    ADD COLUMN IF NOT EXISTS legacy_payload jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS audit_legacy_records_source_key_idx
    ON audit.legacy_records (source_table, legacy_key, imported_at DESC);

-- The import archive is the authoritative pre-merge customer snapshot.  Fill
-- the new per-reference payload only when it is still empty so this migration
-- is idempotent on a partially repaired rehearsal database.
UPDATE trosa.account_legacy_refs ref
   SET legacy_payload = COALESCE(
       (
           SELECT record.payload
             FROM audit.legacy_records record
            WHERE record.source_table = 'trosa/' || ref.legacy_user_id || '.db/customers'
              AND record.legacy_key = ref.legacy_customer_id::text
            ORDER BY record.imported_at DESC, record.id DESC
            LIMIT 1
       ),
       account.legacy_payload,
       '{}'::jsonb
   )
  FROM trosa.accounts account
 WHERE account.id = ref.account_id
   AND ref.legacy_payload = '{}'::jsonb;

-- Public profile pages are evidence about a person or listing, not a unique
-- company identity.  Treating linkedin.com as a company domain was the
-- concrete cause of Aura Trading Company and KPS Global Solutions sharing a
-- canonical row.  Keep this list conservative: only well-known profile and
-- social hosts are excluded; ordinary corporate domains remain eligible for
-- exact matching.
CREATE OR REPLACE FUNCTION trosa.compat_public_profile_domain(value text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT lower(trim(coalesce(value, ''))) = ANY (ARRAY[
        'linkedin.com', 'facebook.com', 'instagram.com', 'twitter.com',
        'x.com', 'youtube.com', 'tiktok.com', 'pinterest.com',
        'whatsapp.com', 'wa.me', 't.me', 'linktr.ee', 'beacons.ai',
        'about.me', 'crunchbase.com', 'yelp.com'
    ]::text[])
    OR lower(trim(coalesce(value, ''))) LIKE ANY (ARRAY[
        '%.linkedin.com', '%.facebook.com', '%.instagram.com',
        '%.twitter.com', '%.youtube.com', '%.tiktok.com',
        '%.pinterest.com', '%.whatsapp.com', '%.linktr.ee',
        '%.crunchbase.com', '%.yelp.com'
    ]::text[])
$$;

-- Keep the existing compatibility trigger API, which calls compat_domain
-- directly, on the same safe boundary as the new importer helper.
CREATE OR REPLACE FUNCTION trosa.compat_domain(value text)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    normalized text := lower(trim(coalesce(value, '')));
BEGIN
    IF normalized = '' THEN
        RETURN '';
    END IF;
    normalized := regexp_replace(normalized, '^https?://', '');
    normalized := split_part(normalized, '/', 1);
    IF position('@' in normalized) > 0 THEN
        normalized := split_part(normalized, '@', 2);
    END IF;
    normalized := split_part(normalized, ':', 1);
    normalized := regexp_replace(normalized, '^www\.', '');
    IF trosa.compat_public_profile_domain(normalized) THEN
        RETURN '';
    END IF;
    RETURN normalized;
END
$$;

CREATE OR REPLACE FUNCTION trosa.compat_matchable_domain(value text)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    normalized text := trosa.compat_domain(value);
BEGIN
    IF normalized = '' OR trosa.compat_public_profile_domain(normalized) THEN
        RETURN '';
    END IF;
    RETURN normalized;
END
$$;

-- Existing public-profile domains are evidence only.  They must not remain a
-- primary company identity or make a cross-user merge look confirmed.
UPDATE core.company_domains
   SET is_primary = false,
       verification_status = 'review'
 WHERE trosa.compat_public_profile_domain(normalized_domain);

UPDATE core.companies company
   SET identity_status = 'review',
       updated_at = now()
 WHERE EXISTS (
           SELECT 1
             FROM core.company_domains domain
            WHERE domain.company_id = company.id
              AND trosa.compat_public_profile_domain(domain.normalized_domain)
       );

-- A shared account still has one canonical row for history and compatibility,
-- but its editable customer fields are no longer allowed to change globally.
-- The user-scoped payload trigger below records the caller's values on the
-- legacy reference instead.
CREATE OR REPLACE FUNCTION trosa.compat_keep_shared_account_canonical()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM trosa.account_legacy_refs ref
         WHERE ref.organization_id = OLD.organization_id
           AND ref.account_id = OLD.id
         GROUP BY ref.organization_id, ref.account_id
        HAVING count(*) > 1
    ) THEN
        NEW.company_id := OLD.company_id;
        NEW.owner_user_id := OLD.owner_user_id;
        NEW.display_name := OLD.display_name;
        NEW.account_status := OLD.account_status;
        NEW.customer_type := OLD.customer_type;
        NEW.channel_type := OLD.channel_type;
        NEW.priority_level := OLD.priority_level;
        NEW.profile := OLD.profile;
        NEW.field := OLD.field;
        NEW.industry := OLD.industry;
        NEW.company_size := OLD.company_size;
        NEW.annual_revenue := OLD.annual_revenue;
        NEW.tags := OLD.tags;
        NEW.attention_state := OLD.attention_state;
        NEW.attention_reason := OLD.attention_reason;
        NEW.attention_updated_at := OLD.attention_updated_at;
        NEW.attention_review_date := OLD.attention_review_date;
        NEW.last_contact_at := OLD.last_contact_at;
        NEW.next_follow_up_at := OLD.next_follow_up_at;
        NEW.is_pinned := OLD.is_pinned;
        NEW.pinned_order := OLD.pinned_order;
        NEW.deleted_at := OLD.deleted_at;
        NEW.legacy_payload := OLD.legacy_payload;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS compat_keep_shared_account_canonical ON trosa.accounts;
CREATE TRIGGER compat_keep_shared_account_canonical
BEFORE UPDATE ON trosa.accounts
FOR EACH ROW EXECUTE FUNCTION trosa.compat_keep_shared_account_canonical();

CREATE OR REPLACE FUNCTION trosa.compat_keep_shared_company_canonical()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM trosa.accounts account
          JOIN trosa.account_legacy_refs ref ON ref.account_id = account.id
         WHERE account.organization_id = OLD.organization_id
           AND account.company_id = OLD.id
         GROUP BY account.company_id
        HAVING count(*) > 1
    ) THEN
        NEW.canonical_name := OLD.canonical_name;
        NEW.normalized_name := OLD.normalized_name;
        NEW.website := OLD.website;
        NEW.country_code := OLD.country_code;
        NEW.city := OLD.city;
        NEW.business_type := OLD.business_type;
        IF NEW.identity_status <> 'review' THEN
            NEW.identity_status := OLD.identity_status;
        END IF;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS compat_keep_shared_company_canonical ON core.companies;
CREATE TRIGGER compat_keep_shared_company_canonical
BEFORE UPDATE ON core.companies
FOR EACH ROW EXECUTE FUNCTION trosa.compat_keep_shared_company_canonical();

-- Project customer fields from the legacy reference first.  The account/core
-- columns remain fallbacks for rows created after this migration or rows that
-- have no matching archive record.
CREATE OR REPLACE VIEW trosa.customers AS
SELECT r.legacy_customer_id AS id,
       CASE WHEN r.legacy_payload ? 'name' THEN coalesce(r.legacy_payload->>'name', '')
            ELSE a.display_name END AS name,
       CASE WHEN r.legacy_payload ? 'company' THEN coalesce(r.legacy_payload->>'company', '')
            ELSE c.canonical_name END AS company,
       CASE WHEN r.legacy_payload ? 'country' THEN coalesce(r.legacy_payload->>'country', '')
            ELSE c.country_code END AS country,
       CASE WHEN r.legacy_payload ? 'level' THEN coalesce(r.legacy_payload->>'level', '')
            ELSE a.priority_level END AS level,
       CASE WHEN r.legacy_payload ? 'type' THEN coalesce(r.legacy_payload->>'type', '')
            ELSE a.channel_type END AS type,
       CASE WHEN r.legacy_payload ? 'website' THEN coalesce(r.legacy_payload->>'website', '')
            ELSE c.website END AS website,
       CASE WHEN r.legacy_payload ? 'profile' THEN coalesce(r.legacy_payload->>'profile', '')
            ELSE a.profile END AS profile,
       CASE WHEN r.legacy_payload ? 'field' THEN coalesce(r.legacy_payload->>'field', '')
            ELSE a.field END AS field,
       CASE WHEN r.legacy_payload ? 'status' THEN coalesce(r.legacy_payload->>'status', '')
            ELSE a.account_status END AS status,
       CASE WHEN r.legacy_payload ? 'notes' THEN coalesce(r.legacy_payload->>'notes', '')
            ELSE coalesce(a.legacy_payload->>'notes', '') END AS notes,
       CASE WHEN r.legacy_payload ? 'system_notes' THEN coalesce(r.legacy_payload->>'system_notes', '')
            ELSE coalesce(a.legacy_payload->>'system_notes', '') END AS system_notes,
       CASE WHEN r.legacy_payload ? 'last_contact' THEN coalesce(r.legacy_payload->>'last_contact', '')
            WHEN a.last_contact_at IS NOT NULL THEN trosa.compat_local_date(a.last_contact_at)
            ELSE coalesce(a.legacy_payload->>'last_contact', '') END AS last_contact,
       CASE WHEN r.legacy_payload ? 'next_follow_up' THEN coalesce(r.legacy_payload->>'next_follow_up', '')
            WHEN a.next_follow_up_at IS NOT NULL THEN trosa.compat_local_date(a.next_follow_up_at)
            ELSE coalesce(a.legacy_payload->>'next_follow_up', '') END AS next_follow_up,
       CASE WHEN lower(coalesce(
                    CASE WHEN r.legacy_payload ? 'manual_next_follow'
                         THEN r.legacy_payload->>'manual_next_follow' END,
                    a.legacy_payload->>'manual_next_follow', '0')) IN ('1', 'true')
            THEN 1 ELSE 0 END AS manual_next_follow,
       CASE WHEN r.legacy_payload ? 'customer_type' THEN coalesce(r.legacy_payload->>'customer_type', '')
            ELSE a.customer_type END AS customer_type,
       CASE WHEN r.legacy_payload ? 'industry' THEN coalesce(r.legacy_payload->>'industry', '')
            ELSE a.industry END AS industry,
       CASE WHEN r.legacy_payload ? 'company_size' THEN coalesce(r.legacy_payload->>'company_size', '')
            ELSE a.company_size END AS company_size,
       CASE WHEN r.legacy_payload ? 'annual_revenue' THEN coalesce(r.legacy_payload->>'annual_revenue', '')
            ELSE a.annual_revenue END AS annual_revenue,
       CASE WHEN r.legacy_payload ? 'tags' THEN coalesce(r.legacy_payload->>'tags', '')
            ELSE a.tags END AS tags,
       CASE WHEN r.legacy_payload ? 'import_source' THEN coalesce(r.legacy_payload->>'import_source', '')
            ELSE coalesce(a.legacy_payload->>'import_source', 'legacy') END AS import_source,
       CASE WHEN r.legacy_payload ? 'external_source' THEN coalesce(r.legacy_payload->>'external_source', '')
            ELSE coalesce(a.legacy_payload->>'external_source', '') END AS external_source,
       CASE WHEN r.legacy_payload ? 'external_id' THEN coalesce(r.legacy_payload->>'external_id', '')
            ELSE coalesce(a.legacy_payload->>'external_id', '') END AS external_id,
       CASE WHEN r.legacy_payload ? 'attention_state' THEN coalesce(r.legacy_payload->>'attention_state', '')
            ELSE a.attention_state END AS attention_state,
       CASE WHEN r.legacy_payload ? 'attention_reason' THEN coalesce(r.legacy_payload->>'attention_reason', '')
            ELSE a.attention_reason END AS attention_reason,
       CASE WHEN r.legacy_payload ? 'attention_updated_at' THEN coalesce(r.legacy_payload->>'attention_updated_at', '')
            WHEN a.attention_updated_at IS NOT NULL THEN a.attention_updated_at::text
            ELSE coalesce(a.legacy_payload->>'attention_updated_at', '') END AS attention_updated_at,
       CASE WHEN r.legacy_payload ? 'attention_review_date' THEN coalesce(r.legacy_payload->>'attention_review_date', '')
            WHEN a.attention_review_date IS NOT NULL THEN a.attention_review_date::text
            ELSE coalesce(a.legacy_payload->>'attention_review_date', '') END AS attention_review_date,
       CASE WHEN lower(coalesce(
                    CASE WHEN r.legacy_payload ? 'is_pinned'
                         THEN r.legacy_payload->>'is_pinned' END,
                    CASE WHEN a.is_pinned THEN '1' ELSE '0' END)) IN ('1', 'true')
            THEN 1 ELSE 0 END AS is_pinned,
       CASE WHEN coalesce(r.legacy_payload->>'pinned_order', '') ~ '^-?[0-9]+$'
            THEN (r.legacy_payload->>'pinned_order')::integer
            ELSE a.pinned_order END AS pinned_order,
       CASE WHEN r.legacy_payload ? 'pinned_at' THEN coalesce(r.legacy_payload->>'pinned_at', '')
            ELSE coalesce(a.legacy_payload->>'pinned_at', '') END AS pinned_at,
       CASE WHEN lower(coalesce(
                    CASE WHEN r.legacy_payload ? 'is_deleted'
                         THEN r.legacy_payload->>'is_deleted' END,
                    CASE WHEN a.deleted_at IS NULL THEN '0' ELSE '1' END)) IN ('1', 'true')
            THEN 1 ELSE 0 END AS is_deleted,
       CASE WHEN r.legacy_payload ? 'deleted_at' THEN coalesce(r.legacy_payload->>'deleted_at', '')
            ELSE coalesce(a.deleted_at::text, '') END AS deleted_at,
       a.created_at::text AS created_at,
       a.updated_at::text AS updated_at
FROM trosa.account_legacy_refs r
JOIN trosa.accounts a ON a.id = r.account_id
JOIN core.companies c ON c.id = a.company_id
WHERE r.organization_id = trosa.compat_org_id()
  AND r.legacy_user_id = trosa.compat_current_user();

-- ``compat_customers_bridge`` performs the canonical write first.  The z-
-- prefixed trigger name guarantees this payload capture runs afterwards for
-- both the legacy view and the trade_os_compat view used by the application.
CREATE OR REPLACE FUNCTION trosa.compat_customers_ref_payload_write()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_user text := trosa.compat_current_user();
    v_customer_id bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        UPDATE trosa.account_legacy_refs
           SET legacy_payload = coalesce(legacy_payload, '{}'::jsonb)
               || jsonb_build_object('is_deleted', 1, 'deleted_at', now()::text)
         WHERE organization_id = trosa.compat_org_id()
           AND legacy_user_id = v_user
           AND legacy_customer_id = OLD.id;
        RETURN OLD;
    END IF;

    v_customer_id := NEW.id;
    IF TG_OP = 'INSERT' AND coalesce(v_customer_id, 0) = 0
       AND current_setting('trade_os.lastrowid', true) ~ '^[0-9]+$' THEN
        v_customer_id := current_setting('trade_os.lastrowid', true)::bigint;
    END IF;

    UPDATE trosa.account_legacy_refs
       SET legacy_payload = coalesce(to_jsonb(NEW), '{}'::jsonb)
       WHERE organization_id = trosa.compat_org_id()
           AND legacy_user_id = v_user
       AND legacy_customer_id = v_customer_id;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS zz_customers_ref_payload_write ON trosa.customers;
CREATE TRIGGER zz_customers_ref_payload_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trosa.customers
FOR EACH ROW EXECUTE FUNCTION trosa.compat_customers_ref_payload_write();

DROP TRIGGER IF EXISTS zz_customers_ref_payload_write ON trade_os_compat.customers;
CREATE TRIGGER zz_customers_ref_payload_write
INSTEAD OF INSERT OR UPDATE OR DELETE ON trade_os_compat.customers
FOR EACH ROW EXECUTE FUNCTION trosa.compat_customers_ref_payload_write();

COMMIT;
