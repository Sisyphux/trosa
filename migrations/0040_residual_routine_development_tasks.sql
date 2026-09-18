-- Close the routine-development follow-up tasks that migration 0039 missed.
--
-- 0039 only closed a development task when the confirmed outreach happened on
-- or after the task's due date.  Imported tasks were often dated ahead of the
-- contact (e.g. contacted 08-18, task due 09-17), so they stayed in Today as
-- overdue human follow-ups even though the prospect had already been contacted.
-- A confirmed contact satisfies the pre-contact development task whatever
-- synthetic due date it carries; the application rule now does the same.
--
-- Idempotent and non-destructive: only status / completed_at / legacy_payload of
-- open routine-development tasks change; no rows are deleted.
BEGIN;

UPDATE trosa.tasks task
   SET status='done',
       completed_at=coalesce(task.completed_at, now()),
       legacy_payload=coalesce(task.legacy_payload, '{}'::jsonb) || jsonb_build_object(
           'auto_closed_by', 'sela_outreach',
           'auto_closed_at', now()::text),
       updated_at=now()
 WHERE task.status='open'
   AND task.task_type='follow_up'
   AND (
        coalesce(task.title, '')   LIKE '%开发新客户%'
     OR coalesce(task.content, '') LIKE '%开发新客户%'
     OR coalesce(task.reason, '')  LIKE '%开发新客户%'
     OR coalesce(task.title, '')   LIKE '%二次开发%'
     OR coalesce(task.content, '') LIKE '%二次开发%'
     OR coalesce(task.reason, '')  LIKE '%二次开发%'
     OR coalesce(task.title, '')   LIKE '%开发信%'
     OR coalesce(task.content, '') LIKE '%开发信%'
     OR coalesce(task.reason, '')  LIKE '%开发信%'
     OR coalesce(task.reason, '')  LIKE '%待首次联系%'
     OR coalesce(task.reason, '')  LIKE '%官网导入%'
     OR coalesce(task.content, '') LIKE '%新开发流程%'
   )
   AND EXISTS (
        SELECT 1
          FROM trosa.outreach_messages message
         WHERE message.account_id = task.account_id
           AND message.sent_at IS NOT NULL
           AND lower(coalesce(message.reply_status, '')) <> 'bounced'
   );

COMMIT;
