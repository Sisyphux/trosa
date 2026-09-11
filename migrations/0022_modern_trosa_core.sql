-- Modern Trosa is expressed in canonical PostgreSQL relations.  The old
-- SQLite-shaped views remain a migration/recovery adapter; new product code
-- reads these relations exclusively.
BEGIN;

CREATE TABLE IF NOT EXISTS trosa.customer_details (
    account_id uuid PRIMARY KEY REFERENCES trosa.accounts(id) ON DELETE CASCADE,
    notes text NOT NULL DEFAULT '',
    system_notes text NOT NULL DEFAULT '',
    import_source text NOT NULL DEFAULT '',
    external_source text NOT NULL DEFAULT '',
    external_id text NOT NULL DEFAULT '',
    pinned_at timestamptz,
    manual_next_task boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Copy the still-useful historical attributes once.  They are no longer read
-- from legacy_payload by the modern product surface.
INSERT INTO trosa.customer_details
       (account_id, notes, system_notes, import_source, external_source,
        external_id, pinned_at, manual_next_task)
SELECT a.id,
       coalesce(r.legacy_payload->>'notes', a.legacy_payload->>'notes', ''),
       coalesce(r.legacy_payload->>'system_notes', a.legacy_payload->>'system_notes', ''),
       coalesce(r.legacy_payload->>'import_source', a.legacy_payload->>'import_source', ''),
       coalesce(r.legacy_payload->>'external_source', a.legacy_payload->>'external_source', ''),
       coalesce(r.legacy_payload->>'external_id', a.legacy_payload->>'external_id', ''),
       CASE WHEN coalesce(r.legacy_payload->>'pinned_at', '') ~ '^20[0-9]{2}-[0-9]{2}-[0-9]{2}'
            THEN (r.legacy_payload->>'pinned_at')::timestamptz ELSE NULL END,
       lower(coalesce(r.legacy_payload->>'manual_next_follow', a.legacy_payload->>'manual_next_follow', '0')) IN ('1','true')
  FROM trosa.account_legacy_refs r
  JOIN trosa.accounts a ON a.id=r.account_id
ON CONFLICT (account_id) DO NOTHING;

CREATE INDEX IF NOT EXISTS trosa_customer_details_external_identity_idx
    ON trosa.customer_details (external_source, external_id)
    WHERE external_source <> '' AND external_id <> '';

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
       coalesce(s.customer_judgment, '') AS customer_judgment
  FROM trosa.account_legacy_refs ref
  JOIN trosa.accounts a ON a.id=ref.account_id
  JOIN core.companies c ON c.id=a.company_id
  LEFT JOIN trosa.customer_details d ON d.account_id=a.id
  LEFT JOIN trosa.customer_states s ON s.organization_id=ref.organization_id
       AND s.legacy_user_id=ref.legacy_user_id AND s.legacy_customer_id=ref.legacy_customer_id
 WHERE ref.organization_id=trosa.compat_org_id()
   AND ref.legacy_user_id=trosa.compat_current_user();

CREATE OR REPLACE VIEW trosa.customer_contacts AS
SELECT ref.legacy_contact_id AS id, ref.legacy_customer_id AS customer_id,
       coalesce(nullif(ref.name, ''), person.full_name, '') AS name, ref.title,
       coalesce(method.value, '') AS email, ref.phone, ref.whatsapp, ref.linkedin,
       ref.preferred_channel, ref.contact_type, ref.is_primary, ref.notes,
       ref.created_at, ref.updated_at
  FROM trosa.contact_legacy_refs ref
  LEFT JOIN core.people person ON person.id=ref.person_id
  LEFT JOIN core.contact_methods method ON method.id=ref.contact_method_id
 WHERE ref.organization_id=trosa.compat_org_id()
   AND ref.legacy_user_id=trosa.compat_current_user();

CREATE OR REPLACE VIEW trosa.today_tasks AS
SELECT task_ref.legacy_id AS id, customer_ref.legacy_customer_id AS customer_id,
       task.title, task.content, task.reason, trosa.compat_local_date(task.due_at) AS due_date,
       task.task_type, task.manual_order, customer.display_name AS customer_name,
       company.canonical_name AS customer_company
  FROM trosa.tasks task
  JOIN trosa.account_legacy_refs customer_ref ON customer_ref.account_id=task.account_id
  JOIN trosa.legacy_row_refs task_ref ON task_ref.target_id=task.id
       AND task_ref.table_name='reminders' AND task_ref.organization_id=customer_ref.organization_id
       AND task_ref.legacy_user_id=customer_ref.legacy_user_id
  JOIN trosa.accounts customer ON customer.id=task.account_id
  JOIN core.companies company ON company.id=customer.company_id
 WHERE customer_ref.organization_id=trosa.compat_org_id()
   AND customer_ref.legacy_user_id=trosa.compat_current_user()
   AND task.status='open' AND task.task_type NOT LIKE 'outreach_%'
   AND customer.deleted_at IS NULL;

COMMIT;
