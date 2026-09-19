-- Inbox 问题模型：Inbox 只承接必须由人作出判断的未决问题。
--
-- 这次前向迁移把「技术来源」和「业务问题」分开，并记录结论来源，使系统能够：
--   1. 把同一业务问题的多条证据收敛为一个问题（question_key）；
--   2. 自动关闭不再需要人工判断的历史问题，并保留原因（resolution_source/reason）；
--   3. 前端围绕问题而不是来源组织（question_kind/source_type）。
--
-- 迁移不删除任何历史证据：被自动关闭的行仍保留原文、来源与时间，只是不再
-- 占用 Inbox。已应用的迁移是 forward-only，后续修正必须新增前向迁移。
BEGIN;

ALTER TABLE trosa.inbox_items ADD COLUMN IF NOT EXISTS question_kind text NOT NULL DEFAULT '';
ALTER TABLE trosa.inbox_items ADD COLUMN IF NOT EXISTS question_key text NOT NULL DEFAULT '';
ALTER TABLE trosa.inbox_items ADD COLUMN IF NOT EXISTS source_type text NOT NULL DEFAULT '';
ALTER TABLE trosa.inbox_items ADD COLUMN IF NOT EXISTS resolution_source text NOT NULL DEFAULT '';
ALTER TABLE trosa.inbox_items ADD COLUMN IF NOT EXISTS resolved_by text NOT NULL DEFAULT '';
ALTER TABLE trosa.inbox_items ADD COLUMN IF NOT EXISTS evidence jsonb NOT NULL DEFAULT '[]'::jsonb;

-- 把历史行标记成业务问题类别与技术来源；技术来源只作为次级证据。
UPDATE trosa.inbox_items
   SET question_kind = CASE item_type
         WHEN 'gmail_capture' THEN 'identity'
         WHEN 'browser_capture' THEN 'identity'
         WHEN 'customer_reply' THEN 'reply'
         WHEN 'sela_agent_request' THEN 'approval'
         WHEN 'sela_identity_review' THEN 'identity_review'
         WHEN 'sela_exclusion_review' THEN 'identity_review'
         ELSE 'identity' END,
       source_type = CASE item_type
         WHEN 'gmail_capture' THEN 'gmail'
         WHEN 'browser_capture' THEN 'browser'
         WHEN 'customer_reply' THEN 'inbox'
         WHEN 'sela_agent_request' THEN 'sela'
         WHEN 'sela_identity_review' THEN 'sela'
         WHEN 'sela_exclusion_review' THEN 'sela'
         ELSE 'system' END
 WHERE question_kind = '';

-- 每条历史证据的默认问题键就是它自己的去重键；运行时 reconciler 会把同一发件
-- 人/来源的证据收敛到同一问题键。
UPDATE trosa.inbox_items
   SET question_key = COALESCE(NULLIF(legacy_payload->>'compat_dedupe_key', ''), dedupe_key)
 WHERE question_key = '';

-- 已退役的类型不再代表需要人工判断的问题。保留行和证据，标记为自动关闭。
UPDATE trosa.inbox_items
   SET status = 'resolved',
       resolved_at = COALESCE(resolved_at, now()),
       resolution_source = 'auto',
       resolution_reason = 'retired_question_type',
       resolution_note = '该类型已不再代表需要人工判断的问题；历史记录保留用于审计。'
 WHERE status = 'open'
   AND item_type IN ('new_customer', 'ai_suggestion', 'uncontacted_follow_up',
                     'sela_follow_up', 'sela_proposal');

-- 退信/投递状态/系统退信通知是投递事实，不是需要归属的客户沟通。
UPDATE trosa.inbox_items
   SET status = 'resolved',
       resolved_at = COALESCE(resolved_at, now()),
       resolution_source = 'auto',
       resolution_reason = 'inbound_noise',
       resolution_note = '退信或系统通知是投递事实，不需要人工归属。'
 WHERE status = 'open'
   AND item_type = 'gmail_capture'
   AND (title ILIKE '%mail delivery%'
        OR title ILIKE '%delivery status notification%'
        OR title ILIKE '%undeliverable%'
        OR title ILIKE '%failure notice%'
        OR title ILIKE '%returned mail%'
        OR title ILIKE '%退信%'
        OR title ILIKE '%mailer-daemon%');

CREATE INDEX IF NOT EXISTS trosa_inbox_question_key_idx
    ON trosa.inbox_items (question_key)
 WHERE status = 'open';

COMMIT;
