-- Internal Inbox decisions are not customer communication.
--
-- Two writers used to leave a reported Interaction on the customer timeline:
--   * resolving a Sela Agent request ("人工处理 AI 建议", event_type agent_decision)
--   * a note fact recorded from an Inbox dialogue ("[Inbox 对话记录]", event_type human_fact)
-- Both are internal bookkeeping of a human answering the assistant.  They are
-- neither a contact with the customer nor work the member reports, yet they
-- showed up in the weekly "实际工作" and in the 沟通记录.  The writers are gone;
-- this hides the rows they already wrote.
--
-- Rows are only flagged is_deleted (the same soft delete the member's own
-- "delete record" uses), so they stay in timeline_events and can be restored
-- through /api/follow-history/<id>/restore.  The Inbox item resolution and the
-- dialogue receipt remain the audit trail of the decision.
BEGIN;

UPDATE trosa.timeline_events
   SET payload = coalesce(payload, '{}'::jsonb)
                 || jsonb_build_object('is_deleted', true, 'is_reported', false)
 WHERE (event_type = 'agent_decision' AND source_module = 'sela_agent')
    OR (event_type = 'human_fact' AND source_module = 'sela_human_input');

COMMIT;
