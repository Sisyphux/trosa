-- One canonical task must produce one Today row.
--
-- account_legacy_refs can contain more than one legacy customer id for an
-- account after historical customer merges.  The old Today view joined every
-- such alias to the same canonical task, so one task was rendered repeatedly
-- with different customer_id values.  Keep one deterministic compatibility
-- id for the task; writes still resolve that id back to the same account.
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
   AND task.status='open' AND task.task_type NOT LIKE 'outreach_%'
   AND customer.deleted_at IS NULL
 ORDER BY task.id, customer_ref.legacy_customer_id, task_ref.legacy_id;

COMMIT;
