-- sela is a tool, not a customer source.
--
-- Customers discovered by sela carried the source "Sela" (or, for older
-- imports, no source at all so the screen fell back to the raw tag "sela" /
-- "sela-v2").  The real channel was never recorded, and the owner's standing
-- decision is that an unknown channel reads as a Google / website search.
--
-- * "Sela" sources become "Google / 官网搜索".
-- * Customers whose only provenance is sela (import_source sela / sela-v2 or an
--   external link to sela, and not a manual / Excel entry) and have no source
--   get "Google / 官网搜索".  A source a person recorded is never touched.
-- Idempotent; only the source text changes.
BEGIN;

UPDATE trosa.customer_details
   SET source = 'Google / 官网搜索'
 WHERE lower(source) IN ('sela', '自动开发')
    OR (source = ''
        AND (lower(import_source) IN ('sela', 'sela-v2')
             OR lower(external_source) IN ('sela', 'sela-history'))
        AND lower(import_source) NOT IN ('manual', 'excel'));

UPDATE trosa.account_legacy_refs
   SET legacy_payload = jsonb_set(legacy_payload, '{source}', to_jsonb('Google / 官网搜索'::text))
 WHERE lower(coalesce(legacy_payload->>'source', '')) IN ('sela', '自动开发');

COMMIT;
