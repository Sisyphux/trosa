-- Close legacy routine-development follow-up tasks that a confirmed automatic
-- (Sela) outreach already satisfied, so they stop showing up in Today as
-- overdue human follow-ups.
--
-- Imported / development-experiment tasks ("开发新客户", "官网导入，待首次联系")
-- were created before Sela owned the prospect lifecycle.  Trosa records the
-- confirmed outreach as a fact, but nothing ever closed those old follow_up
-- rows, so Today kept the prospect even after it had been contacted.  New writes
-- are handled by app._complete_routine_development_tasks; this heals existing
-- data with the same rule and markers.
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
   AND task.due_at IS NOT NULL
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
           AND (
                message.provider = 'sela'
             OR coalesce(message.legacy_payload->>'external_source', '') = 'sela'
           )
           AND (message.sent_at AT TIME ZONE 'Asia/Shanghai')::date
               >= (task.due_at AT TIME ZONE 'Asia/Shanghai')::date
   );

COMMIT;
