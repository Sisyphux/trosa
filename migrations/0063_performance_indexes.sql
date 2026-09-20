-- Indexes for high-frequency read paths that lacked an access path.
--
-- The Today, Customer, Inbox and delivery-status surfaces filter and join the
-- canonical Task, Outreach, Inbox and Email-Delivery tables by owner/status,
-- but only primary keys existed on several of them.  Coverage stayed invisible
-- on small datasets and turned into sequential scans plus per-row work once
-- records accumulated.  These are forward-only, non-destructive additions.
BEGIN;

-- Customer/Today task projections filter by owner and open status and order
-- by due date; the only prior index was a partial unique on follow-ups.
CREATE INDEX IF NOT EXISTS trosa_tasks_account_status_due_idx
    ON trosa.tasks (account_id, status, due_at);

-- Customer-scoped outreach reads and the Interaction UNION join by account and
-- order newest-first.
CREATE INDEX IF NOT EXISTS trosa_outreach_account_sent_idx
    ON trosa.outreach_messages (account_id, sent_at DESC);

-- Delivery-state reconciliation lookups by the provider message id.
CREATE INDEX IF NOT EXISTS trosa_outreach_provider_message_idx
    ON trosa.outreach_messages (provider_message_id)
    WHERE provider_message_id <> '';

-- Inbox listing/counts filter by status and order by creation time, and
-- customer deletion probes by account.
CREATE INDEX IF NOT EXISTS trosa_inbox_status_created_idx
    ON trosa.inbox_items (status, created_at DESC);
CREATE INDEX IF NOT EXISTS trosa_inbox_account_idx
    ON trosa.inbox_items (account_id);

-- Gmail/outreach delivery events are joined back by message.
CREATE INDEX IF NOT EXISTS trosa_email_delivery_outreach_idx
    ON trosa.email_delivery_events (outreach_message_id);

-- Contact cascade/reference checks join by account.
CREATE INDEX IF NOT EXISTS trosa_contact_legacy_refs_account_idx
    ON trosa.contact_legacy_refs (account_id);

COMMIT;
