-- Today/Sela boundary moves to the content-based relationship bucket.
--
-- 0055/0056 filtered Today with a coarse account-level "real interaction"
-- function (inbound/two-way only).  The calibrated boundary is richer and
-- content-based: a Customer is human-owned when their history shows a real
-- business exchange (inquiry, quote, price feedback, sample, order, payment,
-- meeting, or a concrete customer need/reply) and Sela-owned only for pure
-- one-way development.  That judgment lives in the application
-- (trosa_domain.human_owned_customer_ids), so the Today view now returns every
-- open non-outreach task and the application filters by the relationship bucket.
--
-- trosa.account_has_real_interaction is intentionally kept (not dropped): a
-- rollback to the 0056 code still references it, and forward-only migrations
-- must not strand an older release.
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
