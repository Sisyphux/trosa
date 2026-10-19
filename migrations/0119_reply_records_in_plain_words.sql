-- Reply records must read like a person's own work note.
--
-- Sela's reply handoff wrote the integration's internal codes into the result
-- column ("自动处理：REPLY；事件：REPLIED；意图：UNKNOWN", sometimes followed by a
-- Gmail message id), and the summary model sometimes appended a machine remark
-- ("CRM 中未找到该邮件涉及的产品…") to the content.  Both showed up in the weekly
-- report.  This rewrites the existing rows; the writers now produce plain text.
--
-- Idempotent; only result/content text of sela_reply_engine rows changes.
BEGIN;

UPDATE trosa.timeline_events
   SET result = CASE
         WHEN result LIKE '%已发送资料回复%' THEN '已收到客户回复，并已回复发送资料'
         WHEN result LIKE '%事件：NOT_INTERESTED%' THEN '客户暂无意向'
         WHEN result LIKE '%事件：INTERESTED%' THEN '客户有意向，待人工跟进'
         ELSE '已收到客户回复，待人工跟进'
       END
 WHERE source_module = 'sela_reply_engine'
   AND result LIKE '自动处理：%';

UPDATE trosa.timeline_events
   SET result = '已回复客户'
 WHERE source_module = 'sela_reply_engine'
   AND event_type = 'email' AND direction = 'outbound'
   AND result IN ('REPLY', 'REPLIED', 'AUTO_REPLY');

UPDATE trosa.timeline_events
   SET content = btrim(regexp_replace(content,
         '[^。；;\n]*(CRM|crm)[^。；;\n]*(未找到|没有找到|未发现|无相关)[^。；;\n]*[。；;]?', '', 'g'))
 WHERE source_module = 'sela_reply_engine'
   AND content ~ '(CRM|crm)[^。；;\n]*(未找到|没有找到|未发现|无相关)';

COMMIT;
