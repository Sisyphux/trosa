-- Keep the modern Customer list user-scoped when several legacy customers
-- share one canonical account.
--
-- The importer keys canonical accounts by company, so Hamid and Amy can each
-- hold a legacy reference (for example Unico Co. or Durolenz) that points at
-- the same trosa.accounts row.  Migration 0016 keeps every legacy customer's
-- own payload on its account_legacy_refs row, and trosa.customers projects
-- those per-reference values first.  trosa.customer_records regressed to
-- reading the shared account/company/detail columns directly, so one user's
-- archived or edited customer resurfaced in the other user's active list.
--
-- This migration is forward-only and changes no stored rows.  It redefines
-- only the customer_records projection to prefer the caller's own legacy
-- payload and fall back to the shared canonical columns, mirroring the
-- trosa.customers boundary from 0016/0020.  History, contacts, tasks and
-- timeline views already join with a matching legacy_user_id on both sides.
BEGIN;

CREATE OR REPLACE VIEW trosa.customer_records AS
SELECT ref.legacy_customer_id AS id, a.id AS account_id,
       CASE WHEN ref.legacy_payload ? 'name' THEN coalesce(ref.legacy_payload->>'name', '')
            ELSE a.display_name END AS name,
       CASE WHEN ref.legacy_payload ? 'company' THEN coalesce(ref.legacy_payload->>'company', '')
            ELSE c.canonical_name END AS company,
       CASE WHEN ref.legacy_payload ? 'country' THEN coalesce(ref.legacy_payload->>'country', '')
            ELSE c.country_code END AS country,
       CASE WHEN ref.legacy_payload ? 'level' THEN coalesce(ref.legacy_payload->>'level', '')
            ELSE a.priority_level END AS level,
       CASE WHEN ref.legacy_payload ? 'type' THEN coalesce(ref.legacy_payload->>'type', '')
            ELSE a.channel_type END AS customer_type,
       CASE WHEN ref.legacy_payload ? 'website' THEN coalesce(ref.legacy_payload->>'website', '')
            ELSE c.website END AS website,
       CASE WHEN ref.legacy_payload ? 'profile' THEN coalesce(ref.legacy_payload->>'profile', '')
            ELSE a.profile END AS profile,
       CASE WHEN ref.legacy_payload ? 'field' THEN coalesce(ref.legacy_payload->>'field', '')
            ELSE a.field END AS field,
       CASE WHEN ref.legacy_payload ? 'industry' THEN coalesce(ref.legacy_payload->>'industry', '')
            ELSE a.industry END AS industry,
       CASE WHEN ref.legacy_payload ? 'company_size' THEN coalesce(ref.legacy_payload->>'company_size', '')
            ELSE a.company_size END AS company_size,
       CASE WHEN ref.legacy_payload ? 'annual_revenue' THEN coalesce(ref.legacy_payload->>'annual_revenue', '')
            ELSE a.annual_revenue END AS annual_revenue,
       CASE WHEN ref.legacy_payload ? 'tags' THEN coalesce(ref.legacy_payload->>'tags', '')
            ELSE a.tags END AS tags,
       CASE WHEN ref.legacy_payload ? 'status' THEN coalesce(ref.legacy_payload->>'status', '')
            ELSE a.account_status END AS status,
       CASE WHEN ref.legacy_payload ? 'notes' THEN coalesce(ref.legacy_payload->>'notes', '')
            ELSE coalesce(d.notes, '') END AS notes,
       CASE WHEN ref.legacy_payload ? 'system_notes' THEN coalesce(ref.legacy_payload->>'system_notes', '')
            ELSE coalesce(d.system_notes, '') END AS system_notes,
       CASE WHEN ref.legacy_payload ? 'import_source' THEN coalesce(ref.legacy_payload->>'import_source', '')
            ELSE coalesce(d.import_source, '') END AS import_source,
       CASE WHEN ref.legacy_payload ? 'external_source' THEN coalesce(ref.legacy_payload->>'external_source', '')
            ELSE coalesce(d.external_source, '') END AS external_source,
       CASE WHEN ref.legacy_payload ? 'external_id' THEN coalesce(ref.legacy_payload->>'external_id', '')
            ELSE coalesce(d.external_id, '') END AS external_id,
       CASE WHEN lower(coalesce(
                    CASE WHEN ref.legacy_payload ? 'is_pinned'
                         THEN ref.legacy_payload->>'is_pinned' END,
                    CASE WHEN a.is_pinned THEN '1' ELSE '0' END)) IN ('1', 'true')
            THEN true ELSE false END AS is_pinned,
       CASE WHEN coalesce(ref.legacy_payload->>'pinned_order', '') ~ '^-?[0-9]+$'
            THEN (ref.legacy_payload->>'pinned_order')::integer
            ELSE a.pinned_order END AS pinned_order,
       CASE WHEN lower(coalesce(ref.legacy_payload->>'is_deleted',
                    CASE WHEN a.deleted_at IS NULL THEN '0' ELSE '1' END)) IN ('1', 'true')
            THEN coalesce(NULLIF(ref.legacy_payload->>'deleted_at', '')::timestamptz,
                          a.deleted_at, now())
            ELSE NULL END AS deleted_at,
       a.created_at, a.updated_at,
       coalesce(s.business_stage, '') AS business_stage,
       coalesce(s.business_role, '') AS business_role,
       coalesce(s.customer_judgment, '') AS customer_judgment,
       CASE WHEN lower(coalesce(
                    CASE WHEN ref.legacy_payload ? 'manual_next_follow'
                         THEN ref.legacy_payload->>'manual_next_follow' END,
                    CASE WHEN d.manual_next_task THEN '1' ELSE '0' END)) IN ('1', 'true')
            THEN true ELSE false END AS manual_next_task,
       CASE WHEN ref.legacy_payload ? 'last_contact' THEN coalesce(ref.legacy_payload->>'last_contact', '')
            WHEN a.last_contact_at IS NOT NULL THEN trosa.compat_local_date(a.last_contact_at)
            ELSE coalesce(a.legacy_payload->>'last_contact', '') END AS last_interaction_on,
       CASE WHEN ref.legacy_payload ? 'next_follow_up' THEN coalesce(ref.legacy_payload->>'next_follow_up', '')
            WHEN a.next_follow_up_at IS NOT NULL THEN trosa.compat_local_date(a.next_follow_up_at)
            ELSE coalesce(a.legacy_payload->>'next_follow_up', '') END AS next_task_on,
       CASE WHEN ref.legacy_payload ? 'pinned_at' THEN coalesce(ref.legacy_payload->>'pinned_at', '')
            ELSE trosa.compat_local_date(d.pinned_at) END AS pinned_at
  FROM trosa.account_legacy_refs ref
  JOIN trosa.accounts a ON a.id=ref.account_id
  JOIN core.companies c ON c.id=a.company_id
  LEFT JOIN trosa.customer_details d ON d.account_id=a.id
  LEFT JOIN trosa.customer_states s ON s.organization_id=ref.organization_id
       AND s.legacy_user_id=ref.legacy_user_id AND s.legacy_customer_id=ref.legacy_customer_id
 WHERE ref.organization_id=trosa.compat_org_id()
   AND ref.legacy_user_id=trosa.compat_current_user();

COMMIT;
