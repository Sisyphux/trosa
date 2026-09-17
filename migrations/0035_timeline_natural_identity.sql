-- Enforce one canonical timeline identity per natural source key.
--
-- The unified writer derived ``timeline_events.id`` from
-- ``interaction:{source}:{source_reference or legacy_id}``, omitting the owning
-- account and user.  Two different customers (or two users whose per-user
-- legacy id sequences both start at 1) could therefore address the same UUID,
-- so the second INSERT hit ``ON CONFLICT (id) DO NOTHING`` and the fact
-- silently disappeared.  It also meant the account-scoped ``EXISTS`` dedupe
-- and the generated identity disagreed about their scope.
--
-- The writer now includes organization, user, account, source and reference in
-- the identity, and upserts instead of dropping.  This migration makes the
-- database reject a second event for the same
-- ``(account_id, source_module, source_reference)`` whenever a reference
-- exists.  A pre-existing natural-key duplicate would make this statement fail
-- loudly rather than silently deleting history; that is deliberate, because
-- migration-time row deletion needs an explicit operator-approved plan.
BEGIN;

-- A non-empty source reference identifies one communication per account and
-- source.  Empty references stay unconstrained because each manual entry is a
-- distinct fact.
CREATE UNIQUE INDEX IF NOT EXISTS trosa_timeline_natural_identity_idx
    ON trosa.timeline_events (account_id, source_module, source_reference)
    WHERE source_reference <> '';

COMMIT;
