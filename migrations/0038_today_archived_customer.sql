-- Archived Customers must leave Today.
--
-- Archiving a Customer is a per-user soft delete: the canonical write path
-- (trosa_domain.set_customer_deleted) stamps
-- account_legacy_refs.legacy_payload.is_deleted='1' for that user's alias and
-- never touches the shared trosa.accounts.deleted_at.  The Today view only
-- checked customer.deleted_at, so an archived Customer with an open Task kept
-- appearing in 今日跟进 while the Customers list already treated it as
-- archived.  Apply the same per-alias archive predicate that
-- 0030_customer_records_user_scoped_projection.sql established for
-- customer_records: payload wins, accounts.deleted_at stays the fallback.
BEGIN;

CREATE OR REPLACE VIEW trosa.today_tasks AS
SELECT DISTINCT ON (task.id)
       task_ref.legacy_id AS id, customer_ref.legacy_customer_id AS customer_id,
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
   AND trosa.compat_customer_binding(task.legacy_payload, customer_ref.organization_id,
                                     customer_ref.legacy_user_id, customer_ref.account_id,
                                     customer_ref.legacy_customer_id)
   AND task.status='open' AND task.task_type NOT LIKE 'outreach_%'
   AND lower(coalesce(customer_ref.legacy_payload->>'is_deleted',
        CASE WHEN customer.deleted_at IS NULL THEN '0' ELSE '1' END)) NOT IN ('1', 'true')
 ORDER BY task.id, customer_ref.legacy_customer_id, task_ref.legacy_id;

COMMIT;
