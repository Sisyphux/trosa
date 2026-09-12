-- Expose the Customer-owned manual-next-task flag through the modern read
-- model.  Compatibility clients may continue to call it manual_next_follow;
-- modern consumers do not need that historical name.
BEGIN;

CREATE OR REPLACE VIEW trosa.customer_records AS
SELECT ref.legacy_customer_id AS id, a.id AS account_id,
       a.display_name AS name, c.canonical_name AS company, c.country_code AS country,
       a.priority_level AS level, a.channel_type AS customer_type, c.website,
       a.profile, a.field, a.industry, a.company_size, a.annual_revenue, a.tags,
       a.account_status AS status, coalesce(d.notes, '') AS notes,
       coalesce(d.system_notes, '') AS system_notes, coalesce(d.import_source, '') AS import_source,
       coalesce(d.external_source, '') AS external_source, coalesce(d.external_id, '') AS external_id,
       a.is_pinned, a.pinned_order,
       a.deleted_at, a.created_at, a.updated_at,
       coalesce(s.business_stage, '') AS business_stage,
       coalesce(s.business_role, '') AS business_role,
       coalesce(s.customer_judgment, '') AS customer_judgment,
       coalesce(d.manual_next_task, false) AS manual_next_task
  FROM trosa.account_legacy_refs ref
  JOIN trosa.accounts a ON a.id=ref.account_id
  JOIN core.companies c ON c.id=a.company_id
  LEFT JOIN trosa.customer_details d ON d.account_id=a.id
  LEFT JOIN trosa.customer_states s ON s.organization_id=ref.organization_id
       AND s.legacy_user_id=ref.legacy_user_id AND s.legacy_customer_id=ref.legacy_customer_id
 WHERE ref.organization_id=trosa.compat_org_id()
   AND ref.legacy_user_id=trosa.compat_current_user();

COMMIT;
