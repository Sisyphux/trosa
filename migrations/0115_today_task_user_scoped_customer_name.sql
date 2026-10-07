-- Today must show the caller's own customer name on a shared account.
--
-- 0016 made customer fields user-scoped: when several legacy customers point
-- at one canonical account, each member's name/company lives on their own
-- account_legacy_refs.legacy_payload and trosa.customers reads it from there.
-- trosa.today_tasks was never moved onto that boundary and still projected the
-- canonical account's display_name / company canonical_name.  On an account
-- that an earlier import shared across members (Aura Trading Company for one
-- member, KPS Global Solutions for another, linked through a linkedin.com
-- "website"), the Today row therefore showed the other member's customer name
-- while the task, timeline and detail all belonged to the caller's customer.
--
-- This only changes the two name columns; the row set, ordering and customer
-- binding are identical to 0059.
BEGIN;

CREATE OR REPLACE VIEW trosa.today_tasks AS
SELECT DISTINCT ON (task.id)
       task_ref.legacy_id AS id, customer_ref.legacy_customer_id AS customer_id,
       task.title, task.content, task.reason, trosa.compat_local_date(task.due_at) AS due_date,
       task.task_type, task.manual_order,
       CASE WHEN customer_ref.legacy_payload ? 'name'
            THEN coalesce(customer_ref.legacy_payload->>'name', '')
            ELSE customer.display_name END AS customer_name,
       CASE WHEN customer_ref.legacy_payload ? 'company'
            THEN coalesce(customer_ref.legacy_payload->>'company', '')
            ELSE company.canonical_name END AS customer_company
  FROM trosa.tasks task
  JOIN trosa.account_legacy_refs customer_ref ON customer_ref.account_id=task.account_id
  JOIN trosa.legacy_row_refs task_ref ON task_ref.target_id=task.id
       AND task_ref.table_name='reminders' AND task_ref.organization_id=customer_ref.organization_id
       AND task_ref.legacy_user_id=customer_ref.legacy_user_id
  JOIN trosa.accounts customer ON customer.id=task.account_id
  JOIN core.companies company ON company.id=customer.company_id
 WHERE customer_ref.organization_id=trosa.compat_org_id()
   AND customer_ref.legacy_user_id=trosa.compat_current_user()
   AND trosa.compat_customer_binding(task.legacy_payload, customer_ref.organization_id,
                                     customer_ref.legacy_user_id, customer_ref.account_id,
                                     customer_ref.legacy_customer_id)
   AND task.status='open' AND task.task_type NOT LIKE 'outreach_%'
   AND lower(coalesce(customer_ref.legacy_payload->>'is_deleted',
        CASE WHEN customer.deleted_at IS NULL THEN '0' ELSE '1' END)) NOT IN ('1', 'true')
 ORDER BY task.id, customer_ref.legacy_customer_id, task_ref.legacy_id;

COMMIT;
